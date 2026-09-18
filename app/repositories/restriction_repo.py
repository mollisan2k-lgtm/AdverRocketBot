"""User restriction repository (v4 bot-applied restrictions)."""

from __future__ import annotations

import json

from sqlalchemy import select, delete

from app.db.models import UserRestriction
from app.repositories.base import BaseRepository


class RestrictionRepository(BaseRepository[UserRestriction]):
    model = UserRestriction

    async def get_record(
        self, group_id: int, user_telegram_id: int
    ) -> UserRestriction | None:
        """Get restriction record for (group, user) pair."""
        result = await self.session.execute(
            select(UserRestriction)
            .where(
                UserRestriction.group_id == group_id,
                UserRestriction.user_telegram_id == user_telegram_id,
            )
        )
        return result.scalar_one_or_none()

    async def is_restricted(
        self, group_id: int, user_telegram_id: int
    ) -> bool:
        """Check if user is restricted by bot in this group."""
        record = await self.get_record(group_id, user_telegram_id)
        return record is not None and record.restricted_by_bot

    async def add_restriction(
        self,
        group_id: int,
        user_telegram_id: int,
        original_permissions: dict | None = None,
    ) -> UserRestriction:
        """Record that bot restricted user. Stores original perms for restore."""
        existing = await self.get_record(group_id, user_telegram_id)
        if existing:
            return existing  # Already restricted

        perms_json = (
            json.dumps(original_permissions, ensure_ascii=False)
            if original_permissions
            else None
        )
        return await self.create(
            group_id=group_id,
            user_telegram_id=user_telegram_id,
            restricted_by_bot=True,
            original_permissions_json=perms_json,
        )

    async def remove_restriction(
        self, group_id: int, user_telegram_id: int
    ) -> dict | None:
        """
        Remove restriction record.
        Returns original_permissions dict for restore, or None.
        """
        record = await self.get_record(group_id, user_telegram_id)
        if record is None:
            return None

        original = None
        if record.original_permissions_json:
            try:
                original = json.loads(record.original_permissions_json)
            except (json.JSONDecodeError, TypeError):
                pass

        await self.session.execute(
            delete(UserRestriction)
            .where(UserRestriction.id == record.id)
        )
        return original

    async def get_by_group(self, group_id: int) -> list[UserRestriction]:
        """Get all restrictions for a group."""
        result = await self.session.execute(
            select(UserRestriction)
            .where(UserRestriction.group_id == group_id)
        )
        return list(result.scalars().all())

    async def get_active_by_group(self, group_id: int) -> list[UserRestriction]:
        """Get active (bot-applied) restrictions for a group."""
        result = await self.session.execute(
            select(UserRestriction)
            .where(
                UserRestriction.group_id == group_id,
                UserRestriction.restricted_by_bot == True,
            )
        )
        return list(result.scalars().all())
