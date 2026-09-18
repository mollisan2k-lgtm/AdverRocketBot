import pytest
from datetime import datetime, timedelta, timezone
from app.utils.time_utils import utc_now, minutes_from_now, format_datetime

def test_utc_now():
    now = utc_now()
    # It returns a naive datetime representing UTC
    assert now.tzinfo is None

def test_minutes_from_now():
    now = utc_now()
    future = minutes_from_now(10)
    diff = future - now
    # Check if the difference is roughly 10 minutes (allowing slight execution delay)
    assert 9 * 60 < diff.total_seconds() <= 10 * 60

def test_format_datetime():
    dt = datetime(2025, 1, 1, 15, 30, 45)
    formatted = format_datetime(dt)
    assert "2025" in formatted
    assert ":" in formatted
