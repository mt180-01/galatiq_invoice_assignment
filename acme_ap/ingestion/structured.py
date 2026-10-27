"""Deterministic parsers for structured inputs (JSON / XML / CSV).

This is the "e-invoice lane": when a vendor sends machine-readable data no LLM is needed,
which is both cheaper and more accurate than reading a PDF.
"""
from __future__ import annotations

import csv
import io
import json
import xml.etree.ElementTree as ET
from decimal import Decimal

from ..models import Invoice, LineItem
from .parsing import normalize_invoice_number, parse_date, parse_money


def _qty(raw: object, warnings: list[str]) -> int:
    try:
        dec = Decimal(str(raw).strip())
    except Exception:
        warnings.append(f"unreadable quantity {raw!r}")
        return 0
    if dec != dec.to_integral_value():
        warnings.append(f"non-integer quantity {raw!r} rounded")
    return int(dec.to_integral_value())


def _clean(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def from_json(text: str) -> Invoice:
    data = json.loads(text)
    warnings: list[str] = []
    vendor = data.get("vendor")
    if isinstance(vendor, dict):
        vendor = vendor.get("name")
    items = [
        LineItem(
            item=str(li.get("item") or li.get("name") or "").strip(),
            quantity=_qty(li.get("quantity"), warnings),
            unit_price=parse_money(li.get("unit_price")),
            amount=parse_money(li.get("amount")),
            note=li.get("note"),
        )
        for li in data.get("line_items", [])
    ]
    return Invoice(
        invoice_number=normalize_invoice_number(_clean(data.get("invoice_number"))),
        revision=_clean(data.get("revision")),
        vendor=_clean(vendor),
        date=parse_date(data.get("date")),
        due_date=parse_date(data.get("due_date")),
        currency=data.get("currency") or "USD",
        line_items=items,
        subtotal=parse_money(data.get("subtotal")),
        tax_amount=parse_money(data.get("tax_amount")),
        total=parse_money(data.get("total")),
        payment_terms=_clean(data.get("payment_terms")),
        notes=_clean(data.get("notes")),
        channel="structured",
        extractor="json",
        parse_warnings=warnings,
    )


def from_xml(text: str) -> Invoice:
    root = ET.fromstring(text)
    warnings: list[str] = []

    def find(path: str) -> str | None:
        node = root.find(path)
        return _clean(node.text) if node is not None else None

    items = [
        LineItem(
            item=(node.findtext("name") or node.findtext("item") or "").strip(),
            quantity=_qty(node.findtext("quantity"), warnings),
            unit_price=parse_money(node.findtext("unit_price")),
            amount=parse_money(node.findtext("amount")),
        )
        for node in root.findall("./line_items/item")
    ]
    return Invoice(
        invoice_number=normalize_invoice_number(find("./header/invoice_number")),
        vendor=find("./header/vendor"),
        date=parse_date(find("./header/date")),
        due_date=parse_date(find("./header/due_date")),
        currency=find("./header/currency") or "USD",
        line_items=items,
        subtotal=parse_money(find("./totals/subtotal")),
        tax_amount=parse_money(find("./totals/tax_amount")),
        total=parse_money(find("./totals/total")),
        payment_terms=find("./payment_terms"),
        channel="structured",
        extractor="xml",
        parse_warnings=warnings,
    )


def from_csv(text: str) -> Invoice:
    rows = list(csv.reader(io.StringIO(text)))
    warnings: list[str] = []
    if rows and [c.strip().lower() for c in rows[0][:2]] == ["field", "value"]:
        return _csv_key_value(rows[1:], warnings)
    return _csv_tabular(rows, warnings)


def _csv_key_value(rows: list[list[str]], warnings: list[str]) -> Invoice:
    header: dict[str, str] = {}
    items: list[LineItem] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if current.get("item"):
            items.append(LineItem(
                item=current["item"], quantity=_qty(current.get("quantity", 0), warnings),
                unit_price=parse_money(current.get("unit_price")),
            ))
        current.clear()

    for row in rows:
        if len(row) < 2:
            continue
        key, value = row[0].strip().lower(), row[1].strip()
        if key == "item":
            flush()
            current["item"] = value
        elif key in ("quantity", "unit_price"):
            current[key] = value
        else:
            header[key] = value
    flush()
    return Invoice(
        invoice_number=normalize_invoice_number(header.get("invoice_number")),
        vendor=_clean(header.get("vendor")),
        date=parse_date(header.get("date")),
        due_date=parse_date(header.get("due_date")),
        line_items=items,
        subtotal=parse_money(header.get("subtotal")),
        tax_amount=parse_money(header.get("tax")),
        total=parse_money(header.get("total")),
        payment_terms=_clean(header.get("payment_terms")),
        channel="structured", extractor="csv", parse_warnings=warnings,
    )


def _csv_tabular(rows: list[list[str]], warnings: list[str]) -> Invoice:
    head = [c.strip().lower() for c in rows[0]]
    col = {name: i for i, name in enumerate(head)}
    items: list[LineItem] = []
    meta: dict[str, str] = {}
    footer: dict[str, str] = {}
    for row in rows[1:]:
        row = row + [""] * (len(head) - len(row))
        if row[col["item"]].strip():
            for name in ("invoice number", "vendor", "date", "due date"):
                meta.setdefault(name, row[col[name]].strip())
            items.append(LineItem(
                item=row[col["item"]].strip(), quantity=_qty(row[col["qty"]], warnings),
                unit_price=parse_money(row[col["unit price"]]),
                amount=parse_money(row[col["line total"]]),
            ))
        else:
            label = row[col["unit price"]].strip().lower().rstrip(":")
            if label:
                footer[label] = row[col["line total"]].strip()
    tax_key = next((k for k in footer if k.startswith("tax")), None)
    return Invoice(
        invoice_number=normalize_invoice_number(meta.get("invoice number")),
        vendor=_clean(meta.get("vendor")),
        date=parse_date(meta.get("date")),
        due_date=parse_date(meta.get("due date")),
        line_items=items,
        subtotal=parse_money(footer.get("subtotal")),
        tax_amount=parse_money(footer.get(tax_key)) if tax_key else None,
        total=parse_money(footer.get("total")),
        channel="structured", extractor="csv", parse_warnings=warnings,
    )
