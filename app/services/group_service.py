"""
Group service — seller group lifecycle.

Handles:
- Registration of seller groups
- Admin moderation (approve/reject)
- Bot rights checks
- Group settings management
"""

from __future__ import annotations

import logging
import secrets
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SellerGroup
from app.repositories.group_repo import GroupRepository
from app.integrations.telegram_api import TelegramAPIService
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class GroupService:
    """Seller group management."""

    def __init__(self, session: AsyncSession, telegram_api: TelegramAPIService):
        self.session = session
        self.repo = GroupRepository(session)
        self.telegram_api = telegram_api

    # ── Registration ─────────────────────────────────────────────────────

    async def register_group(
        self,
        user_id: int,
        chat_id: int,
        category_id: int,
    ) -> SellerGroup:
        """
        Register a seller group.

        Steps:
        1. Check group suitability (supergroup, bot admin, can_restrict)
        2. Check if group already registered
        3. Create pending group record
        """
        # Already registered?
        existing = await self.repo.get_by_chat_id(chat_id)
        if existing:
            raise ValueError("Эта группа уже зарегистрирована.")

        # Check suitability via Telegram API
        check = await self.telegram_api.check_group_suitability(chat_id)
        if not check.ok:
            raise ValueError(check.reason)

        # Generate deep link code
        deep_link = f"grp_{secrets.token_urlsafe(8)}"

        group = SellerGroup(
            user_id=user_id,
            telegram_chat_id=chat_id,
            title=check.chat.title if check.chat else None,
            username=check.chat.username if check.chat else None,
            category_id=category_id,
            status="pending",
            member_count=check.member_count,
            deep_link_code=deep_link,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.session.add(group)
        await self.session.flush()

        logger.info(
            "Group registered: id=%d chat_id=%d user=%d",
            group.id, chat_id, user_id,
        )
        return group

    # ── Moderation ───────────────────────────────────────────────────────

    async def approve(self, group_id: int) -> bool:
        """Approve a pending group."""
        group = await self.repo.get_by_id(group_id)
        if not group or group.status != "pending":
            return False

        group.status = "approved"
        group.updated_at = utc_now()
        logger.info("Group %d approved", group_id)
        return True

    async def reject(self, group_id: int, reason: str) -> bool:
        """Reject a pending group."""
        group = await self.repo.get_by_id(group_id)
        if not group or group.status != "pending":
            return False

        group.status = "rejected"
        group.rejection_reason = reason
        group.updated_at = utc_now()
        logger.info("Group %d rejected: %s", group_id, reason)
        return True

    async def disable(self, group_id: int) -> bool:
        """Disable an approved group."""
        group = await self.repo.get_by_id(group_id)
        if not group or group.status != "approved":
            return False

        group.status = "disabled"
        group.updated_at = utc_now()
        return True

    # ── Bot Rights ───────────────────────────────────────────────────────

    async def check_and_update_rights(self, group: SellerGroup) -> bool:
        """
        Check if bot still has admin rights.
        Updates bot_has_rights field.
        Returns current rights status.
        """
        check = await self.telegram_api.check_group_suitability(
            group.telegram_chat_id
        )

        had_rights = group.bot_has_rights
        group.bot_has_rights = check.ok

        if not check.ok and had_rights:
            group.rights_lost_at = utc_now()
            logger.warning(
                "Bot lost rights in group %d (%s): %s",
                group.id, group.telegram_chat_id, check.reason,
            )
        elif check.ok and not had_rights:
            group.rights_lost_at = None
            logger.info("Bot rights restored in group %d", group.id)

        # Update member count
        if check.member_count > 0:
            group.member_count = check.member_count

        group.updated_at = utc_now()
        return check.ok

    # ── Settings ─────────────────────────────────────────────────────────

    async def update_settings(
        self,
        group_id: int,
        tasks_per_distribution: int | None = None,
        interval_minutes: int | None = None,
    ) -> SellerGroup:
        """Update group distribution settings."""
        group = await self.repo.get_by_id(group_id)
        if not group:
            raise ValueError("Группа не найдена.")

        if tasks_per_distribution is not None:
            if tasks_per_distribution < 1 or tasks_per_distribution > 50:
                raise ValueError("Количество заданий: от 1 до 50.")
            group.tasks_per_distribution = tasks_per_distribution

        if interval_minutes is not None:
            if interval_minutes < 1 or interval_minutes > 43200:
                raise ValueError("Интервал: от 1 минуты до 30 дней.")
            group.interval_minutes = interval_minutes

        group.updated_at = utc_now()
        return group

    # ── Queries ──────────────────────────────────────────────────────────

    async def get_by_id(self, group_id: int) -> SellerGroup | None:
        return await self.repo.get_by_id(group_id)

    async def get_by_chat_id(self, chat_id: int) -> SellerGroup | None:
        return await self.repo.get_by_chat_id(chat_id)

    async def get_by_deep_link(self, code: str) -> SellerGroup | None:
        return await self.repo.get_by_deep_link(code)

    async def get_by_user(self, user_id: int) -> Sequence[SellerGroup]:
        return await self.repo.get_by_user(user_id)

    async def get_pending(self) -> Sequence[SellerGroup]:
        return await self.repo.get_pending()

    async def get_approved_by_category(
        self, category_id: int
    ) -> Sequence[SellerGroup]:
        return await self.repo.get_approved_by_category(category_id)
