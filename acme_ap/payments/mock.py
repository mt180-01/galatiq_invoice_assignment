"""The brief's mock payment function. The simulated bank calls it to 'move' the money; swap the bank adapter for a real
one and this function disappears with the simulation."""
from __future__ import annotations

import logging
from decimal import Decimal

log = logging.getLogger("acme.payment")


def mock_payment(vendor: str, amount: Decimal) -> dict:
    log.info("Paid %s to %s", amount, vendor)
    return {"status": "success"}
