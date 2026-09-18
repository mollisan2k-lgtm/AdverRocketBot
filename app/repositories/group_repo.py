"""Seller group repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, func

from app.db.models import SellerGroup
from app.repositories.base import BaseRepository


class GroupRepository(BaseRepository[SellerGroup]):
    model = SellerGroup

    async def get_by_chat_id(self, chat_id: int) -> SellerGroup | None:
        """Find group by Telegram chat ID."""
        result = await self.session.execute(
            select(SellerGroup).where(SellerGroup.telegram_chat_id == chat_id)
        )
        return result.scalar_one_or_none()

    async def get_by_deep_link(self, code: str) -> SellerGroup | None:
        """Find group by deep link code."""
        result = await self.session.execute(
            select(SellerGroup).where(SellerGroup.deep_link_code == code)
        )
        return result.scalar_one_or_none()

    async def get_by_user(self, user_id: int) -> Sequence[SellerGroup]:
        """Get all groups belonging to a seller."""
        result = await self.session.execute(
            select(SellerGroup)
            .where(SellerGroup.user_id == user_id)
            .order_by(SellerGroup.created_at.desc())
        )
        return result.scalars().all()

    async def get_approved_by_category(self, category_id: int) -> Sequence[SellerGroup]:
        """Get approved groups for a category (for task distribution)."""
        result = await self.session.execute(
            select(SellerGroup)
            .where(
                SellerGroup.category_id == category_id,
                SellerGroup.status == "approved",
                SellerGroup.bot_has_rights == True,
            )
            .order_by(SellerGroup.created_at)
        )
        return result.scalars().all()

    async def get_pending(self) -> Sequence[SellerGroup]:
        """Get groups awaiting moderation."""
        result = await self.session.execute(
            select(SellerGroup)
            .where(SellerGroup.status == "pending")
            .order_by(SellerGroup.created_at)
        )
        return result.scalars().all()

    async def get_approved(self) -> Sequence[SellerGroup]:
        """Get all approved groups (for rights recheck)."""
        result = await self.session.execute(
            select(SellerGroup)
            .where(SellerGroup.status == "approved")
            .order_by(SellerGroup.created_at)
        )
        return result.scalars().all()

    async def get_disabled_for_recheck(self) -> Sequence[SellerGroup]:
        """Get disabled groups to recheck rights (every 15 min)."""
        result = await self.session.execute(
            select(SellerGroup)
            .where(
                SellerGroup.status == "approved",
                SellerGroup.bot_has_rights == False,
            )
        )
        return result.scalars().all()

    async def count_by_user(self, user_id: int) -> int:
        """Count seller's groups."""
        result = await self.session.execute(
            select(func.count(SellerGroup.id))
            .where(SellerGroup.user_id == user_id)
        )
        return result.scalar_one()

    async def get_active_members(self, group_id: int) -> list[int]:
        """Get Telegram IDs of active members in a group."""
        from app.db.models import SellerGroupMember
        result = await self.session.execute(
            select(SellerGroupMember.user_telegram_id)
            .where(
                SellerGroupMember.group_id == group_id,
                SellerGroupMember.is_member == True,
                SellerGroupMember.status == "member"
            )
        )
        return list(result.scalars().all())
