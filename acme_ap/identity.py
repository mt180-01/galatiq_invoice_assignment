"""What makes two invoices the same bill."""
from __future__ import annotations

import hashlib

from .erp import ErpAdapter
from .erp.mock_netsuite import norm
from .models import Invoice


def bill_key(inv: Invoice, erp: ErpAdapter | None = None) -> str:
    """vendor | invoice number | revision. The vendor is the master-data name when the ERP knows it (so "FastShip Ltd." and its legal name are one
    vendor, and a respelling that normalises to the same name is too); otherwise the normalised text as written."""
    if inv.invoice_number:
        record = erp.lookup_vendor(inv.vendor) if erp is not None and inv.vendor else None
        vendor = norm(record.name if record else (inv.vendor or ""))
        return f"{vendor}|{inv.invoice_number}|{inv.revision or ''}"
    return "anon|" + hashlib.sha1(repr(inv.model_dump(mode="json", exclude={"source_path", "source_format"})).encode()).hexdigest()[:12]
