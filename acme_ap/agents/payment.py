"""Payment agent: the only place money moves."""
from __future__ import annotations

import hashlib
from decimal import Decimal


def mock_payment(vendor, amount):
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}


def pay(vendor: str, amount: Decimal, currency: str) -> str:
    """Returns a payment reference; raises if the bank call does not succeed."""
    if currency != "USD":
        raise ValueError(f"refusing to pay in {currency}")
    result = mock_payment(vendor, amount)
    if result.get("status") != "success":
        raise RuntimeError(f"payment failed: {result}")
    return "PAY-" + hashlib.sha1(f"{vendor}|{amount}".encode()).hexdigest()[:8].upper()
