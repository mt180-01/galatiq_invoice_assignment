"""Regex extractor for unstructured invoice text (typos, OCR noise, free-form emails).

CPU-bound, no network: runs inside the worker processes. It is the offline fallback for the
LLM extractor and also the independent cross-check against it.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..models import Invoice, LineItem
from .parsing import detect_currency, fix_ocr_digits, normalize_account, normalize_invoice_number, parse_date, parse_money

_NUM = r"[\dOo][\dOo,]*(?:\.[\dOo]+)?"
_LABEL_WORDS = {"date", "due", "tax", "total", "invoice", "subtotal", "terms", "shipping", "amt", "ref", "po"}

_INV_NO = re.compile(r"\b(?:invoice|inv)\b[ \t]*(?:number|no\.?|#)?[ \t]*[:#]?[ \t]*((?:[A-Z]{1,4}[-/]?|INV[-\s]?)?\d{3,}(?:/[A-Z0-9]+)?)", re.I)
_VENDOR = re.compile(r"^[ \t]*(?:vendor|vndr|supplier|seller)[ \t]*:[ \t]*(.+)$", re.I | re.M)
_FROM = re.compile(r"^[ \t]*from[ \t]*:[ \t]*(.+)$", re.I | re.M)
_DATE_LIKE = r"(?=\d|[A-Za-z]{3,9}\.?[ \t]+\d)"  # what follows must start like a date, so "the due date on the invoice?" in prose is not one
_INVOICE_DATE = re.compile(rf"\b(?:invoice|inv)[ \t]+(?:date|dt)[ \t]*:?[ \t]*{_DATE_LIKE}(.+)", re.I)
_DATE = re.compile(rf"(?<![A-Za-z])(?<!due )(?<!sent )(?:date|dt)[ \t]*(?::[ \t]*|[ \t]+{_DATE_LIKE})(.+)", re.I)
_DUE = re.compile(rf"(?<!balance )(?<!amount )(?<!total )\bdue(?:[ \t]*(?:date|dt))?[ \t]*(?::[ \t]*|[ \t]+{_DATE_LIKE})(.+)", re.I)
_TERMS = re.compile(r"(?:payment|pymnt)[ \t]+terms[ \t]*:[ \t]*(.+)|^[ \t]*terms[ \t]*:[ \t]*(.+)", re.I | re.M)
_ACCOUNT = re.compile(r"^[ \t]*(?:bank[ \t]+)?(?:account|acct|a/c|iban)[ \t]*(?:no\.?|number|#)?[ \t]*[:#][ \t]*([A-Za-z0-9][A-Za-z0-9 \-]{3,33})[ \t]*$", re.I | re.M)
_NOTES = re.compile(r"^[ \t]*notes?[ \t]*:[ \t]*(.+)$", re.I | re.M)
_FORMERLY = re.compile(r"\(\s*formerly[^)]*\)", re.I)

_TOTAL = re.compile(rf"^[ \t]*(?:grand[ \t]+total|total[ \t]+amount|total[ \t]+due|balance[ \t]+due|amount[ \t]+due|amt[ \t]+due|total)(?:[ \t]*\([A-Za-z ]{{0,12}}\))?[ \t]*:?[ \t]*\$?[ \t]*({_NUM})(?:[ \t]*[A-Z]{{3}})?[ \t]*$", re.I | re.M)
# A bare "Amount" or "Amt" is a weaker signal (it is also a line item's column), used only when nothing is labelled as a total.
_TOTAL_WEAK = re.compile(rf"^[ \t]*(?:amt|amount)[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)
_SUBTOTAL = re.compile(rf"^[ \t]*sub[ \t-]*total[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)
_TAX = re.compile(rf"^[ \t]*(?:sales[ \t]+)?tax(?:[ \t]*\([^)]*\))?[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)
_SHIPPING = re.compile(rf"^[ \t]*shipping[ \t]*:?[ \t]*\$?[ \t]*({_NUM})[ \t]*$", re.I | re.M)

# "WidgetA   qty: 10   unit price: $250.00", "GadgetX  qty 20  @ $750 ea", "- SuperGizmo   x12   $400.00 each"
_ITEM_QTY = re.compile(r"^[ \t]*(?:[-•*][ \t]*)?(?P<name>[A-Za-z][\w .\-/]*?)[ \t]+(?:qty:?[ \t]*|x(?=\d))(?P<qty>-?\d+)\b(?P<rest>.*)$", re.I)
# "WidgetA (rush order)   4   $300.00   $1,200.00 note"
_ITEM_ROW = re.compile(rf"^[ \t]*(?P<name>[A-Za-z][^\d$]*?)[ \t]+(?P<qty>-?\d+)[ \t]+\$?(?P<price>{_NUM})[ \t]+\$?(?P<amt>{_NUM})(?P<note>.*)$", re.I)
# "5 x WidgetA @ $250.00", "WidgetB - 3 units @ $500", "2  WidgetA  at $250.00 each"
_ITEM_AT = [
    re.compile(rf"^[ \t]*(?:[-•*][ \t]*)?(?P<qty>\d+)[ \t]*x[ \t]+(?P<name>[A-Za-z][\w .\-/]*?)[ \t]*(?:@|at)[ \t]*\$?(?P<price>{_NUM})(?:[ \t]*(?:ea|each))?[ \t]*$", re.I),
    re.compile(rf"^[ \t]*(?:[-•*][ \t]*)?(?P<name>[A-Za-z][\w .\-/]*?)[ \t]*[-–][ \t]*(?P<qty>\d+)[ \t]*(?:units?|pcs|pieces)[ \t]*(?:@|at)[ \t]*\$?(?P<price>{_NUM})(?:[ \t]*(?:ea|each))?[ \t]*$", re.I),
    re.compile(rf"^[ \t]*(?P<qty>\d+)[ \t]+(?P<name>[A-Za-z][\w .\-/]*?)[ \t]+at[ \t]+\$?(?P<price>{_NUM})(?:[ \t]*(?:ea|each))?[ \t]*$", re.I),
]
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


def _invoice_number(text: str, warnings: list[str]) -> str | None:
    """Several things on a page look like invoice numbers ("see also invoice 31337"). Prefer the canonical INV-... form,
    then labelled ones; if the best candidates still disagree, say so rather than silently picking one."""
    found = []
    for m in _INV_NO.finditer(text):
        label, token = text[m.start():m.start(1)], m.group(1)
        found.append((2 if token.upper().startswith("INV") else 1 if re.search(r"[:#]|number|no\b", label, re.I) else 0, token))
    if not found:
        return None
    best = max(score for score, _ in found)
    tops = list(dict.fromkeys(normalize_invoice_number(tok) for score, tok in found if score == best))
    if len(tops) > 1:
        warnings.append(f"extractor disagreement (regex): several possible invoice numbers {', '.join(map(str, tops))}")
    return next(tok for score, tok in found if score == best)


NON_ITEM = re.compile(r"^\s*(?:shipping|freight|delivery|handling|postage|carriage|sales[ \t]+tax|tax|vat|discount|subtotal|sub-total|total|deposit|surcharge|fee)s?\b", re.I)


def _clean_name(raw: str) -> tuple[str, str | None]:
    """The product as the catalogue would name it, and any note: 'WidgetA (rush order)' and 'WidgetA - Premium widget with a 2-year warranty' are both item WidgetA."""
    name, note = raw.strip(), None
    m = re.search(r"\s*\(([^)]*)\)\s*$", name)
    if m:
        name, note = name[: m.start()].strip(), m.group(1).strip()
    for sep in (" - ", " – ", " — ", ": "):
        head, found, tail = name.partition(sep)
        if found and head.strip():
            name, note = head.strip(), note or tail.strip()[:80]
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


# Words that make a line a label with nothing after it, as PDF text extraction often leaves them ("Vendor:" then the value on the next line).
_FIELD_LABELS = (r"invoice[ \t]*(?:number|no\.?|#|date)|inv[ \t]*(?:number|no\.?|#|date)|date|due(?:[ \t]*date)?|vendor|supplier|seller|from|bill[ \t]*from|"
                 r"payment[ \t]*terms|terms|currency|sub-?total|tax(?:[ \t]*\([^)]*\))?|shipping|total(?:[ \t]*\([^)]*\))?|grand[ \t]*total|amount(?:[ \t]*due)?|balance[ \t]*due|"
                 r"item|product|description|quantity|qty|unit[ \t]*price|price|rate|po(?:[ \t]*number)?")
# Only genuine field labels are joined to the next line: a section header such as "Items:" or "Notes:" must keep the lines under it.
_LABEL_ONLY = re.compile(rf"^[ \t]*(?:{_FIELD_LABELS})[ \t]*:[ \t]*$", re.I)
_ITEM_KV = re.compile(r"^[ \t]*(?:item|product|description)[ \t]*:[ \t]*(?P<name>\S.*?)[ \t]*$", re.I)
_QTY_KV = re.compile(r"^[ \t]*(?:quantity|qty)[ \t]*:[ \t]*(?P<q>\d+)[ \t]*$", re.I)
_PRICE_KV = re.compile(rf"^[ \t]*(?:unit[ \t]*price|price|rate)[ \t]*:[ \t]*\$?(?P<p>{_NUM})[ \t]*$", re.I)
_COMPANY = re.compile(r"\b(?:inc|ltd|llc|corp|co|group|partners|supply|industries|company|gmbh|plc|sons)\b\.?", re.I)
KNOWN_VENDORS: list[str] = []  # the vendor master's names and aliases; set by the pipeline (and in each worker process), empty means "none known"


def set_known_vendors(names: list[str]) -> None:
    KNOWN_VENDORS[:] = sorted(set(names), key=len, reverse=True)  # longest first, so "Widgets Inc." wins over "Widgets"


def join_label_lines(text: str) -> str:
    """'Vendor:' on one line and its value on the next becomes 'Vendor: value'. Applied only to labels with nothing after them."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        if _LABEL_ONLY.match(lines[i]):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and not _LABEL_ONLY.match(lines[j]):
                out.append(f"{lines[i].rstrip().rstrip(':')}: {lines[j].strip()}")
                i = j + 1
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def vendor_from_context(text: str) -> str | None:
    """No 'Vendor:' label: a name from the vendor master anywhere in the text, else a company-looking letterhead on the first lines."""
    low = text.lower()
    for name in KNOWN_VENDORS:
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(name.lower())}(?![A-Za-z0-9])", low):
            return name
    for line in [l.strip() for l in text.splitlines() if l.strip()][:4]:
        words = line.split()
        looks_like_a_name = len(words) <= 6 and all(w[0].isupper() or w.lower() in ("&", "and", "of", "the") for w in words)  # a sentence is not a letterhead
        if looks_like_a_name and len(line) <= 60 and _COMPANY.search(line) and not re.search(r"[@:]|\d{3,}|invoice", line, re.I):
            return re.sub(r"\s{2,}.*$", "", line)
    return None


