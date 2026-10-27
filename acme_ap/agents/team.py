"""The team: six agents, each with one job, its own tools, and a rule for when it may spend model tokens.

  Intake      reads any document into an Invoice           regex/parsers first; model only when the reading is in doubt
  Validation  checks the Invoice against the ERP           never uses a model: rules and lookups are exact and free
  Reviewer    second opinion on invoices policy approves   model only where it can change the outcome (risk-gated)
  Desk        gets a human decision when one is required   never uses a model: routing, signed tokens, reminders
  Payment     schedules and sends money                    never uses a model: money moves by rules only
  Comms       tells vendors and the AP team what happened  never uses a model: templates from the facts

Agents are thin on purpose: the logic lives in modules that are tested on their own. What an agent adds is a clear contract
(`CHARTERS`), a trace entry per invoice (who did what, how long, how many model calls), and one place where its token policy lives.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal

from ..erp import ErpAdapter
from ..ingestion import llm_extract
from ..llm import LLM
from .tools import reviewer_tools
from ..models import Approval, Finding, Invoice, Outcome
from ..outreach import draft_followup
from . import approval, validation


@dataclass(frozen=True)
class Charter:
    name: str
    job: str
    tools: str
    model_use: str       # when (if ever) it may call the LLM
    hands_to: str


CHARTERS = [
    Charter("Intake", "Turn any document (PDF, scan, email, JSON/XML/CSV/UBL, text) into a structured Invoice.",
            "format parsers, PDF text/OCR, regex reader, Grok reader with self-check", "Only when the regex reading is incomplete, "
            "inconsistent or has warnings; the answer is cross-checked field by field against the regex reading.", "Validation"),
    Charter("Validation", "Check the Invoice against the ERP and policy: vendor, items, stock, arithmetic, dates, fraud signals, bank account.",
            "ERP lookups, rule engine", "Never.", "Reviewer"),
    Charter("Reviewer", "Make the approve / escalate / reject call. Policy sets the floor; the model may only make it stricter.",
            "policy engine, rule-based critic, Grok draft + challenger; Grok may call read-only tools (inventory, vendor master, vendor payment history, limits)", "Only when policy would approve AND there is a reason for a second "
            "opinion (warning, half the sign-off limit, an AI-read document, or an amount far above the vendor's history). Challenger only if the draft departs from policy.", "Desk or Payment"),
    Charter("Desk", "When a person must decide: pick the right approver, send a one-click signed card, remind, escalate, expire.",
            "routing table, signed tokens, approval ledger", "Never.", "Payment (on approval)"),
    Charter("Payment", "Schedule to the due date or capture a discount, check the payee's bank account, send idempotently, reconcile.",
            "payment ledger, bank adapter", "Never.", "Comms"),
    Charter("Comms", "Tell people what is happening: one question to a vendor when fixable, remittance notices, the AP digest.",
            "templates over the recorded facts", "Never.", "(end)"),
]


@asynccontextmanager
async def step(out: Outcome, agent: str, action: str):
    """Records who did what, for how long, in the invoice's trace."""
    t = time.perf_counter()
    try:
        yield
    finally:
        out.trace.append({"agent": agent, "action": action, "ms": round((time.perf_counter() - t) * 1000, 2)})


class IntakeAgent:
    name = "Intake"

    def __init__(self, llm: LLM, gated: bool = True):
        self.llm, self.gated = llm, gated

    async def refine(self, inv: Invoice, text: str) -> Invoice:
        if self.llm.available and inv.channel == "unstructured":
            return await llm_extract.extract(text, inv, self.llm, gate=self.gated)
        return inv


class ValidationAgent:
    name = "Validation"

    def __init__(self, erp: ErpAdapter, payments):
        self.erp, self.payments = erp, payments

    def check(self, inv: Invoice) -> list[Finding]:
        return validation.validate(inv, self.erp, self.payments)


class ReviewerAgent:
    name = "Reviewer"

    def __init__(self, llm: LLM, erp: ErpAdapter, gated: bool = True):
        self.llm, self.erp, self.gated = llm, erp, gated
        self.tools = reviewer_tools(erp)

    def _usual_amount(self, inv: Invoice) -> str | None:
        """A cheap local look at the vendor's history; an amount far above anything they have billed earns a second opinion."""
        if not (self.llm.available and inv.vendor):  # only worth a query if a model could act on it
            return None
        hist = self.erp.vendor_history(inv.vendor)
        if hist["bills"] >= 3 and inv.total and inv.total > 2 * Decimal(hist["max_total"]):
            return f"total {inv.total} is more than twice the largest of this vendor's {hist['bills']} earlier invoices ({hist['max_total']})"
        return None

    async def decide(self, inv: Invoice, findings: list[Finding]) -> Approval:
        return await approval.decide(inv, findings, self.llm, gate=self.gated, tools=self.tools, risk_hint=self._usual_amount(inv))


class DeskAgent:
    name = "Desk"

    def __init__(self, approvals):
        self.approvals = approvals

    async def request(self, out: Outcome, key: str):
        if self.approvals is None:
            return None
        req = await asyncio.to_thread(self.approvals.open_request, out, key)
        if req:
            out.approval_request, out.awaiting = req[0], req[1].role
            out.approval_card = await asyncio.to_thread(self.approvals.render_card, req[0])
        return req


class PaymentAgent:
    name = "Payment"

    def __init__(self, payments, concurrency: int = 8):
        self.payments = payments
        self._sem = asyncio.Semaphore(concurrency)  # banks rate-limit; many submissions still overlap

    async def pay(self, inv: Invoice, key: str):
        async with self._sem:
            return await asyncio.to_thread(self.payments.pay, inv, key)


class CommsAgent:
    name = "Comms"

    @staticmethod
    def followup(out: Outcome) -> str | None:
        return draft_followup(out)
