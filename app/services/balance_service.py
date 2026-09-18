"""
Balance service — all financial operations.

Every money movement follows the same pattern:
1. Lock user row (FOR UPDATE)
2. Validate current balance
3. Calculate new balance
4. Update user balance atomically
5. Record ledger entry with idempotency key

NEVER call user.available / user.reserved directly —
always go through this service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_repo import UserRepository
from app.repositories.ledger_repo import LedgerRepository
from app.utils.decimal_utils import (
    to_decimal, round_down, to_db, from_db,
    ZERO, is_valid_amount,
)

logger = logging.getLogger(__name__)


class InsufficientFundsError(Exception):
    """Not enough available balance."""
    pass


class InsufficientReserveError(Exception):
    """Not enough reserved balance."""
    pass


class DuplicateOperationError(Exception):
    """Idempotency key already used."""
    pass


@dataclass(frozen=True)
class BalanceSnapshot:
    """Result of a balance operation."""
    available: Decimal
    reserved: Decimal
    amount: Decimal
    operation_type: str
    ledger_id: int

    @property
    def total(self) -> Decimal:
        return self.available + self.reserved


class BalanceService:
    """
    Handles all balance mutations.
    Must be called within an active transaction (session).
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = UserRepository(session)
        self.ledger_repo = LedgerRepository(session)

    async def _get_locked_user(self, user_id: int):
        """Get user with row lock. Raises if not found."""
        user = await self.user_repo.get_balance_for_update(user_id)
        if user is None:
            raise ValueError(f"User {user_id} not found")
        return user

    async def _apply(
        self,
        user_id: int,
        operation_type: str,
        amount: Decimal,
        direction: str,
        new_available: Decimal,
        new_reserved: Decimal,
        old_available: Decimal,
        old_reserved: Decimal,
        reference_type: str | None = None,
        reference_id: int | None = None,
        idempotency_key: str | None = None,
        reason: str | None = None,
    ) -> BalanceSnapshot:
        """Apply balance change and record ledger entry."""
        # Idempotency check
        if idempotency_key:
            exists = await self.ledger_repo.idempotency_check(idempotency_key)
            if exists:
                raise DuplicateOperationError(
                    f"Operation already executed: {idempotency_key}"
                )

        # Update user balance
        await self.user_repo.atomic_update_balance(
            user_id,
            available=to_db(new_available),
            reserved=to_db(new_reserved),
        )

        old_total = old_available + old_reserved
        new_total = new_available + new_reserved

        # Record ledger
        entry = await self.ledger_repo.record(
            user_id=user_id,
            operation_type=operation_type,
            amount=to_db(amount),
            direction=direction,
            balance_before=to_db(old_total),
            balance_after=to_db(new_total),
            available_before=to_db(old_available),
            available_after=to_db(new_available),
            reserved_before=to_db(old_reserved),
            reserved_after=to_db(new_reserved),
            reference_type=reference_type,
            reference_id=reference_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )

        logger.info(
            "Balance operation",
            extra={
                "user_id": user_id,
                "operation": operation_type,
                "amount": str(amount),
                "direction": direction,
                "available": str(new_available),
                "reserved": str(new_reserved),
            },
        )

        return BalanceSnapshot(
            available=new_available,
            reserved=new_reserved,
            amount=amount,
            operation_type=operation_type,
            ledger_id=entry.id,
        )

    # ── Public Operations ────────────────────────────────────────────────

    async def credit_deposit(
        self,
        user_id: int,
        amount: Decimal,
        deposit_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Credit user's available balance after successful deposit.
        available += amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if not is_valid_amount(amount):
            raise ValueError(f"Invalid deposit amount: {amount}")

        new_available = old_available + amount

        return await self._apply(
            user_id=user_id,
            operation_type="deposit",
            amount=amount,
            direction="credit",
            new_available=new_available,
            new_reserved=old_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="deposit",
            reference_id=deposit_id,
            idempotency_key=idempotency_key or f"deposit:{deposit_id}",
            reason="Пополнение баланса",
        )

    async def reserve_for_campaign(
        self,
        user_id: int,
        amount: Decimal,
        campaign_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Reserve funds for a new campaign.
        available -= amount, reserved += amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if old_available < amount:
            raise InsufficientFundsError(
                f"Need {amount}, available {old_available}"
            )

        new_available = old_available - amount
        new_reserved = old_reserved + amount

        return await self._apply(
            user_id=user_id,
            operation_type="campaign_reserve",
            amount=amount,
            direction="debit",
            new_available=new_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="campaign",
            reference_id=campaign_id,
            idempotency_key=idempotency_key or f"campaign_reserve:{campaign_id}",
            reason="Резерв для рекламной кампании",
        )

    async def spend_from_reserve(
        self,
        user_id: int,
        amount: Decimal,
        campaign_id: int,
        task_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Spend from reserved balance when task is confirmed.
        reserved -= amount (money leaves user's account)
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if old_reserved < amount:
            raise InsufficientReserveError(
                f"Need {amount} from reserve, have {old_reserved}"
            )

        new_reserved = old_reserved - amount

        return await self._apply(
            user_id=user_id,
            operation_type="campaign_spend",
            amount=amount,
            direction="debit",
            new_available=old_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="campaign",
            reference_id=campaign_id,
            idempotency_key=idempotency_key or f"task_spend:{task_id}",
            reason="Списание за выполненное задание",
        )

    async def release_reserve(
        self,
        user_id: int,
        amount: Decimal,
        campaign_id: int,
        reason: str = "Возврат резерва",
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Release reserved funds back to available (partial refund, campaign cancel).
        reserved -= amount, available += amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if old_reserved < amount:
            raise InsufficientReserveError(
                f"Need {amount} from reserve, have {old_reserved}"
            )

        new_available = old_available + amount
        new_reserved = old_reserved - amount

        return await self._apply(
            user_id=user_id,
            operation_type="reserve_release",
            amount=amount,
            direction="credit",
            new_available=new_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="campaign",
            reference_id=campaign_id,
            idempotency_key=idempotency_key,
            reason=reason,
        )

    async def credit_seller_reward(
        self,
        user_id: int,
        amount: Decimal,
        campaign_id: int,
        task_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Credit seller for confirmed task completion.
        available += amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        new_available = old_available + amount

        return await self._apply(
            user_id=user_id,
            operation_type="seller_reward",
            amount=amount,
            direction="credit",
            new_available=new_available,
            new_reserved=old_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="campaign",
            reference_id=campaign_id,
            idempotency_key=idempotency_key or f"seller_reward:{task_id}",
            reason="Вознаграждение за выполненное задание",
        )

    async def reserve_for_withdrawal(
        self,
        user_id: int,
        amount: Decimal,
        withdrawal_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Reserve funds for pending withdrawal.
        available -= amount, reserved += amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if old_available < amount:
            raise InsufficientFundsError(
                f"Need {amount}, available {old_available}"
            )

        new_available = old_available - amount
        new_reserved = old_reserved + amount

        return await self._apply(
            user_id=user_id,
            operation_type="withdrawal_reserve",
            amount=amount,
            direction="debit",
            new_available=new_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="withdrawal",
            reference_id=withdrawal_id,
            idempotency_key=idempotency_key or f"withdrawal_reserve:{withdrawal_id}",
            reason="Резерв для вывода",
        )

    async def complete_withdrawal(
        self,
        user_id: int,
        amount: Decimal,
        withdrawal_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Finalize withdrawal — money leaves the system.
        reserved -= amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if old_reserved < amount:
            raise InsufficientReserveError(
                f"Need {amount} from reserve, have {old_reserved}"
            )

        new_reserved = old_reserved - amount

        return await self._apply(
            user_id=user_id,
            operation_type="withdrawal_payout",
            amount=amount,
            direction="debit",
            new_available=old_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="withdrawal",
            reference_id=withdrawal_id,
            idempotency_key=idempotency_key or f"withdrawal_payout:{withdrawal_id}",
            reason="Вывод средств",
        )

    async def release_withdrawal(
        self,
        user_id: int,
        amount: Decimal,
        withdrawal_id: int,
        reason: str = "Отмена вывода",
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Release withdrawal reserve (rejected / failed).
        reserved -= amount, available += amount
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        if old_reserved < amount:
            raise InsufficientReserveError(
                f"Need {amount} from reserve, have {old_reserved}"
            )

        new_available = old_available + amount
        new_reserved = old_reserved - amount

        return await self._apply(
            user_id=user_id,
            operation_type="withdrawal_release",
            amount=amount,
            direction="credit",
            new_available=new_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="withdrawal",
            reference_id=withdrawal_id,
            idempotency_key=idempotency_key or f"withdrawal_release:{withdrawal_id}",
            reason=reason,
        )

    async def admin_adjustment(
        self,
        user_id: int,
        amount: Decimal,
        reason: str,
        admin_telegram_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Admin manual balance adjustment (positive = credit, negative = debit).
        available += amount (can be negative)
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        amount = round_down(amount)
        new_available = old_available + amount

        if new_available < ZERO:
            raise InsufficientFundsError(
                f"Adjustment would result in negative balance: {new_available}"
            )

        direction = "credit" if amount >= ZERO else "debit"
        abs_amount = abs(amount)

        return await self._apply(
            user_id=user_id,
            operation_type="admin_adjustment",
            amount=abs_amount,
            direction=direction,
            new_available=new_available,
            new_reserved=old_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            idempotency_key=idempotency_key,
            reason=f"[Admin {admin_telegram_id}] {reason}",
        )

    async def refund_with_commission(
        self,
        user_id: int,
        gross_amount: Decimal,
        commission_amount: Decimal,
        campaign_id: int,
        idempotency_key: str | None = None,
    ) -> BalanceSnapshot:
        """
        Refund from reserve with commission deducted.
        net = gross - commission
        reserved -= gross, available += net
        (commission is lost — platform revenue)
        """
        user = await self._get_locked_user(user_id)
        old_available = from_db(user.available)
        old_reserved = from_db(user.reserved)

        gross_amount = round_down(gross_amount)
        commission_amount = round_down(commission_amount)
        net_amount = gross_amount - commission_amount

        if old_reserved < gross_amount:
            raise InsufficientReserveError(
                f"Need {gross_amount} from reserve, have {old_reserved}"
            )

        new_available = old_available + net_amount
        new_reserved = old_reserved - gross_amount

        return await self._apply(
            user_id=user_id,
            operation_type="refund",
            amount=net_amount,
            direction="credit",
            new_available=new_available,
            new_reserved=new_reserved,
            old_available=old_available,
            old_reserved=old_reserved,
            reference_type="campaign",
            reference_id=campaign_id,
            idempotency_key=idempotency_key,
            reason=f"Возврат {net_amount} (комиссия {commission_amount})",
        )

    # ── Query ────────────────────────────────────────────────────────────

    async def get_balance(self, user_id: int) -> tuple[Decimal, Decimal]:
        """Get (available, reserved) without locking."""
        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            return ZERO, ZERO
        return from_db(user.available), from_db(user.reserved)
