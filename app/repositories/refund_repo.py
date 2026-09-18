"""Refund repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, func

from app.db.models import Refund
from app.repositories.base import BaseRepository


class RefundRepository(BaseRepository[Refund]):
    model = Refund

    async def get_by_campaign(self, campaign_id: int) -> Sequence[Refund]:
        """Get all refunds for a campaign."""
        result = await self.session.execute(
            select(Refund)
            .where(Refund.campaign_id == campaign_id)
            .order_by(Refund.created_at.desc())
        )
        return result.scalars().all()

    async def get_by_user(
        self, user_id: int, offset: int = 0, limit: int = 20
    ) -> Sequence[Refund]:
        """Get user's refunds paginated."""
        result = await self.session.execute(
            select(Refund)
            .where(Refund.user_id == user_id)
            .order_by(Refund.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def idempotency_check(self, key: str) -> bool:
        """Check if refund with this idempotency key already exists."""
        result = await self.session.execute(
            select(func.count(Refund.id))
            .where(Refund.idempotency_key == key)
        )
        return result.scalar_one() > 0

    async def create_refund(
        self,
        user_id: int,
        campaign_id: int,
        refund_type: str,
        gross_amount: str,
        commission_percent: str,
        commission_amount: str,
        net_amount: str,
        idempotency_key: str | None = None,
    ) -> Refund | None:
        """
        Create a refund entry with idempotency guard.
        Returns None if idempotency key already used.
        """
        if idempotency_key:
            exists = await self.idempotency_check(idempotency_key)
            if exists:
                return None

        return await self.create(
            user_id=user_id,
            campaign_id=campaign_id,
            refund_type=refund_type,
            gross_amount=gross_amount,
            commission_percent=commission_percent,
            commission_amount=commission_amount,
            net_amount=net_amount,
            idempotency_key=idempotency_key,
        )

    async def sum_refunded_by_campaign(self, campaign_id: int) -> int:
        """Count total refunds for a campaign."""
        result = await self.session.execute(
            select(func.count(Refund.id))
            .where(Refund.campaign_id == campaign_id)
        )
        return result.scalar_one()
