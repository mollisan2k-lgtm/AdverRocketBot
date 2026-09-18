"""Deposit (Crypto Pay invoice) repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, update, func

from app.db.models import Deposit
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now


class DepositRepository(BaseRepository[Deposit]):
    model = Deposit

    async def get_by_invoice_id(self, invoice_id: int) -> Deposit | None:
        """Find deposit by Crypto Pay invoice ID."""
        result = await self.session.execute(
            select(Deposit).where(Deposit.invoice_id == invoice_id)
        )
        return result.scalar_one_or_none()

    async def get_by_user(
        self, user_id: int, offset: int = 0, limit: int = 20
    ) -> Sequence[Deposit]:
        """Get user's deposits paginated."""
        result = await self.session.execute(
            select(Deposit)
            .where(Deposit.user_id == user_id)
            .order_by(Deposit.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def get_pending_by_user(self, user_id: int) -> Sequence[Deposit]:
        """Get user's pending (unpaid) deposits."""
        result = await self.session.execute(
            select(Deposit)
            .where(Deposit.user_id == user_id, Deposit.status == "pending")
            .order_by(Deposit.created_at.desc())
        )
        return result.scalars().all()

    async def mark_paid(
        self,
        invoice_id: int,
        external_status: str | None = None,
        external_data_json: str | None = None,
    ) -> int:
        """
        Mark deposit as paid by invoice_id.
        Conditional UPDATE: only if status is 'pending'.
        Returns rowcount (0 = already processed or not found).
        """
        result = await self.session.execute(
            update(Deposit)
            .where(
                Deposit.invoice_id == invoice_id,
                Deposit.status == "pending",
            )
            .values(
                status="paid",
                paid_at=utc_now(),
                external_status=external_status,
                external_data_json=external_data_json,
            )
        )
        return result.rowcount

    async def expire_overdue(self) -> int:
        """Bulk-expire pending deposits past their expiry. Returns count."""
        now = utc_now()
        result = await self.session.execute(
            update(Deposit)
            .where(
                Deposit.status == "pending",
                Deposit.expires_at.isnot(None),
                Deposit.expires_at <= now,
            )
            .values(status="expired")
        )
        return result.rowcount

    async def count_by_user(self, user_id: int) -> int:
        """Count total deposits for a user."""
        result = await self.session.execute(
            select(func.count(Deposit.id))
            .where(Deposit.user_id == user_id)
        )
        return result.scalar_one()

    async def sum_paid_by_user(self, user_id: int) -> str:
        """Sum of all paid deposits for a user (as TEXT)."""
        result = await self.session.execute(
            select(func.coalesce(func.sum(func.cast(Deposit.amount, func.text())), "0.00"))
            .where(Deposit.user_id == user_id, Deposit.status == "paid")
        )
        return result.scalar_one()
