"""
Time utilities.

- DB stores UTC
- Display uses Europe/Moscow
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

# Moscow timezone: UTC+3
MOSCOW_TZ = timezone(timedelta(hours=3))


def utc_now() -> datetime:
    """Current UTC time (timezone-naive for SQLite compatibility)."""
    return datetime.utcnow()


def to_moscow(dt: datetime | None) -> datetime | None:
    """Convert UTC datetime to Moscow time for display."""
    if dt is None:
        return None
    # Treat naive datetime as UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MOSCOW_TZ)


def format_datetime(dt: datetime | None) -> str:
    """Format datetime for display in Moscow timezone."""
    if dt is None:
        return "—"
    moscow = to_moscow(dt)
    return moscow.strftime("%d.%m.%Y %H:%M")


def format_date(dt: datetime | None) -> str:
    """Format date only."""
    if dt is None:
        return "—"
    moscow = to_moscow(dt)
    return moscow.strftime("%d.%m.%Y")


def seconds_ago(dt: datetime) -> float:
    """Seconds elapsed since the given UTC datetime."""
    return (utc_now() - dt).total_seconds()


def minutes_from_now(minutes: int) -> datetime:
    """UTC datetime `minutes` from now."""
    return utc_now() + timedelta(minutes=minutes)
