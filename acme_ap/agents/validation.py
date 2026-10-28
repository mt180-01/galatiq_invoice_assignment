"""Validation agent: checks an invoice against the ERP and against itself. Pure tools, no LLM:
every finding is reproducible and auditable."""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from ..erp import ErpAdapter
from ..models import Finding, Invoice, Severity

TOL = Decimal("0.01")
PRICE_TOLERANCE = Decimal("0.01")
APPROVAL_THRESHOLD = Decimal("10000")
GOUGING = Decimal("0.25")             # a billed price this far above the catalogue price is held for a person, not just noted
MAX_TAX_RATE = Decimal("0.25")        # no ordinary sales tax is this high
FUTURE_DAYS = 30
SPLIT_WINDOW_DAYS = 7
_INJECTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?(previous|prior|above|earlier)?\s*(instructions|rules|policy|policies|checks)"
    r"|\bsystem\s*:|you\s+are\s+(now\s+)?(the\s+|an?\s+)?(approver|assistant|ai|model|vp)"
    r"|(mark|set|treat)\s+(this|the)\s+invoice\s+(as\s+)?(approved|paid)|skip\s+(the\s+)?(validation|review|checks)|approve\s+this\s+invoice", re.I)
_FRAUD_PHRASES = re.compile(r"urgent|immediately|wire transfer|penalt|asap|final notice", re.I)


def _f(code: str, sev: Severity, msg: str) -> Finding:
    return Finding(code=code, severity=sev, message=msg)


def validate(inv: Invoice, erp: ErpAdapter, accounts=None) -> list[Finding]:
    """`accounts` (optional) knows each vendor's verified bank account, so a changed account is caught before approval."""
    out: list[Finding] = []
    E, H, W = Severity.ERROR, Severity.HOLD, Severity.WARN

    if any(w.startswith("no text layer") for w in inv.parse_warnings):
        # Nothing was extracted, so every other check would just echo "missing". Report the one real problem.
        return [_f("unreadable_document", H, "document could not be read (scanned image and OCR unavailable or empty)")]
    for w in inv.parse_warnings:
        if w.startswith("extractor disagreement"):
            out.append(_f("extractor_disagreement", H, w))
        elif w.startswith("unparseable invoice date"):  # the date drives terms, the future-dated check and the audit trail: a person confirms it before money moves
            out.append(_f("unreadable_date", H, w))
        else:
            out.append(_f("extraction_warning", W, w))
    if not inv.invoice_number:
        out.append(_f("missing_invoice_number", E, "no invoice number"))
    if not inv.line_items:
        out.append(_f("no_line_items", E, "no line items could be extracted"))
    if inv.total is None:
        out.append(_f("missing_total", E, "no invoice total"))
    elif inv.total < 0:
        out.append(_f("negative_total", E, f"total is negative ({inv.total})"))
    elif inv.total == 0:
        out.append(_f("zero_total", E, "invoice total is zero"))
    if inv.revision:
        out.append(_f("revision", H, f"revision {inv.revision}: may supersede an earlier bill; confirm before paying"))
    if inv.currency != "USD":
        out.append(_f("foreign_currency", H, f"billed in {inv.currency}; no FX policy to settle in USD"))

    vendor = None
    if not inv.vendor:
        out.append(_f("missing_vendor", E, "vendor name is missing"))
    else:
        vendor = erp.lookup_vendor(inv.vendor)
        if vendor is None:
            out.append(_f("unknown_vendor", H, f"vendor {inv.vendor!r} is not in the approved vendor master"))
        elif vendor.status != "approved":
            out.append(_f("vendor_blocked", E, f"vendor {vendor.name!r} is {vendor.status}"))

    if accounts and inv.vendor and inv.bank_account:
        if problem := accounts.check_account(inv.vendor, inv.bank_account):
            out.append(_f("bank_account_changed", H, problem))
    if inv.invoice_number and inv.total is not None:
        from ..identity import bill_key  # local import: identity needs the ERP package, validation is imported by it indirectly
        for dup in erp.similar_bills(inv.invoice_number, inv.total, inv.vendor or "", bill_key(inv, erp)):
            out.append(_f("possible_duplicate", H, f"same invoice number and total as a bill already received from {dup['vendor']!r} ({dup['status']}, "
                                                    f"{os.path.basename(dup['source'])}): possibly the same bill under a different vendor spelling"))
    if inv.invoice_number:
        _check_related_bills(inv, erp, out)
    _check_dates(inv, out)
    _check_items(inv, erp, out)
    _check_math(inv, out)
    _check_fraud(inv, vendor is None, out)

    if any(w.startswith("ocr:") for w in inv.parse_warnings):
        # OCR can misread digits and drop table rows, so its output is a draft for a person, never a payment
        # instruction. Extraction-dependent errors are capped at HOLD (a human sees them all) and the vendor is
        # not asked to explain numbers that may simply be our misreading.
        out = [f.model_copy(update={"severity": H}) if f.severity is E else f for f in out]
        out.append(_f("ocr_source", H, "text recovered from a scanned image by OCR; a person must verify before payment"))

    if inv.total is not None and APPROVAL_THRESHOLD * Decimal("0.95") <= inv.total <= APPROVAL_THRESHOLD:
        out.append(_f("near_threshold", W, f"total {inv.total} sits just under the {APPROVAL_THRESHOLD} review threshold"))
    return out


