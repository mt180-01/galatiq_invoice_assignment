"""Payment terms: "Net 30", "2/10 Net 30", "Due on receipt"."""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

_DISCOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%\s*)?/\s*(\d+)\s*,?\s*(?:net|n)\s*/?\s*(\d+)", re.I)
_NET = re.compile(r"net\s*(\d+)", re.I)
_NOW = re.compile(r"immediate|on receipt|upon receipt|\bcod\b|prepaid", re.I)


@dataclass(frozen=True)
class Terms:
    net_days: int | None = None
    discount_pct: Decimal = Decimal(0)
    discount_days: int = 0


def parse_terms(text: str | None) -> Terms:
    if not text:
        return Terms()
    if m := _DISCOUNT.search(text):
        return Terms(int(m.group(3)), Decimal(m.group(1)), int(m.group(2)))
    if m := _NET.search(text):
        return Terms(int(m.group(1)))
    return Terms(0) if _NOW.search(text) else Terms()
