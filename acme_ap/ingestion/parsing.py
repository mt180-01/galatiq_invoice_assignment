"""Forgiving number/date parsing for messy invoice text (OCR 'O' for '0', odd formats)."""
from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from decimal import Decimal, InvalidOperation

_DATE_FORMATS = ("%Y-%m-%d", "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y")


def normalize_account(raw: object) -> str | None:
    text = re.sub(r"\s+", "", str(raw)).upper() if raw else ""
    return text or None


def fix_ocr_digits(token: str) -> str:
    """'3,500.O0' -> '3,500.00'; only touches O/o that sit next to a digit or separator."""
    return re.sub(r"(?<=[\d.,])[Oo]|[Oo](?=[\d.,])", "0", token)


def parse_money(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    cleaned = fix_ocr_digits(str(value)).replace("$", "").replace("€", "").replace(",", "").strip()
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_date(value: object) -> str | None:
    """Return an ISO date or None (e.g. 'yesterday' is deliberately not guessed)."""
    if not value:
        return None
    return _parse_date(str(value))


@lru_cache(maxsize=4096)
def _parse_date(raw: str) -> str | None:
    text = fix_ocr_digits(raw.strip())
    text = re.sub(r"\s+", " ", text)
    if _ISO.fullmatch(text):  # the common case: skip trying seven other formats
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalize_invoice_number(raw: str | None) -> str | None:
    if not raw:
        return None
    m = re.search(r"(\d{3,})", raw)
    return f"INV-{m.group(1)}" if m else raw.strip()


def detect_currency(text: str) -> str:
    if re.search(r"\bEUR\b|€", text):
        return "EUR"
    if re.search(r"\bGBP\b|£", text):
        return "GBP"
    return "USD"
