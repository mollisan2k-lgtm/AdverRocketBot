import pytest
from app.integrations.crypto_pay import InvoiceData

def test_invoice_data_parsing():
    raw_data = {
        "invoice_id": 12345,
        "status": "paid",
        "asset": "USDT",
        "amount": "10.00",
        "bot_invoice_url": "https://t.me/CryptoBot?start=inv123",
        "description": "Test invoice",
        "paid_at": "2025-01-01T12:00:00Z",
        "payload": "deposit:42"
    }
    
    invoice = InvoiceData.from_dict(raw_data)
    assert invoice.invoice_id == 12345
    assert invoice.status == "paid"
    assert invoice.asset == "USDT"
    assert invoice.amount == "10.00"
    assert invoice.pay_url == "https://t.me/CryptoBot?start=inv123"
    assert invoice.description == "Test invoice"
    assert invoice.paid_at == "2025-01-01T12:00:00Z"
    assert invoice.payload == "deposit:42"
