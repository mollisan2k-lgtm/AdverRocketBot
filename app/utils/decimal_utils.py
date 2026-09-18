"""
Decimal utilities for financial calculations.

Rules:
- NEVER use float for money
- All amounts: Decimal with 2 decimal places
- Rounding: ROUND_DOWN to 0.01 USDT
- Minimum monetary unit: 0.01 USDT
- Storage: as TEXT in SQLite
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, InvalidOperation

# Constants
ZERO = Decimal("0.00")
MIN_AMOUNT = Decimal("0.01")
TWO_PLACES = Decimal("0.01")
HUNDRED = Decimal("100")


def to_decimal(value: str | int | float | Decimal | None) -> Decimal:
    """Convert value to Decimal. Returns ZERO for None/empty."""
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ZERO


def round_down(amount: Decimal) -> Decimal:
    """Round down to 2 decimal places (0.01 USDT)."""
    return amount.quantize(TWO_PLACES, rounding=ROUND_DOWN)


def format_amount(amount: Decimal | str) -> str:
    """Format amount for display: '0.00 USDT'."""
    d = to_decimal(amount) if isinstance(amount, str) else amount
    return f"{round_down(d)} USDT"


def format_amount_plain(amount: Decimal | str) -> str:
    """Format amount without currency suffix: '0.00'."""
    d = to_decimal(amount) if isinstance(amount, str) else amount
    return str(round_down(d))


def calculate_commission(amount: Decimal, percent: Decimal) -> Decimal:
    """
    Calculate commission amount.
    commission = amount * (percent / 100), rounded down.
    """
    return round_down(amount * percent / HUNDRED)


def is_valid_amount(amount: Decimal) -> bool:
    """Check if amount >= minimum (0.01 USDT)."""
    return amount >= MIN_AMOUNT


def to_db(amount: Decimal) -> str:
    """Convert Decimal to TEXT for SQLite storage."""
    return str(round_down(amount))


def from_db(value: str | None) -> Decimal:
    """Convert TEXT from SQLite to Decimal."""
    return to_decimal(value)
