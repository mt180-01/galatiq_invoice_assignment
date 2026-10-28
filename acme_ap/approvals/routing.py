"""Who must decide, and why. Pure functions: easy to audit and test."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..models import Outcome, Severity

VP_THRESHOLD = Decimal("10000")   # above this a person must sign off, however clean the invoice
CFO_THRESHOLD = Decimal("50000")

# Role mailboxes, each with the delegate who gets it if the owner does not respond (fictional addresses).
DIRECTORY = {
    "VP Finance":  {"email": "vp-finance@acmecorp.example",  "delegate": "CFO"},
    "CFO":         {"email": "cfo@acmecorp.example",         "delegate": "CEO"},
    "CEO":         {"email": "ceo@acmecorp.example",         "delegate": None},
    "Procurement": {"email": "procurement@acmecorp.example", "delegate": "VP Finance"},
    "Treasury":    {"email": "treasury@acmecorp.example",    "delegate": "VP Finance"},
    "AP Manager":  {"email": "ap-manager@acmecorp.example",  "delegate": "VP Finance"},
}

def authorized(role: str, escalated_from: str | None = None) -> set[str]:
    """Mailboxes allowed to decide a request: whoever it is with now, anyone senior to them (the delegate chain),
    and the original owner if it was escalated. A forwarded or leaked token is useless to anyone else."""
    allowed, current = set(), role
    while current:
        allowed.add(DIRECTORY[current]["email"])
        current = DIRECTORY[current]["delegate"]
    if escalated_from:
        allowed.add(DIRECTORY[escalated_from]["email"])
    return allowed


# Which finding needs whose eyes (first match wins, in this order).
_DOMAIN = [("fraud_indicators", "AP Manager"), ("bank_account_changed", "AP Manager"), ("possible_duplicate", "AP Manager"), ("pressure_language", "AP Manager"), ("extractor_disagreement", "AP Manager"),
           ("ocr_source", "AP Manager"), ("revision", "AP Manager"), ("foreign_currency", "Treasury"), ("unknown_vendor", "Procurement")]
NOT_APPROVABLE = {"unreadable_document"}  # nothing to approve: a person has to read/key it first


@dataclass(frozen=True)
class Route:
    role: str
    reasons: list[str]
    approvable: bool = True


def route(outcome: Outcome) -> Route | None:
    """None = no human decision applies (paid, rejected, or already failed)."""
    if outcome.invoice is None or outcome.approval is None or outcome.paid or outcome.error:
        return None
    if outcome.approval.decision.value != "escalate":
        return None
    findings = outcome.findings
    codes = {f.code for f in findings}
    if codes & NOT_APPROVABLE:
        return Route("AP Manager", ["document could not be read"], approvable=False)
    total = outcome.invoice.total or Decimal(0)
    reasons = [f.message for f in findings if f.severity is Severity.HOLD]
    if total > VP_THRESHOLD:
        reasons.insert(0, f"amount {total} is over the {VP_THRESHOLD} sign-off limit")
    if total > CFO_THRESHOLD:
        return Route("CFO", reasons)
    for code, role in _DOMAIN:
        if code in codes:
            return Route(role, reasons)
    return Route("VP Finance", reasons)
