"""What makes two invoices the same bill."""
from __future__ import annotations

import hashlib

from .erp import ErpAdapter
from .erp.mock_netsuite import norm
from .models import Invoice


def order_fingerprint(inv: Invoice, erp: ErpAdapter | None = None) -> str | None:
    """The same order, whatever number it is billed under: vendor, total and what was bought. Two invoices with one fingerprint and different
    numbers are a repeat of an order, which is how a bill gets paid twice."""
    if not (inv.vendor and inv.total is not None and inv.line_items):
        return None
    record = erp.lookup_vendor(inv.vendor) if erp is not None else None
    items = sorted((norm(li.item), li.quantity) for li in inv.line_items)
    return hashlib.sha1(repr((norm(record.name if record else inv.vendor), str(inv.total), items)).encode()).hexdigest()[:16]


def bill_key(inv: Invoice, erp: ErpAdapter | None = None) -> str:
    """vendor | invoice number | revision. The vendor is the master-data name when the ERP knows it (so "FastShip Ltd." and its legal name are one
    vendor, and a respelling that normalises to the same name is too); otherwise the normalised text as written."""
    if inv.invoice_number:
        record = erp.lookup_vendor(inv.vendor) if erp is not None and inv.vendor else None
        vendor = norm(record.name if record else (inv.vendor or ""))
        return f"{vendor}|{inv.invoice_number}|{inv.revision or ''}"
    return "anon|" + hashlib.sha1(repr(inv.model_dump(mode="json", exclude={"source_path", "source_format"})).encode()).hexdigest()[:12]
