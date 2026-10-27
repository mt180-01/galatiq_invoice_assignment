"""Vendor follow-up drafts: ask the least possible, only when the vendor can actually fix it.

Principles
  * One email per invoice, at most MAX_ASKS questions, each answerable in one line.
  * Never ask for something we can resolve ourselves (stock levels, FX policy, approvals).
  * Never contact the sender when the invoice looks fraudulent, the vendor is unverified, or it is
    a duplicate: those go to a human, because a reply address on a suspicious invoice is attacker-controlled.
  * Drafts only. Nothing is sent by this system.
"""
from __future__ import annotations

from .models import Outcome, Severity

MAX_ASKS = 3
NO_CONTACT = {"fraud_indicators", "pressure_language", "unknown_vendor", "vendor_blocked", "duplicate_invoice",
              "extractor_disagreement"}


def _asks(outcome: Outcome) -> list[tuple[str, str]]:
    """(finding code, question) in priority order, de-duplicated."""
    inv = outcome.invoice
    questions: dict[str, str] = {}
    for f in outcome.findings:
        if f.severity is not Severity.ERROR:
            continue
        match f.code:
            case "missing_vendor":
                questions[f.code] = "What is the legal name of the company issuing this invoice?"
            case "missing_invoice_number":
                questions[f.code] = "What is the invoice number?"
            case "total_math" | "subtotal_math" | "line_math":
                questions["math"] = ("The line items do not add up to the invoice total "
                                     f"({f.message}). Is the stated total correct, or can you send a corrected invoice?")
            case "invalid_quantity" | "negative_total":
                questions["qty"] = ("A quantity or the total is negative or zero. Is this a credit note, "
                                    "or should it be a corrected invoice? (reply 'credit note' or send the corrected one)")
            case "unknown_item":
                questions[f.code] = (f"We cannot match an item on this invoice to our catalogue ({f.message}). "
                                     "Which PO number and PO line does it belong to?")
    if inv and not inv.due_date and any("due date" in w for w in inv.parse_warnings):
        questions["due"] = "What is the payment due date (YYYY-MM-DD)?"
    return list(questions.items())


def draft_followup(outcome: Outcome) -> str | None:
    inv = outcome.invoice
    if inv is None or outcome.approval is None or outcome.paid or outcome.error:
        return None
    if any(f.code in NO_CONTACT for f in outcome.findings):
        return None
    asks = _asks(outcome)[:MAX_ASKS]
    if not asks:
        return None
    ref = inv.invoice_number or outcome.subject or "your invoice"
    lines = [
        f"To: {outcome.sender or '(sender unknown: file intake)'}",
        f"Subject: Quick question on invoice {ref}",
        "",
        "Hello,",
        "",
        f"Thanks for your invoice {ref}. We can pay it as soon as we confirm {'this' if len(asks) == 1 else 'these'}"
        f" (a one-line reply is fine, no forms or portal needed):",
        "",
    ]
    lines += [f"  {i}. {q}" for i, (_, q) in enumerate(asks, 1)]
    lines += ["", "Thank you,", "Accounts Payable, Acme Corp"]
    return "\n".join(lines)