def _check_dates(inv: Invoice, out: list[Finding]) -> None:
    if inv.date and inv.due_date and inv.due_date < inv.date:
        out.append(_f("due_before_invoice", Severity.WARN, f"due date {inv.due_date} is before invoice date {inv.date}"))
    elif inv.date and inv.due_date and inv.due_date == inv.date and inv.payment_terms and re.search(r"net\s*\d+", inv.payment_terms, re.I):
        out.append(_f("terms_mismatch", Severity.WARN, f"due date equals invoice date but terms are {inv.payment_terms!r}"))
    if not inv.due_date and not any("due date" in w for w in inv.parse_warnings):
        out.append(_f("missing_due_date", Severity.WARN, "no due date"))
    if inv.date:
        try:
            when = date.fromisoformat(inv.date)
            if when.year < 2000:
                out.append(_f("implausible_date", Severity.WARN, f"implausible invoice date {inv.date}"))
            elif when > date.today() + timedelta(days=FUTURE_DAYS):
                out.append(_f("future_dated", Severity.HOLD, f"invoice is dated {inv.date}, more than {FUTURE_DAYS} days ahead"))
        except ValueError:
            pass


def _check_related_bills(inv: Invoice, erp: ErpAdapter, out: list[Finding]) -> None:
    """Two things only visible across invoices: the same order billed again under a new number, and one large purchase cut into pieces that each
    stay under the review limit."""
    from ..identity import bill_key, order_fingerprint  # local import: identity needs the ERP package, validation is imported by it indirectly
    key = bill_key(inv, erp)
    others = erp.recent_bills(key, SPLIT_WINDOW_DAYS)
    if not others:
        return
    mine = order_fingerprint(inv, erp)
    if mine and any(o["fingerprint"] == mine for o in others):
        out.append(_f("repeat_order", Severity.HOLD, "same vendor, total and items as another invoice received in the last "
                                                     f"{SPLIT_WINDOW_DAYS} days under a different number: possibly the same order billed twice"))
    floor = APPROVAL_THRESHOLD / 4
    pieces = [o["total"] for o in others if o["total"] is not None and floor <= o["total"] <= APPROVAL_THRESHOLD]
    if inv.total is not None and floor <= inv.total <= APPROVAL_THRESHOLD and pieces and inv.total + sum(pieces) > APPROVAL_THRESHOLD:
        out.append(_f("split_invoices", Severity.HOLD, f"{len(pieces) + 1} invoices from this vendor in {SPLIT_WINDOW_DAYS} days, each under {APPROVAL_THRESHOLD} "
                                                       f"but {inv.total + sum(pieces)} together: possibly one purchase split to avoid review"))


