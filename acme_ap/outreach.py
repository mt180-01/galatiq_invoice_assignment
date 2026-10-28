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
NO_CONTACT = {"fraud_indicators", "bank_account_changed", "pressure_language", "unknown_vendor", "vendor_blocked", "duplicate_invoice",
              "extractor_disagreement", "ocr_source"}


def _asks(outcome: Outcome) -> list[tuple[str, str]]:
    """(finding code, question) in priority order, de-duplicated."""
    inv = outcome.invoice
    questions: dict[str, str] = {}
    if any(f.code == "unreadable_document" for f in outcome.findings):
        return [("unreadable", "We could not read the attached file. Could you resend it as a text-based PDF, or just reply with the invoice number, date, items and total?")]
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


def followup_context(outcome: Outcome) -> dict | None:
    """Who to ask and what to ask, decided by rules alone. None = nobody should be contacted."""
    inv = outcome.invoice
    if inv is None or outcome.approval is None or outcome.paid or outcome.error:
        return None
    if any(f.code in NO_CONTACT for f in outcome.findings):
        return None
    asks = _asks(outcome)[:MAX_ASKS]
    if not asks:
        return None
    return {"to": outcome.sender or "(sender unknown: file intake)", "ref": inv.invoice_number or outcome.subject or "your invoice", "asks": asks}


def render_followup(ctx: dict, body: str | None = None) -> str:
    """The email. `body` (from a model, already checked) replaces the template's wording; the questions are always ours."""
    head = [f"To: {ctx['to']}", f"Subject: Quick question on invoice {ctx['ref']}", ""]
    if body:
        return "\n".join(head + [body.strip()])
    asks = ctx["asks"]
    lines = head + ["Hello,", "", f"Thanks for your invoice {ctx['ref']}. We can pay it as soon as we confirm {'this' if len(asks) == 1 else 'these'}"
                    f" (a one-line reply is fine, no forms or portal needed):", ""]
    lines += [f"  {i}. {q}" for i, (_, q) in enumerate(asks, 1)]
    return "\n".join(lines + ["", "Thank you,", "Accounts Payable, Acme Corp"])


def draft_followup(outcome: Outcome) -> str | None:
    ctx = followup_context(outcome)
    return render_followup(ctx) if ctx else None
