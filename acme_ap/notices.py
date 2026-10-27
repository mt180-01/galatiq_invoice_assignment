"""Telling people what is happening before they have to ask.

  * Vendors: a remittance advice when paid, a date when scheduled, a heads-up when waiting for sign-off.
  * AP team: one digest of what is stuck, with whom, for how long, and what happens next.
Drafts only; nothing is sent. The same rule as follow-ups applies: a suspicious invoice's sender is never contacted.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from .metrics import Kpis, kpis
from .models import Outcome
from .outreach import NO_CONTACT

EXPECTED_DECISION = "within 2 business days"  # matches the 48h escalation in the approval workflow


def draft_notice(outcome: Outcome, today: date | None = None) -> str | None:
    inv = outcome.invoice
    if inv is None or outcome.error or outcome.approval is None or outcome.followup:
        return None  # a question to the vendor already covers this invoice
    if outcome.status == "DUPLICATE" or any(f.code in NO_CONTACT or f.code == "unreadable_document" for f in outcome.findings):
        return None
    today = today or date.today()
    ref = inv.invoice_number or "your invoice"
    amount = outcome.payment_amount or inv.total or Decimal(0)
    if outcome.paid:
        rail = (outcome.payment_ref or "").split("-")[0]
        eta = "same day" if rail == "WIRE" else "about 2 business days"
        body = (f"We sent payment of {inv.currency} {amount:,.2f} on {today} by {rail or 'bank transfer'} "
                f"(reference {outcome.payment_ref}). It should arrive {eta}.")
        subject = f"Payment sent: invoice {ref}"
    elif outcome.status == "SCHEDULED":
        body = (f"Invoice {ref} is approved. Payment of {inv.currency} {amount:,.2f} will be sent on {outcome.release_on}, "
                f"in line with the payment terms. Nothing is needed from you.")
        subject = f"Approved: invoice {ref}"
    elif outcome.status == "AWAITING":
        body = (f"We received invoice {ref} and it is with {outcome.awaiting} for final sign-off. "
                f"You will have a decision {EXPECTED_DECISION}. Nothing is needed from you.")
        subject = f"Received: invoice {ref}"
    else:
        return None  # rejections for our own reasons are handled internally
    return "\n".join([f"To: {outcome.sender or '(sender unknown: file intake)'}", f"Subject: {subject}", "",
                      "Hello,", "", body, "", "Accounts Payable, Acme Corp"])


def _money(value) -> str:
    return f"{value:,.2f}" if value is not None else "-"


def digest(outcomes: list[Outcome], waiting: list[dict], payments: list | None = None, today: date | None = None) -> str:
    """For the AP manager: what needs attention today and why, before anyone has to chase."""
    k: Kpis = kpis(outcomes)
    lines = [f"AP daily digest {today or date.today()}", "",
             f"{k.invoices} invoice(s) processed: {k.hands_off} needed nobody ({k.hands_off_rate:.0%}), "
             f"{len(waiting)} waiting on a person, {k.stopped} stopped before payment, "
             f"{k.duplicates_blocked} duplicate(s) blocked, {k.asked_vendor} vendor question(s) drafted.", ""]
    lines.append("WAITING ON A PERSON (oldest first)")
    if waiting:
        for w in sorted(waiting, key=lambda w: -w["age_hours"]):
            moved = f", moved up from {w['escalated_from']}" if w["escalated_from"] else ""
            lines.append(f"  - {w['invoice']} {w['vendor']} {_money(w['amount'])}: with {w['role']}{moved}, {w['age_hours']:.0f}h old. "
                         f"{w['next']} in {w['next_in_hours']:.0f}h. Why: {'; '.join(w['reasons'])[:90]}")
    else:
        lines.append("  nothing")
    lines += ["", "STOPPED BEFORE PAYMENT"]
    stopped = [o for o in outcomes if o.status == "REJECT" and o.invoice]
    lines += [f"  - {o.invoice.invoice_number} {o.invoice.vendor}: {o.approval.reasoning[:110]}" for o in stopped] or ["  nothing"]
    if k.reasons:
        lines.append("  most common reasons: " + ", ".join(f"{c} x{n}" for c, n in k.reasons.most_common(4)))
    if payments is not None:
        later = [p for p in payments if p["state"] == "scheduled"]
        held = [p for p in payments if p["state"] in ("held", "failed")]
        lines += ["", f"PAYMENTS: {len(later)} scheduled for later ({_money(sum(Decimal(p['amount']) for p in later))}), "
                      f"{len(held)} held or failed" + ("".join(f"\n  - {p['bill_key']}: {p['last_error']}" for p in held))]
    return "\n".join(lines)
