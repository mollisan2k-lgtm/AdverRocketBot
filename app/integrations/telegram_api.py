"""
Telegram Bot API service — wrapper over aiogram Bot for business operations.

Handles:
- Group suitability checks (supergroup, bot admin, can_restrict_members)
- Restrict / unrestrict members with permission tracking
- Target (channel/group) validation for campaigns
- Chat member status checks

All HTTP calls here are meant to be called OUTSIDE of DB transactions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.types import (
    Chat,
    ChatMember,
    ChatMemberAdministrator,
    ChatMemberRestricted,
    ChatPermissions,
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logger = logging.getLogger(__name__)


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class GroupCheckResult:
    ok: bool
    reason: str = ""
    chat: Chat | None = None
    member_count: int = 0


@dataclass
class TargetInfo:
    """Validated target for a campaign."""
    target_chat_id: int
    target_type: str          # 'channel' or 'group'
    target_username: str | None
    target_title: str


# ── Permission constants ─────────────────────────────────────────────────────

# Restrict: deny all message-sending capabilities
RESTRICTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    # Deliberately NOT setting admin-level permissions:
    # can_change_info, can_invite_users, can_pin_messages, can_manage_topics
    # These are left as-is (inherited from group defaults).
)

# Default user permissions for unrestrict (safe set, no admin rights)
DEFAULT_USER_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    # NOT granting admin permissions:
    # can_change_info=False,
    # can_invite_users=False,
    # can_pin_messages=False,
    # can_manage_topics=False,
)


# ── Service ──────────────────────────────────────────────────────────────────

class TelegramAPIService:
    """
    Business-level wrapper over aiogram Bot.

    All methods perform HTTP calls to Telegram API and must be called
    OUTSIDE of database transactions.
    """

    def __init__(self, bot: Bot):
        self.bot = bot

    # ── Group checks ─────────────────────────────────────────────────────

    async def check_group_suitability(self, chat_id: int) -> GroupCheckResult:
        """
        Check if a group is suitable for seller registration.

        Requirements (v4 plan):
        1. Chat is a supergroup
        2. Bot is an administrator
        3. Bot has can_restrict_members
        """
        try:
            chat = await self.bot.get_chat(chat_id)
        except TelegramBadRequest:
            return GroupCheckResult(ok=False, reason="Группа не найдена или бот не имеет доступа.")
        except TelegramForbiddenError:
            return GroupCheckResult(ok=False, reason="Бот заблокирован в этой группе.")

        # 1. Supergroup check
        if chat.type != "supergroup":
            return GroupCheckResult(
                ok=False,
                reason="Подключить можно только супергруппу. "
                       "Обычные группы не поддерживают ограничение участников.",
            )

        # 2-3. Bot admin with can_restrict_members
        try:
            bot_member = await self.bot.get_chat_member(chat_id, self.bot.id)
        except TelegramBadRequest:
            return GroupCheckResult(ok=False, reason="Не удалось проверить права бота.")

        if not isinstance(bot_member, ChatMemberAdministrator):
            return GroupCheckResult(
                ok=False,
                reason="Бот должен быть администратором группы.",
            )

        if not bot_member.can_restrict_members:
            return GroupCheckResult(
                ok=False,
                reason="Боту необходимо право «Ограничение участников» (can_restrict_members).",
            )

        return GroupCheckResult(
            ok=True,
            chat=chat,
            member_count=chat.member_count or 0,
        )

    # ── Target validation (for campaigns) ────────────────────────────────

    async def validate_campaign_target(
        self, target_input: str, target_type: str
    ) -> TargetInfo:
        """
        Validate a campaign target (channel or group) via Telegram API.

        Ensures:
        1. Target exists and bot can access it
        2. Target type matches (channel / group / supergroup)
        3. Bot is admin (required for getChatMember checks on other users)
        4. Returns stable chat_id

        Raises ValueError with a user-friendly message on failure.
        """
        # 1. Get chat info
        try:
            chat = await self.bot.get_chat(target_input)
        except TelegramBadRequest:
            raise ValueError(
                "Канал/группа не найден(а) или бот не имеет доступа. "
                "Убедитесь, что бот добавлен в канал/группу."
            )
        except TelegramForbiddenError:
            raise ValueError("Бот заблокирован в этом канале/группе.")

        # 2. Type check
        allowed_types: dict[str, tuple[str, ...]] = {
            "channel": ("channel",),
            "group": ("group", "supergroup"),
        }
        if chat.type not in allowed_types.get(target_type, ()):
            raise ValueError(
                f"Объект не является {target_type}. "
                f"Обнаружен тип: {chat.type}."
            )

        # 3. Bot must be admin for reliable getChatMember on other users
        try:
            bot_member = await self.bot.get_chat_member(chat.id, self.bot.id)
        except TelegramBadRequest:
            raise ValueError("Не удалось проверить права бота в канале/группе.")

        if not isinstance(bot_member, ChatMemberAdministrator):
            raise ValueError(
                "Бот должен быть администратором канала/группы "
                "для надёжной проверки подписок участников."
            )

        return TargetInfo(
            target_chat_id=chat.id,
            target_type=target_type,
            target_username=chat.username,
            target_title=chat.title or "Без названия",
        )

    # ── Chat member checks ───────────────────────────────────────────────

    async def get_chat_member_safe(
        self, chat_id: int, user_id: int
    ) -> ChatMember | None:
        """Get chat member status, return None on error."""
        try:
            return await self.bot.get_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            return None

    async def is_user_subscribed(self, chat_id: int, user_id: int) -> bool:
        """Check if user is a member (subscribed) to the chat."""
        member = await self.get_chat_member_safe(chat_id, user_id)
        if member is None:
            return False
        return member.status in ("member", "administrator", "creator", "restricted")

    async def is_user_member(self, chat_id: int, user_id: int) -> bool | None:
        """
        Check if user is an active member of the chat.
        Returns None if check failed (API error).
        """
        member = await self.get_chat_member_safe(chat_id, user_id)
        if member is None:
            return None
        # 'left' and 'kicked' mean not a member
        return member.status not in ("left", "kicked")

    # ── Restrict / Unrestrict ────────────────────────────────────────────

    async def restrict_member(self, chat_id: int, user_id: int) -> bool:
        """
        Apply message restriction to a user.
        Returns True on success, False on failure.
        """
        try:
            await self.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=RESTRICTED_PERMISSIONS,
            )
            return True
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning(
                "Failed to restrict user %d in chat %d: %s",
                user_id, chat_id, e,
            )
            return False

    async def unrestrict_member(
        self,
        chat_id: int,
        user_id: int,
        original_permissions: dict[str, Any] | None = None,
    ) -> bool:
        """
        Remove bot-applied restriction from a user.

        If original_permissions are provided (from user_restrictions table),
        restore those. Otherwise, apply DEFAULT_USER_PERMISSIONS.
        """
        if original_permissions:
            permissions = ChatPermissions(**original_permissions)
        else:
            permissions = DEFAULT_USER_PERMISSIONS

        try:
            await self.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=permissions,
            )
            return True
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning(
                "Failed to unrestrict user %d in chat %d: %s",
                user_id, chat_id, e,
            )
            return False

    async def get_member_permissions(
        self, chat_id: int, user_id: int
    ) -> dict[str, Any] | None:
        """
        Extract current permissions of a user before restricting.
        Returns a dict suitable for ChatPermissions constructor, or None.
        """
        member = await self.get_chat_member_safe(chat_id, user_id)
        if member is None:
            return None

        if isinstance(member, ChatMemberRestricted):
            # Already restricted — capture current state
            return {
                "can_send_messages": member.can_send_messages,
                "can_send_audios": member.can_send_audios,
                "can_send_documents": member.can_send_documents,
                "can_send_photos": member.can_send_photos,
                "can_send_videos": member.can_send_videos,
                "can_send_video_notes": member.can_send_video_notes,
                "can_send_voice_notes": member.can_send_voice_notes,
                "can_send_polls": member.can_send_polls,
                "can_send_other_messages": member.can_send_other_messages,
                "can_add_web_page_previews": member.can_add_web_page_previews,
                "can_change_info": member.can_change_info,
                "can_invite_users": member.can_invite_users,
                "can_pin_messages": member.can_pin_messages,
                "can_manage_topics": member.can_manage_topics,
            }

        # Regular member — no specific permissions stored
        return None

    # ── Utility ──────────────────────────────────────────────────────────

    async def get_chat(self, chat_id: int | str) -> Chat:
        """Get chat info. Raises TelegramBadRequest if not found."""
        return await self.bot.get_chat(chat_id)

    async def health_check(self) -> bool:
        """Check Telegram API connectivity."""
        try:
            me = await self.bot.get_me()
            return me is not None
        except Exception:
            return False
