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

        withdrawal = Withdrawal(
            user_id=user_id,
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

    async def process_payout(
        self,
        withdrawal_id: int,
        user_telegram_id: int,
    ) -> dict:
        """
        Process approved withdrawal via Crypto Pay transfer.

        Plan v4 spend_id lifecycle:
        1. Generate spend_id = f"w{withdrawal_id}"
        2. Check if transfer already exists (reconciliation before retry)
        3. If not: create transfer
        4. Update withdrawal + payout records
        """
        w = await self.repo.get_by_id(withdrawal_id)
        if not w or w.status not in ("approved", "processing"):
            return {"ok": False, "error": "Invalid withdrawal status"}

        if not self.crypto_pay:
            return {"ok": False, "error": "Crypto Pay not configured"}

        # Mark as processing
        w.status = "processing"
        w.updated_at = utc_now()
        await self.session.flush()

        amount = from_db(w.amount)
        spend_id = f"w{withdrawal_id}"
        w.spend_id = spend_id

        # 1. Reconciliation: check if transfer already exists
        existing = await self.crypto_pay.find_transfer_by_spend_id(spend_id)
        if existing:
            # Transfer already succeeded
            return await self._complete_payout(w, existing.transfer_id, spend_id, amount)

        # 2. Create payout record
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
        await self.session.flush()

        # 3. Execute transfer
        try:
            transfer = await self.crypto_pay.transfer(
                user_id=user_telegram_id,
                asset=w.asset,
                amount=str(amount),
                spend_id=spend_id,
                comment=f"Вывод #{withdrawal_id}",
            )
            return await self._complete_payout(
                w, transfer.transfer_id, spend_id, amount, payout
            )

        except CryptoPayNetworkError:
            # AMBIGUOUS — transfer may have succeeded
            # Mark as processing and let reconciliation handle it
            payout.status = "pending"
            payout.error_message = "Network error — awaiting reconciliation"
            logger.warning(
                "Withdrawal %d transfer ambiguous (network error)",
                withdrawal_id,
            )
            return {"ok": False, "error": "network_ambiguous", "needs_reconciliation": True}

        except CryptoPayError as e:
            # Definite failure
            payout.status = "failed"
            payout.error_message = str(e)
            w.status = "error"
            w.error_message = str(e)
            w.updated_at = utc_now()

            logger.error("Withdrawal %d transfer failed: %s", withdrawal_id, e)
            return {"ok": False, "error": str(e)}

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
        if not w or w.status != "error":
            return False

        amount = from_db(w.amount)
        await self.balance_service.release_withdrawal(
            user_id=w.user_id,
            amount=amount,
            withdrawal_id=withdrawal_id,
            reason="Ошибка вывода — средства возвращены",
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
