"""
User service — registration, profile management.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.repositories.user_repo import UserRepository
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class UserService:
    """Manages user registration, lookup, and blocking."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = UserRepository(session)

    async def get_or_create(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> User:
        """Find existing user or create new one."""
        user = await self.repo.get_by_telegram_id(telegram_id)
        if user:
            # Update profile fields if changed
            changed = False
            if username and user.username != username:
                user.username = username
                changed = True
            if first_name and user.first_name != first_name:
                user.first_name = first_name
                changed = True
            if last_name and user.last_name != last_name:
                user.last_name = last_name
                changed = True
            if changed:
                user.updated_at = utc_now()
            return user

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            available="0.00",
            reserved="0.00",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.session.add(user)
        await self.session.flush()
        logger.info("New user registered: tg_id=%d", telegram_id)
        return user

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.repo.get_by_telegram_id(telegram_id)

    async def get_by_id(self, user_id: int) -> User | None:
        return await self.repo.get_by_id(user_id)

    async def block_user(
        self, user_id: int, reason: str
    ) -> bool:
        """Block a user. Returns True on success."""
        user = await self.repo.get_by_id(user_id)
        if not user:
            return False
        user.is_blocked = True
        user.block_reason = reason
        user.blocked_at = utc_now()
        user.updated_at = utc_now()
        return True

    async def unblock_user(self, user_id: int) -> bool:
        """Unblock a user."""
        user = await self.repo.get_by_id(user_id)
        if not user:
            return False
        user.is_blocked = False
        user.block_reason = None
        user.blocked_at = None
        user.updated_at = utc_now()
        return True
