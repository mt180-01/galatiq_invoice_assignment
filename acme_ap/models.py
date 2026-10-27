"""Typed records that flow between pipeline stages."""
from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    item: str
    quantity: int
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    note: str | None = None


class Invoice(BaseModel):
    invoice_number: str | None = None
    revision: str | None = None
    vendor: str | None = None
    date: str | None = None
    due_date: str | None = None
    currency: str = "USD"
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    shipping: Decimal | None = None
    total: Decimal | None = None
    payment_terms: str | None = None
    notes: str | None = None
    # provenance
    source_path: str = ""
    source_format: str = ""
    channel: str = "unstructured"  # structured (e-invoice/API) | unstructured (PDF/text)
    extractor: str = ""
    parse_warnings: list[str] = Field(default_factory=list)


class Severity(str, Enum):
    ERROR = "error"  # blocks payment
    HOLD = "hold"  # needs a human; never auto-paid
    WARN = "warn"  # payable, but logged


class Finding(BaseModel):
    code: str
    severity: Severity
    message: str


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"


class Approval(BaseModel):
    decision: Decision
    reasoning: str
    rounds: int = 1
    critiques: list[str] = Field(default_factory=list)


class Outcome(BaseModel):
    source_path: str
    invoice: Invoice | None = None
    findings: list[Finding] = Field(default_factory=list)
    approval: Approval | None = None
    paid: bool = False
    payment_ref: str | None = None
    error: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.error:
            return "FAILED"
        if self.paid:
            return "PAID"
        if any(f.code == "duplicate_invoice" for f in self.findings):
            return "DUPLICATE"
        return (self.approval.decision.value if self.approval else "unknown").upper()
