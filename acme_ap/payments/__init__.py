"""Payments: schedule, submit (idempotently), reconcile. See service.py."""
from .bank import BankAdapter, BankError, BankRejected, MockBank
from .service import PayResult, PaymentService, Reconciliation


def default_payments(erp) -> PaymentService:
    """A payment service on the same database as the ERP mock (tests, scripts, the approver CLI)."""
    fresh = getattr(erp, "ledger_reset", False)
    return PaymentService(erp.path, erp, MockBank(erp.path + ".bank", reset=fresh), reset=fresh)


__all__ = ["BankAdapter", "BankError", "BankRejected", "MockBank", "PayResult", "PaymentService", "Reconciliation", "default_payments"]
