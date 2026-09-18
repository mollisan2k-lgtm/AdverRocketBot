"""Payout repository (Crypto Pay transfer records)."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, update

from app.db.models import Payout
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now


class PayoutRepository(BaseRepository[Payout]):
    model = Payout

    async def get_by_spend_id(self, spend_id: str) -> Payout | None:
        """Find payout by idempotent spend_id."""
        result = await self.session.execute(
            select(Payout).where(Payout.spend_id == spend_id)
        )
        return result.scalar_one_or_none()

    async def get_by_withdrawal(self, withdrawal_id: int) -> Sequence[Payout]:
        """Get all payouts for a withdrawal."""
        result = await self.session.execute(
            select(Payout)
            .where(Payout.withdrawal_id == withdrawal_id)
            .order_by(Payout.created_at)
        )
        return result.scalars().all()

    async def mark_completed(
        self,
        spend_id: str,
        transfer_id: int | None = None,
        external_data_json: str | None = None,
    ) -> int:
        """Mark payout as completed. Returns rowcount."""
        result = await self.session.execute(
            update(Payout)
            .where(Payout.spend_id == spend_id, Payout.status == "pending")
            .values(
                status="completed",
                transfer_id=transfer_id,
                external_data_json=external_data_json,
            )
        )
        return result.rowcount

    async def mark_failed(
        self, spend_id: str, error_message: str
    ) -> int:
        """Mark payout as failed. Returns rowcount."""
        result = await self.session.execute(
            update(Payout)
            .where(Payout.spend_id == spend_id, Payout.status == "pending")
            .values(status="failed", error_message=error_message)
        )
        return result.rowcount