def kv_items(lines: list[str]) -> list[LineItem]:
    """Items written as blocks of 'Item: X', 'Quantity: n', 'Unit Price: p' (what a one-cell-per-line PDF becomes)."""
    items, i = [], 0
    while i < len(lines):
        m = _ITEM_KV.match(lines[i])
        if m:
            qty = price = None
            for look in lines[i + 1:i + 4]:
                if (q := _QTY_KV.match(look)):
                    qty = int(q.group("q"))
                elif (p := _PRICE_KV.match(look)):
                    price = parse_money(p.group("p"))
            if qty is not None:
                name, note = _clean_name(m.group("name"))
                items.append(LineItem(item=name, quantity=qty, unit_price=price, note=note))
        i += 1
    return items


def extract(text: str, fmt: str = "text") -> Invoice:
    text = join_label_lines(text)
    warnings: list[str] = []
    vendor, alias_note = _vendor(text)
    vendor = vendor or vendor_from_context(text)
    due_raw = _field(_DUE, text)
    date_raw = _field(_INVOICE_DATE, text) or _field(_DATE, text)
    due = parse_date(due_raw)
    if due_raw and not due:
        warnings.append(f"unparseable due date {due_raw!r}")
    if date_raw and not parse_date(date_raw):
        warnings.append(f"unparseable invoice date {date_raw!r}")

    items: list[LineItem] = kv_items(text.splitlines())
    for line in ([] if items else text.splitlines()):
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
        if NON_ITEM.match(line):
            continue  # a shipping, tax or discount line is a charge, not a product
        m = next((hit for pat in _ITEM_AT if (hit := pat.match(line))), None)
        if m:
            name, note = _clean_name(m.group("name"))
            items.append(LineItem(item=name, quantity=int(m.group("qty")), unit_price=parse_money(m.group("price")), note=note))
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
    number = _invoice_number(text, warnings)
    if number is None:
        warnings.append("invoice number not found")
    totals = list(dict.fromkeys(_dec(m.group(1)) for m in _TOTAL.finditer(text)))  # labelled totals only; several different ones are ambiguous
    if len(totals) > 1:
        warnings.append(f"extractor disagreement (regex): several different totals {', '.join(map(str, totals))}")
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
        total=_dec(_first(_TOTAL, text) or _first(_TOTAL_WEAK, text)),
        payment_terms=terms_raw.strip() if terms_raw else None,
        bank_account=normalize_account(_first(_ACCOUNT, text)),
        notes=notes,
        channel="unstructured",
        extractor=f"heuristic:{fmt}",
        parse_warnings=warnings,
    )


def _dec(raw: str | None) -> Decimal | None:
    return parse_money(fix_ocr_digits(raw)) if raw else None
