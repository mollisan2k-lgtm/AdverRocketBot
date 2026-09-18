import pytest
from decimal import Decimal
from app.utils.decimal_utils import to_db, from_db, round_down, is_valid_amount, format_amount_plain

def test_to_db_from_db():
    assert to_db(Decimal("1.23")) == "1.23"
    assert from_db("1.23") == Decimal("1.23")
    assert to_db(Decimal("0.00")) == "0.00"
    assert from_db("0") == Decimal("0.00")

def test_round_down():
    assert round_down(Decimal("1.239")) == Decimal("1.23")
    assert round_down(Decimal("1.231")) == Decimal("1.23")
    assert round_down(Decimal("1.200")) == Decimal("1.20")

def test_is_valid_amount():
    assert is_valid_amount(Decimal("1.23")) is True
    assert is_valid_amount(Decimal("-1.23")) is False
    assert is_valid_amount(Decimal("0.00")) is False
    assert is_valid_amount(Decimal("0.01")) is True

def test_format_amount_plain():
    assert format_amount_plain(Decimal("1.20")) == "1.20"
    assert format_amount_plain(Decimal("1.23")) == "1.23"
