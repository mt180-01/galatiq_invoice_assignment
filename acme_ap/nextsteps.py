"""What would get this invoice paid: for each reason it was stopped or held, who has to do what.

A rejection that only says "no" leaves the person to work out the fix. This turns the findings into a short to-do list, each line addressed to whoever can
act (the vendor, AP, a manager). It is derived from the findings, so it can never disagree with the decision, and it changes nothing about it.
"""
from __future__ import annotations

from .models import Decision, Finding, Outcome

VENDOR, AP, APPROVER = "ask the vendor", "AP", "approver"

# code -> (who, what). Order here is the order shown (the most fundamental problems first).
STEPS: dict[str, tuple[str, str]] = {
    "unreadable_document": (AP, "get a readable copy (a text PDF or the e-invoice file) and upload it again"),
    "missing_invoice_number": (VENDOR, "send a corrected invoice with its number"),
    "missing_vendor": (VENDOR, "send a corrected invoice that names the supplier"),
    "missing_total": (VENDOR, "send a corrected invoice with a total"),
    "no_line_items": (VENDOR, "send a corrected invoice that lists what was bought"),
    "vendor_blocked": (AP, "do not pay; ask procurement why this vendor is blocked"),
    "unknown_vendor": (AP, "check the supplier is real; if so, add them to the vendor master, then resubmit"),
    "unknown_item": (VENDOR, "confirm the product code; if it is a new product, add it to the catalogue, then resubmit"),
    "out_of_stock": (AP, "check the goods were received; billing for something with no stock is a red flag"),
    "insufficient_stock": (VENDOR, "confirm the quantity delivered and send a corrected invoice"),
    "invalid_quantity": (VENDOR, "send a corrected invoice; a quantity must be a positive number (use a credit note for returns)"),
    "negative_total": (VENDOR, "this looks like a credit note, not an invoice; handle it as one"),
    "zero_total": (VENDOR, "send a corrected invoice"),
    "line_math": (VENDOR, "send a corrected invoice: a line's quantity x price does not match its amount"),
    "subtotal_math": (VENDOR, "send a corrected invoice: the lines do not add up to the subtotal"),
    "total_math": (VENDOR, "send a corrected invoice: subtotal + tax + shipping does not equal the total"),
    "implausible_tax": (VENDOR, "ask what the tax line is; send a corrected invoice if it is wrong"),
    "price_variance": (APPROVER, "compare the billed price with the agreed price; approve only if a price change was agreed"),
    "duplicate_invoice": (AP, "nothing: this bill was already received. Open the first one"),
    "possible_duplicate": (AP, "compare with the earlier bill named in the finding; reject whichever is the copy"),
    "repeat_order": (AP, "check with the vendor and the receiving dock whether this is a second delivery or the same order billed twice"),
    "split_invoices": (APPROVER, "treat the invoices together as one purchase; approve them as a group or ask why they were split"),
    "bank_account_changed": (AP, "call the vendor on a number you already had (not one from the invoice) to confirm the new account; two people must approve it"),
    "fraud_indicators": (AP, "do not pay; escalate to whoever handles suspected fraud"),
    "embedded_instructions": (AP, "read the document: it contains text aimed at an automated reader; treat it as suspicious"),
    "pressure_language": (AP, "pressure to pay at once is a common fraud sign; verify with the vendor before paying"),
    "future_dated": (VENDOR, "ask why the invoice is dated in the future; it can be processed when its date arrives"),
    "foreign_currency": (AP, "decide how to settle a non-USD bill (no exchange-rate policy exists yet)"),
    "revision": (AP, "check whether this replaces an earlier bill; pay only one of them"),
    "extractor_disagreement": (AP, "two readings of the document disagree; open it and confirm the figures"),
    "ocr_source": (AP, "this came from a scan; check the figures against the image before payment"),
}


def next_steps(out: Outcome) -> list[tuple[str, str]]:
    """(who, what) lines for an invoice that is not simply paid or scheduled; [] when nothing is needed."""
    if out.approval is None or out.approval.decision is Decision.APPROVE:
        return []
    return _unique([STEPS[f.code] for f in sorted(_blocking(out.findings), key=lambda f: list(STEPS).index(f.code)) if f.code in STEPS])


def _blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity.value in ("error", "hold")]


def _unique(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(items))
