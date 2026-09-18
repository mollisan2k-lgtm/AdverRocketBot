"""Category repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from app.db.models import Category
from app.repositories.base import BaseRepository


class CategoryRepository(BaseRepository[Category]):
    model = Category

    async def get_active(self) -> Sequence[Category]:
        """Get all active categories."""
        result = await self.session.execute(
            select(Category).where(Category.status == "active")
            .order_by(Category.name)
        )
        return result.scalars().all()

    async def get_by_status(self, status: str) -> Sequence[Category]:
        """Get categories by status."""
        result = await self.session.execute(
            select(Category).where(Category.status == status)
            .order_by(Category.name)
        )
        return result.scalars().all()

    async def set_status(self, category_id: int, status: str) -> int:
        """Update category status."""
        return await self.update_by_id(category_id, status=status)

    async def has_active_campaigns(self, category_id: int) -> bool:
        """Check if category has active campaigns."""
        from app.db.models import Campaign
        from sqlalchemy import func
        result = await self.session.execute(
            select(func.count(Campaign.id))
            .where(
                Campaign.category_id == category_id,
                Campaign.status.in_(["active", "paused"]),
            )
        )
        return result.scalar_one() > 0

    async def has_active_groups(self, category_id: int) -> bool:
        """Check if category has active seller groups."""
        from app.db.models import SellerGroup
        from sqlalchemy import func
        result = await self.session.execute(
            select(func.count(SellerGroup.id))
            .where(
                SellerGroup.category_id == category_id,
                SellerGroup.status == "approved",
            )
        )
        return result.scalar_one() > 0
