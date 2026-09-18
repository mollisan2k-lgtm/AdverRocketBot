"""
Withdrawal service — request, approve, process, reconcile.

Plan v4 requirements:
- spend_id for idempotent transfers
- Reconciliation before retry (check getTransfers first)
- Reserve on request, release on reject/failure
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Withdrawal, Payout
from app.repositories.withdrawal_repo import WithdrawalRepository
from app.repositories.payout_repo import PayoutRepository
from app.repositories.settings_repo import SettingsRepository
from app.services.balance_service import BalanceService
from app.integrations.crypto_pay import (
    CryptoPayService, CryptoPayError, CryptoPayNetworkError,
)
from app.utils.decimal_utils import to_db, from_db, round_down, ZERO, is_valid_amount
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class WithdrawalService:
    """Manages withdrawal lifecycle with Crypto Pay payout."""

    def __init__(
        self,
        session: AsyncSession,
        crypto_pay: CryptoPayService | None = None,
    ):
        self.session = session
        self.repo = WithdrawalRepository(session)
        self.payout_repo = PayoutRepository(session)
        self.balance_service = BalanceService(session)
        self.crypto_pay = crypto_pay

    async def request_withdrawal(
        self,
        user_id: int,
        amount: Decimal,
        recipient_telegram_id: int,
        asset: str = "USDT",
    ) -> Withdrawal:
        """
        Create withdrawal request.
        Immediately reserves funds (available -> reserved).
        """
        amount = round_down(amount)
        if not is_valid_amount(amount):
            raise ValueError("Сумма вывода должна быть положительной.")

        settings_repo = SettingsRepository(self.session)
        min_w_str = await settings_repo.get_value("min_withdrawal")
        max_w_str = await settings_repo.get_value("max_withdrawal")
        
        min_w = Decimal(min_w_str) if min_w_str else Decimal("5.00")
        max_w = Decimal(max_w_str) if max_w_str else Decimal("1000.00")

        if amount < min_w:
            raise ValueError(f"Минимальная сумма вывода: {min_w} USDT")
        if amount > max_w:
            raise ValueError(f"Максимальная сумма вывода: {max_w} USDT")

        # Check for existing live withdrawal
        has_pending = await self.repo.has_pending(user_id=user_id)
        if has_pending:
            raise ValueError("У вас уже есть активная заявка на вывод. Дождитесь её обработки.")

        withdrawal = Withdrawal(
            user_id=user_id,
            recipient_telegram_id=recipient_telegram_id,
            amount=to_db(amount),
            asset=asset,
            status="pending",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.session.add(withdrawal)
        await self.session.flush()

        # Reserve funds
        await self.balance_service.reserve_for_withdrawal(
            user_id=user_id,
            amount=amount,
            withdrawal_id=withdrawal.id,
            idempotency_key=f"withdrawal_reserve:{withdrawal.id}",
        )

        logger.info("Withdrawal requested: id=%d user=%d amount=%s", withdrawal.id, user_id, amount)
        return withdrawal

    async def approve(self, withdrawal_id: int) -> bool:
        """Admin approves withdrawal -> status 'approved'."""
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.status != "pending":
            return False
        w.status = "approved"
        w.approved_at = utc_now()
        w.updated_at = utc_now()
        return True

    async def reject(
        self, withdrawal_id: int, reason: str
    ) -> bool:
        """
        Admin rejects withdrawal.
        Release reserved funds back to available.
        """
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.status != "pending":
            return False

        w.status = "rejected"
        w.rejection_reason = reason
        w.updated_at = utc_now()

        amount = from_db(w.amount)
        await self.balance_service.release_withdrawal(
            user_id=w.user_id,
            amount=amount,
            withdrawal_id=withdrawal_id,
            reason=f"Отклонено: {reason}",
            idempotency_key=f"withdrawal_release:{withdrawal_id}",
        )
        return True

    async def prepare_payout(
        self,
        withdrawal_id: int,
        worker_id: str,
        claim_token: str,
        expected_gen: int,
    ) -> dict | None:
        """Phase 1: Prepare DB for payout."""
        from sqlalchemy import update
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.status not in ("approved", "processing"):
            return None
            
        if w.claim_token != claim_token or w.generation != expected_gen:
            return None

        spend_id = f"w{withdrawal_id}"
        amount = from_db(w.amount)

        res = await self.session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.claim_token == claim_token,
                Withdrawal.generation == expected_gen
            )
            .values(
                status="processing",
                spend_id=spend_id,
                updated_at=utc_now(),
                generation=Withdrawal.generation + 1
            )
        )
        if res.rowcount == 0:
            return None

        w.status = "processing"
        w.spend_id = spend_id

        # Create payout record if not exists
        payout = await self.payout_repo.get_by_withdrawal(withdrawal_id)
        if not payout:
            payout = Payout(
                withdrawal_id=withdrawal_id,
                user_id=w.user_id,
                spend_id=spend_id,
                amount=to_db(amount),
                asset=w.asset,
                status="pending",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            self.session.add(payout)

        return {"ok": True, "spend_id": spend_id, "amount": amount}

    async def finalize_payout_success(
        self, withdrawal_id: int, claim_token: str, expected_gen: int, transfer_id: int
    ) -> bool:
        """Phase 3: Finalize on HTTP success."""
        from sqlalchemy import update
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.claim_token != claim_token or w.status != "processing" or w.generation != expected_gen:
            return False
            
        res = await self.session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.claim_token == claim_token,
                Withdrawal.generation == expected_gen
            )
            .values(generation=Withdrawal.generation + 1)
        )
        if res.rowcount == 0:
            return False

        amount = from_db(w.amount)
        payout = await self.payout_repo.get_by_withdrawal(withdrawal_id)
        return await self._complete_payout(w, transfer_id, w.spend_id, amount, payout)

    async def mark_payout_ambiguous(self, withdrawal_id: int, claim_token: str, expected_gen: int) -> bool:
        """Phase 3: Finalize on Network Error -> requires reconciliation."""
        from sqlalchemy import update
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.claim_token != claim_token or w.status != "processing" or w.generation != expected_gen:
            return False
            
        res = await self.session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.claim_token == claim_token,
                Withdrawal.generation == expected_gen
            )
            .values(
                status="reconciliation_required",
                updated_at=utc_now(),
                generation=Withdrawal.generation + 1
            )
        )
        if res.rowcount == 0:
            return False
            
        payout = await self.payout_repo.get_by_withdrawal(withdrawal_id)
        if payout:
            payout.status = "pending"
            payout.error_message = "Network error - awaiting reconciliation"
            payout.updated_at = utc_now()
        return True

    async def finalize_payout_failure(self, withdrawal_id: int, claim_token: str, expected_gen: int, error: str) -> bool:
        """Phase 3: Finalize on HTTP failure."""
        from sqlalchemy import update
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.claim_token != claim_token or w.status != "processing" or w.generation != expected_gen:
            return False
            
        res = await self.session.execute(
            update(Withdrawal)
            .where(
                Withdrawal.id == withdrawal_id,
                Withdrawal.claim_token == claim_token,
                Withdrawal.generation == expected_gen
            )
            .values(
                status="failed",
                error_message=error,
                updated_at=utc_now(),
                generation=Withdrawal.generation + 1
            )
        )
        if res.rowcount == 0:
            return False
            
        payout = await self.payout_repo.get_by_withdrawal(withdrawal_id)
        if payout:
            payout.status = "failed"
            payout.error_message = error
            payout.updated_at = utc_now()
        
        amount = from_db(w.amount)
        await self.balance_service.release_withdrawal(
            user_id=w.user_id,
            amount=amount,
            withdrawal_id=withdrawal_id,
            reason="Ошибка вывода — средства возвращены",
            idempotency_key=f"withdrawal_error_release:{withdrawal_id}",
        )
        return True

    async def _complete_payout(
        self,
        withdrawal: Withdrawal,
        transfer_id: int,
        spend_id: str,
        amount: Decimal,
        payout: Payout | None = None,
    ) -> dict:
        """Finalize successful payout."""
        withdrawal.transfer_id = transfer_id
        withdrawal.status = "completed"
        withdrawal.completed_at = utc_now()
        withdrawal.updated_at = utc_now()

        if payout:
            payout.transfer_id = transfer_id
            payout.status = "completed"
            payout.updated_at = utc_now()

        # Deduct from reserved balance
        await self.balance_service.complete_withdrawal(
            user_id=withdrawal.user_id,
            amount=amount,
            withdrawal_id=withdrawal.id,
            idempotency_key=f"withdrawal_payout:{withdrawal.id}",
        )

        logger.info(
            "Withdrawal %d completed: transfer_id=%d amount=%s",
            withdrawal.id, transfer_id, amount,
        )
        return {"ok": True, "transfer_id": transfer_id}

    async def handle_failed_payout(self, withdrawal_id: int) -> bool:
        """
        Release funds after a definite payout failure.
        Called after reconciliation confirms no transfer exists.
        """
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.status != "failed":
            return False

        amount = from_db(w.amount)
        await self.balance_service.release_withdrawal(
            user_id=w.user_id,
            amount=amount,
            withdrawal_id=withdrawal_id,
            reason="Ошибка вывода — средства возвращены (реконсиляция)",
            idempotency_key=f"withdrawal_error_release:{withdrawal_id}",
        )
        return True

    # ── Queries ──────────────────────────────────────────────────────────

    async def get_by_id(self, withdrawal_id: int) -> Withdrawal | None:
        return await self.repo.get_by_id(withdrawal_id)

    async def get_pending(self) -> list[Withdrawal]:
        return await self.repo.get_by_status("pending")

    async def get_approved(self) -> list[Withdrawal]:
        return await self.repo.get_by_status("approved")

    async def get_processing(self) -> list[Withdrawal]:
        return await self.repo.get_by_status("processing")

    async def get_user_withdrawals(self, user_id: int) -> list[Withdrawal]:
        return await self.repo.get_by_user(user_id)
