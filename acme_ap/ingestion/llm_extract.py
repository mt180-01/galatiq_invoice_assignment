"""LLM extraction for unstructured text, with a self-correction loop and a cross-check.

1. Ask Grok for the schema-shaped JSON.
2. Check its arithmetic; if it does not add up, show the model its own errors and retry.
3. Compare against the independent regex extraction; any disagreement forces human review,
   so a hallucinated number cannot silently become a payment.
"""
from __future__ import annotations

from decimal import Decimal

from ..llm import LLM, LLMUnavailable
from ..models import Invoice, LineItem
from .parsing import normalize_invoice_number, parse_date, parse_money

SYSTEM = """You extract invoice data. Return JSON:
{"invoice_number": str|null, "vendor": str|null, "date": "YYYY-MM-DD"|null, "due_date": "YYYY-MM-DD"|null,
 "currency": "USD", "line_items": [{"item": str, "quantity": int, "unit_price": number|null, "amount": number|null}],
 "subtotal": number|null, "tax_amount": number|null, "shipping": number|null, "total": number|null,
 "payment_terms": str|null, "notes": str|null}
Fix obvious OCR typos (O for 0). Never invent values: use null when absent. Keep negative numbers as written."""
MAX_ATTEMPTS = 3


def _build(data: dict) -> Invoice:
    items = [LineItem(item=str(li["item"]), quantity=int(li["quantity"]), unit_price=parse_money(li.get("unit_price")),
                      amount=parse_money(li.get("amount"))) for li in data.get("line_items", [])]
    return Invoice(
        invoice_number=normalize_invoice_number(data.get("invoice_number")), vendor=data.get("vendor") or None,
        date=parse_date(data.get("date")), due_date=parse_date(data.get("due_date")),
        currency=data.get("currency") or "USD", line_items=items, subtotal=parse_money(data.get("subtotal")),
        tax_amount=parse_money(data.get("tax_amount")), shipping=parse_money(data.get("shipping")),
        total=parse_money(data.get("total")), payment_terms=data.get("payment_terms") or None,
        notes=data.get("notes") or None, channel="unstructured", extractor="grok")


def _self_check(inv: Invoice) -> list[str]:
    problems = []
    if inv.line_items and inv.total is not None:
        lines = sum((li.amount if li.amount is not None else (li.unit_price or 0) * li.quantity) for li in inv.line_items)
        expected = (inv.subtotal if inv.subtotal is not None else lines) + (inv.tax_amount or 0) + (inv.shipping or 0)
        if abs(Decimal(expected) - inv.total) > Decimal("0.01"):
            problems.append(f"line items/subtotal/tax/shipping add up to {expected}, but total is {inv.total}")
    return problems


async def extract(text: str, baseline: Invoice, llm: LLM) -> Invoice:
    feedback = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            inv = _build(await llm.chat_json(SYSTEM, f"Invoice text:\n{text}\n{feedback}"))
        except (LLMUnavailable, KeyError, ValueError, TypeError) as exc:
            baseline.parse_warnings.append(f"LLM extraction failed ({exc}); used regex extraction")
            return baseline
        problems = _self_check(inv)
        if not problems:
            break
        # Only retry if the regex baseline thinks the document itself is consistent; otherwise the
        # arithmetic error is real (a bad invoice) and validation should report it, not the model.
        if not _self_check(baseline):
            feedback = "Your previous answer had problems: " + "; ".join(problems) + ". Re-read the text and correct them."
            continue
        break
    inv.source_path, inv.source_format = baseline.source_path, baseline.source_format
    inv.parse_warnings = list(baseline.parse_warnings)
    diffs = []
    if inv.total != baseline.total:
        diffs.append(f"total {inv.total} vs {baseline.total}")
    if sorted((li.item, li.quantity) for li in inv.line_items) != sorted((li.item, li.quantity) for li in baseline.line_items):
        diffs.append("line items/quantities differ")
    if diffs:
        inv.parse_warnings.append("extractor disagreement (grok vs regex): " + "; ".join(diffs))
    inv.extractor = f"grok+regex-check (attempt {attempt})"
    return inv
