"""
Category service — manage subscription categories and pricing.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category
from app.repositories.category_repo import CategoryRepository
from app.utils.decimal_utils import to_db, from_db, round_down
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class CategoryService:
    """Manage categories with buyer/seller pricing."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = CategoryRepository(session)

    async def create(
        self,
        name: str,
        buyer_price: Decimal,
        seller_payout: Decimal,
    ) -> Category:
        """Create a new category."""
        buyer_price = round_down(buyer_price)
        seller_payout = round_down(seller_payout)

        if seller_payout > buyer_price:
            raise ValueError("Выплата продавцу не может превышать цену покупателя.")
        if buyer_price <= Decimal("0"):
            raise ValueError("Цена должна быть положительной.")

        category = Category(
            name=name,
            buyer_price=to_db(buyer_price),
            seller_payout=to_db(seller_payout),
            status="active",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.session.add(category)
        await self.session.flush()
        logger.info("Category created: id=%d name=%s", category.id, name)
        return category

    async def get_by_id(self, category_id: int) -> Category | None:
        return await self.repo.get_by_id(category_id)

    async def get_active(self) -> list[Category]:
        """Get all active categories."""
        return await self.repo.get_by_status("active")

    async def get_all(self) -> list[Category]:
        """Get all categories (admin)."""
        return await self.repo.get_all()

    async def update_price(
        self,
        category_id: int,
        buyer_price: Decimal,
        seller_payout: Decimal,
    ) -> Category:
        """Update category pricing. Does NOT affect existing campaigns (snapshots)."""
        category = await self.repo.get_by_id(category_id)
        if not category:
            raise ValueError("Категория не найдена.")

        buyer_price = round_down(buyer_price)
        seller_payout = round_down(seller_payout)

        if seller_payout > buyer_price:
            raise ValueError("Выплата продавцу не может превышать цену покупателя.")

        category.buyer_price = to_db(buyer_price)
        category.seller_payout = to_db(seller_payout)
        category.updated_at = utc_now()
        return category

    async def set_status(self, category_id: int, status: str) -> Category:
        """Change category status (active, disabled, archived)."""
        allowed = {"active", "disabled", "archived"}
        if status not in allowed:
            raise ValueError(f"Invalid status: {status}")

        category = await self.repo.get_by_id(category_id)
        if not category:
            raise ValueError("Категория не найдена.")

        category.status = status
        category.updated_at = utc_now()
        return category
