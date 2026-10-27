"""Regex extractor for unstructured invoice text (typos, OCR noise, free-form emails).

CPU-bound, no network: runs inside the worker processes. It is the offline fallback for the
LLM extractor and also the independent cross-check against it.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..models import Invoice, LineItem
from .parsing import detect_currency, fix_ocr_digits, normalize_invoice_number, parse_date, parse_money

_NUM = r"[\dOo][\dOo,]*(?:\.[\dOo]+)?"
_LABEL_WORDS = {"date", "due", "tax", "total", "invoice", "subtotal", "terms", "shipping", "amt", "ref", "po"}

_INV_NO = re.compile(r"\b(?:invoice|inv)\b[ \t]*(?:number|no\.?|#)?[ \t]*[:#]?[ \t]*((?:INV[-\s]?)?\d{3,})", re.I)
_VENDOR = re.compile(r"^[ \t]*(?:vendor|vndr|supplier|seller)[ \t]*:[ \t]*(.+)$", re.I | re.M)
_FROM = re.compile(r"^[ \t]*from[ \t]*:[ \t]*(.+)$", re.I | re.M)
_DATE = re.compile(r"(?<![A-Za-z])(?<!due )(?:date|dt)[ \t]*:[ \t]*(.+)", re.I)
_DUE = re.compile(r"\bdue(?:[ \t]*(?:date|dt))?[ \t]*:[ \t]*(.+)", re.I)
_TERMS = re.compile(r"(?:payment|pymnt)[ \t]+terms[ \t]*:[ \t]*(.+)|^[ \t]*terms[ \t]*:[ \t]*(.+)", re.I | re.M)
_NOTES = re.compile(r"^[ \t]*notes?[ \t]*:[ \t]*(.+)$", re.I | re.M)
_FORMERLY = re.compile(r"\(\s*formerly[^)]*\)", re.I)

_TOTAL = re.compile(rf"^[ \t]*(?:grand[ \t]+total|total[ \t]+amount|total[ \t]+due|total|amt|amount)[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)
_SUBTOTAL = re.compile(rf"^[ \t]*sub[ \t-]*total[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)
_TAX = re.compile(rf"^[ \t]*(?:sales[ \t]+)?tax(?:[ \t]*\([^)]*\))?[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)
_SHIPPING = re.compile(rf"^[ \t]*shipping[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)

# "WidgetA   qty: 10   unit price: $250.00", "GadgetX  qty 20  @ $750 ea", "- SuperGizmo   x12   $400.00 each"
_ITEM_QTY = re.compile(r"^[ \t]*(?:[-•*][ \t]*)?(?P<name>[A-Za-z][\w .\-/]*?)[ \t]+(?:qty:?[ \t]*|x(?=\d))(?P<qty>-?\d+)\b(?P<rest>.*)$", re.I)
# "WidgetA (rush order)   4   $300.00   $1,200.00 note"
_ITEM_ROW = re.compile(rf"^[ \t]*(?P<name>[A-Za-z][^\d$]*?)[ \t]+(?P<qty>-?\d+)[ \t]+\$?(?P<price>{_NUM})[ \t]+\$?(?P<amt>{_NUM})(?P<note>.*)$", re.I)
_MONEY = re.compile(rf"\$[ \t]*({_NUM})")


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None)


def _field(pattern: re.Pattern[str], text: str) -> str | None:
    """First match per line, cut at the next wide gap (columns on the same row)."""
    for line in text.splitlines():
        m = pattern.search(line)
        if m:
            return re.split(r"[ \t]{2,}", m.group(1).strip())[0].strip() or None
    return None


def _clean_name(raw: str) -> tuple[str, str | None]:
    name, note = raw.strip(), None
    m = re.search(r"\s*\(([^)]*)\)\s*$", name)
    if m:
        name, note = name[: m.start()].strip(), m.group(1).strip()
    return name, note


def _vendor(text: str) -> tuple[str | None, str | None]:
    raw = _first(_VENDOR, text)
    if raw is None:
        m = _FROM.search(text)
        if m and "@" not in m.group(1):
            raw = m.group(1)
            nxt = text[m.end():].lstrip("\n").split("\n", 1)[0]
            if _FORMERLY.search(nxt):
                return re.split(r"[ \t]{2,}", raw.strip())[0], nxt.strip().strip("()")
    return (re.split(r"[ \t]{2,}", raw.strip())[0] if raw else None), None


def extract(text: str, fmt: str = "text") -> Invoice:
    warnings: list[str] = []
    vendor, alias_note = _vendor(text)
    due_raw = _field(_DUE, text)
    date_raw = _field(_DATE, text)
    due = parse_date(due_raw)
    if due_raw and not due:
        warnings.append(f"unparseable due date {due_raw!r}")
    if date_raw and not parse_date(date_raw):
        warnings.append(f"unparseable invoice date {date_raw!r}")

    items: list[LineItem] = []
    for line in text.splitlines():
        low = line.strip().lower()
        head = low.split(":")[0].split()
        if not head or head[0] in _LABEL_WORDS:  # blank, or noise like a lone ":" from OCR
            continue
        m = _ITEM_QTY.match(line)
        if m:
            prices = [parse_money(p) for p in _MONEY.findall(m.group("rest"))]
            name, note = _clean_name(m.group("name"))
            items.append(LineItem(item=name, quantity=int(m.group("qty")),
                                  unit_price=prices[0] if prices else None, note=note))
            continue
        m = _ITEM_ROW.match(line)
        if m and m.group("name").strip().lower() not in _LABEL_WORDS:
            name, note = _clean_name(m.group("name"))
            tail = m.group("note").strip() or None
            items.append(LineItem(item=name, quantity=int(m.group("qty")),
                                  unit_price=parse_money(m.group("price")),
                                  amount=parse_money(m.group("amt")), note=tail or note))

    notes = _first(_NOTES, text)
    if alias_note:
        notes = f"{notes + '; ' if notes else ''}{alias_note}"
    terms_raw = _first(_TERMS, text)
    number = _first(_INV_NO, text)
    if number is None:
        warnings.append("invoice number not found")
    return Invoice(
        invoice_number=normalize_invoice_number(number),
        vendor=vendor,
        date=parse_date(date_raw),
        due_date=due,
        currency=detect_currency(text),
        line_items=items,
        subtotal=_dec(_first(_SUBTOTAL, text)),
        tax_amount=_dec(_first(_TAX, text)),
        shipping=_dec(_first(_SHIPPING, text)),
        total=_dec(_first(_TOTAL, text)),
        payment_terms=terms_raw.strip() if terms_raw else None,
        notes=notes,
        channel="unstructured",
        extractor=f"heuristic:{fmt}",
        parse_warnings=warnings,
    )


def _dec(raw: str | None) -> Decimal | None:
    return parse_money(fix_ocr_digits(raw)) if raw else None
