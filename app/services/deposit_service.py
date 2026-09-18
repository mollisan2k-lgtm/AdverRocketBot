"""
Deposit service — create invoices, process payments.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Deposit
from app.repositories.deposit_repo import DepositRepository
from app.repositories.settings_repo import SettingsRepository
from app.services.balance_service import BalanceService
from app.integrations.crypto_pay import CryptoPayService, InvoiceData
from app.utils.decimal_utils import to_db, from_db, round_down, ZERO, is_valid_amount
from app.utils.time_utils import utc_now

logger = logging.getLogger(__name__)


class DepositService:
    """Manages deposit (invoice) lifecycle."""

    def __init__(
        self,
        session: AsyncSession,
        crypto_pay: CryptoPayService | None = None,
    ):
        self.session = session
        self.repo = DepositRepository(session)
        self.balance_service = BalanceService(session)
        self.crypto_pay = crypto_pay

    async def create_deposit(
        self,
        user_id: int,
        amount: Decimal,
        asset: str = "USDT",
    ) -> Deposit:
        """
        Create a deposit record and a Crypto Pay invoice.

        Steps:
        1. Validate amount
        2. Create DB row with status=pending
        3. Call Crypto Pay createInvoice
        4. Save invoice_id and pay_url
        """
        amount = round_down(amount)
        if not is_valid_amount(amount):
            raise ValueError("Сумма пополнения должна быть положительной.")

        settings_repo = SettingsRepository(self.session)
        min_dep_str = await settings_repo.get_value("min_deposit")
        min_dep = Decimal(min_dep_str) if min_dep_str else Decimal("1.00")
        
        if amount < min_dep:
            raise ValueError(f"Минимальная сумма пополнения: {min_dep} USDT")

        deposit = Deposit(
            user_id=user_id,
            amount=to_db(amount),
            asset=asset,
            status="pending",
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.session.add(deposit)
        await self.session.flush()

        # Create Crypto Pay invoice
        if self.crypto_pay:
            try:
                invoice = await self.crypto_pay.create_invoice(
                    amount=str(amount),
                    asset=asset,
                    description=f"Пополнение баланса #{deposit.id}",
                    payload=f"deposit:{deposit.id}",
                    expires_in=3600,  # 1 hour
                )
                deposit.invoice_id = invoice.invoice_id
                deposit.pay_url = invoice.pay_url
            except Exception as e:
                logger.error("Failed to create invoice for deposit %d: %s", deposit.id, e)
                deposit.status = "cancelled"
                deposit.error_message = str(e) if hasattr(deposit, 'error_message') else None
                raise

        logger.info("Deposit created: id=%d user=%d amount=%s", deposit.id, user_id, amount)
        return deposit

    async def confirm_payment(
        self, deposit_id: int, invoice_data: InvoiceData | None = None
    ) -> bool:
        """
        Confirm a deposit payment (called by invoice checker or webhook).

        Idempotent: if deposit is already 'paid', returns True without double-credit.
        """
        deposit = await self.repo.get_by_id(deposit_id)
        if not deposit:
            return False

        if deposit.status == "paid":
            return True  # Already processed

        if deposit.status != "pending":
            return False

        # Update deposit
        deposit.status = "paid"
        deposit.paid_at = utc_now()
        deposit.updated_at = utc_now()
        if invoice_data:
            deposit.external_status = invoice_data.status
            import json
            deposit.external_data_json = json.dumps({
                "invoice_id": invoice_data.invoice_id,
                "paid_at": invoice_data.paid_at,
                "amount": invoice_data.amount,
            })

        # Credit balance
        amount = from_db(deposit.amount)
        await self.balance_service.credit_deposit(
            user_id=deposit.user_id,
            amount=amount,
            deposit_id=deposit.id,
            idempotency_key=f"deposit:{deposit.id}",
        )

        logger.info("Deposit confirmed: id=%d amount=%s", deposit_id, amount)
        return True

    async def mark_expired(self, deposit_id: int) -> bool:
        """Mark a pending deposit as expired."""
        deposit = await self.repo.get_by_id(deposit_id)
        if not deposit or deposit.status != "pending":
            return False
        deposit.status = "expired"
        deposit.updated_at = utc_now()
        return True

    async def get_by_id(self, deposit_id: int) -> Deposit | None:
        return await self.repo.get_by_id(deposit_id)

    async def get_by_invoice_id(self, invoice_id: int) -> Deposit | None:
        return await self.repo.get_by_invoice_id(invoice_id)

    async def get_pending_deposits(self) -> list[Deposit]:
        """Get all pending deposits (for invoice checker)."""
        from sqlalchemy import select
        result = await self.session.execute(
            select(Deposit)
            .where(Deposit.status == "pending")
            .order_by(Deposit.created_at)
        )
        return list(result.scalars().all())

    async def get_user_deposits(self, user_id: int) -> list[Deposit]:
        return await self.repo.get_by_user(user_id)