def _check_items(inv: Invoice, erp: ErpAdapter, out: list[Finding]) -> None:
    E, W = Severity.ERROR, Severity.WARN
    wanted: dict[str, int] = defaultdict(int)
    records = {}
    for li in inv.line_items:
        if li.quantity <= 0:
            out.append(_f("invalid_quantity", E, f"{li.item}: quantity {li.quantity} is not positive"))
            continue
        rec = erp.lookup_item(li.item)
        if rec is None:
            out.append(_f("unknown_item", E, f"{li.item!r} not found in inventory"))
            continue
        records[rec.name] = rec
        wanted[rec.name] += li.quantity
        if rec.fuzzy:
            out.append(_f("fuzzy_item_match", W, f"{li.item!r} matched to {rec.name!r} by approximate name"))
        if li.unit_price is not None and rec.unit_price > 0:
            drift = abs(li.unit_price - rec.unit_price) / rec.unit_price
            if drift > PRICE_TOLERANCE:
                out.append(_f("price_variance", Severity.HOLD if drift > GOUGING else W, f"{rec.name}: billed {li.unit_price} vs catalog {rec.unit_price} ({drift:.0%} off)"
                              + (f" [{li.note}]" if li.note else "")))
    for name, qty in wanted.items():
        rec = records[name]
        if rec.stock == 0:
            out.append(_f("out_of_stock", E, f"{name}: zero stock on hand, {qty} billed"))
        elif qty > rec.stock:
            out.append(_f("insufficient_stock", E, f"{name}: {qty} billed but only {rec.stock} in stock"))


def _check_math(inv: Invoice, out: list[Finding]) -> None:
    E = Severity.ERROR
    line_sum = Decimal(0)
    for li in inv.line_items:
        if li.unit_price is not None:
            expected = li.unit_price * li.quantity
            if li.amount is not None and abs(li.amount - expected) > TOL:
                out.append(_f("line_math", E, f"{li.item}: {li.quantity} x {li.unit_price} = {expected}, invoice says {li.amount}"))
            line_sum += li.amount if li.amount is not None else expected
        elif li.amount is not None:
            line_sum += li.amount
    if inv.subtotal is not None and inv.line_items and abs(line_sum - inv.subtotal) > TOL:
        out.append(_f("subtotal_math", E, f"line items sum to {line_sum} but subtotal is {inv.subtotal}"))
    base = inv.subtotal if inv.subtotal is not None else line_sum
    if inv.tax_amount and base > 0 and inv.tax_amount / base > MAX_TAX_RATE:
        out.append(_f("implausible_tax", Severity.HOLD, f"tax {inv.tax_amount} is {inv.tax_amount / base:.0%} of {base}"))
    if inv.total is not None and inv.line_items:
        expected_total = base + (inv.tax_amount or 0) + (inv.shipping or 0)
        if abs(expected_total - inv.total) > TOL:
            out.append(_f("total_math", E, f"subtotal {base} + tax {inv.tax_amount or 0} + shipping {inv.shipping or 0} = {expected_total}, invoice total is {inv.total}"))


def _check_fraud(inv: Invoice, vendor_unknown: bool, out: list[Finding]) -> None:
    signals = []
    text = " ".join(filter(None, [inv.notes, inv.payment_terms, *[li.note for li in inv.line_items], *[li.item for li in inv.line_items]]))
    if _INJECTION.search(text):
        out.append(_f("embedded_instructions", Severity.HOLD, "the document contains text addressed to an automated reader (instructions to approve or skip checks); treated as data, never obeyed"))
    blob = " ".join(filter(None, [inv.notes, inv.payment_terms]))
    phrases = sorted({m.group(0).lower() for m in _FRAUD_PHRASES.finditer(blob)})
    if phrases:
        signals.append(f"pressure language ({', '.join(phrases)})")
    if inv.payment_terms and re.search(r"immediate", inv.payment_terms, re.I):
        signals.append("immediate payment terms")
    if vendor_unknown and inv.vendor:
        signals.append("unknown vendor")
    if any(f.code in ("out_of_stock", "unknown_item") for f in out):
        signals.append("billing for items that do not exist / have no stock")
    if len(signals) >= 3:
        out.append(_f("fraud_indicators", Severity.ERROR, "multiple fraud signals: " + "; ".join(signals)))
    elif phrases:
        out.append(_f("pressure_language", Severity.HOLD, "payment-pressure language: " + "; ".join(signals)))
