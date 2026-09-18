"""Balance ledger repository — financial audit trail."""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import select, func

from app.db.models import BalanceLedger
from app.repositories.base import BaseRepository


class LedgerRepository(BaseRepository[BalanceLedger]):
    model = BalanceLedger

    async def get_by_user(
        self, user_id: int, offset: int = 0, limit: int = 50
    ) -> Sequence[BalanceLedger]:
        """Get user's ledger entries paginated (newest first)."""
        result = await self.session.execute(
            select(BalanceLedger)
            .where(BalanceLedger.user_id == user_id)
            .order_by(BalanceLedger.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()

    async def get_by_reference(
        self, reference_type: str, reference_id: int
    ) -> Sequence[BalanceLedger]:
        """Get ledger entries for a specific entity (campaign, withdrawal, etc.)."""
        result = await self.session.execute(
            select(BalanceLedger)
            .where(
                BalanceLedger.reference_type == reference_type,
                BalanceLedger.reference_id == reference_id,
            )
            .order_by(BalanceLedger.created_at)
        )
        return result.scalars().all()

    async def idempotency_check(self, key: str) -> bool:
        """Check if a ledger entry with this idempotency key already exists."""
        result = await self.session.execute(
            select(func.count(BalanceLedger.id))
            .where(BalanceLedger.idempotency_key == key)
        )
        return result.scalar_one() > 0

    async def record(
        self,
        user_id: int,
        operation_type: str,
        amount: str,
        direction: str,
        balance_before: str,
        balance_after: str,
        available_before: str,
        available_after: str,
        reserved_before: str,
        reserved_after: str,
        reference_type: str | None = None,
        reference_id: int | None = None,
        idempotency_key: str | None = None,
        reason: str | None = None,
    ) -> BalanceLedger:
        """
        Record a financial ledger entry.
        This is the ONLY way to create ledger entries.
        """
        # Idempotency guard
        if idempotency_key:
            exists = await self.idempotency_check(idempotency_key)
            if exists:
                # Return existing entry
                result = await self.session.execute(
                    select(BalanceLedger)
                    .where(BalanceLedger.idempotency_key == idempotency_key)
                )
                return result.scalar_one()

        return await self.create(
            user_id=user_id,
            operation_type=operation_type,
            amount=amount,
            direction=direction,
            balance_before=balance_before,
            balance_after=balance_after,
            available_before=available_before,
            available_after=available_after,
            reserved_before=reserved_before,
            reserved_after=reserved_after,
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    async def count_by_user(self, user_id: int) -> int:
        """Count total ledger entries for a user."""
        result = await self.session.execute(
            select(func.count(BalanceLedger.id))
            .where(BalanceLedger.user_id == user_id)
        )
        return result.scalar_one()

    async def get_by_type(
        self, operation_type: str, offset: int = 0, limit: int = 50
    ) -> Sequence[BalanceLedger]:
        """Get ledger entries by operation type (admin analytics)."""
        result = await self.session.execute(
            select(BalanceLedger)
            .where(BalanceLedger.operation_type == operation_type)
            .order_by(BalanceLedger.created_at.desc())
            .offset(offset).limit(limit)
        )
        return result.scalars().all()
