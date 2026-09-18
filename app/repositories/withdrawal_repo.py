"""Withdrawal repository."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, update, func

from app.db.models import Withdrawal
from app.repositories.base import BaseRepository
from app.utils.time_utils import utc_now
from datetime import timedelta


# Allowed withdrawal state transitions
WITHDRAWAL_TRANSITIONS = {
    "pending": ["approved", "rejected"],
    "approved": ["processing", "rejected"],
    "processing": ["completed", "error"],
    "completed": [],       # terminal
    "rejected": [],        # terminal
    "error": ["processing"],  # can retry
}


class WithdrawalRepository(BaseRepository[Withdrawal]):
    model = Withdrawal

    async def get_by_user(
        self, user_id: int, offset: int = 0, limit: int = 20
    ) -> Sequence[Withdrawal]:
        """Get user's withdrawals paginated."""
        result = await self.session.execute(
            select(Withdrawal)
            .where(Withdrawal.user_id == user_id)
            .order_by(Withdrawal.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def get_pending(self) -> Sequence[Withdrawal]:
        """Get all pending withdrawals for admin review."""
        result = await self.session.execute(
            select(Withdrawal)
            .where(Withdrawal.status == "pending")
            .order_by(Withdrawal.created_at)
        )
        return result.scalars().all()

    async def get_by_status(self, status: str) -> Sequence[Withdrawal]:
        """Get withdrawals by status."""
        result = await self.session.execute(
            select(Withdrawal)
            .where(Withdrawal.status == status)
            .order_by(Withdrawal.created_at.desc())
        )
        return result.scalars().all()

    async def has_pending(self, user_id: int) -> bool:
        """Check if user has a pending withdrawal (prevent double-submit)."""
        result = await self.session.execute(
            select(func.count(Withdrawal.id))
            .where(
                Withdrawal.user_id == user_id,
                Withdrawal.status.in_(["pending", "approved", "processing"]),
            )
        )
        return result.scalar_one() > 0

    async def transition_status(
        self, withdrawal_id: int, new_status: str, **extra_fields
    ) -> int:
        """
        Conditional status transition.
        Returns rowcount (0 if transition not allowed or race).
        """
        allowed_from = [
            s for s, targets in WITHDRAWAL_TRANSITIONS.items()
            if new_status in targets
        ]
        if not allowed_from:
            return 0

        values = {"status": new_status, **extra_fields}
        if new_status == "approved":
            values["approved_at"] = utc_now()
        elif new_status == "completed":
            values["completed_at"] = utc_now()

        result = await self.session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.status.in_(allowed_from),
            )
            .values(**values)
        )
        return result.rowcount

    async def count_pending(self) -> int:
        """Count pending withdrawals (for admin dashboard)."""
        result = await self.session.execute(
            select(func.count(Withdrawal.id))
            .where(Withdrawal.status == "pending")
        )
        return result.scalar_one()

    async def claim_for_processing(self, worker_token: str, lease_minutes: int = 5) -> Sequence[Withdrawal]:
        """
        Claim unassigned or expired-lease withdrawals for processing.
        MUST be called inside run_atomic.
        """
        now = utc_now()
        lease_expiry = now + timedelta(minutes=lease_minutes)
        
        result = await self.session.execute(
            select(Withdrawal)
            .where(
                Withdrawal.status.in_(["approved", "processing"]),
                (Withdrawal.lease_expires_at == None) | (Withdrawal.lease_expires_at <= now)
            )
        )
        withdrawals = result.scalars().all()
        for w in withdrawals:
            w.worker_token = worker_token
            w.lease_expires_at = lease_expiry
            w.status = "processing"
            w.updated_at = now
            
        return withdrawals
