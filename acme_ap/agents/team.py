"""The team: six agents, each with one job, its own tools, and a rule for when it may spend model tokens.

  Intake      reads any document into an Invoice           regex/parsers first; model only when the reading is in doubt
  Validation  checks the Invoice against the ERP           rules; a resolver searches (with tools) only for names it cannot match
  Reviewer    second opinion before any payment            model + tools on every invoice about to be paid; never on rejects
  Desk        gets a human decision when one is required   never uses a model: routing, signed tokens, reminders
  Payment     schedules and sends money                    never uses a model: money moves by rules only
  Comms       tells vendors and the AP team what happened  model only phrases; facts and who to contact come from rules, text is checked

Agents are thin on purpose: the logic lives in modules that are tested on their own. What an agent adds is a clear contract
(`CHARTERS`), a trace entry per invoice (who did what, how long, how many model calls), and one place where its token policy lives.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal

from ..erp import ErpAdapter
from ..ingestion import llm_extract
from ..llm import LLM, invoice_ledger
from .tools import reviewer_tools
from ..models import Approval, Finding, Invoice, Outcome
from .comms import Comms
from . import approval, resolver, validation


log = logging.getLogger("acme.agents")


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
    Charter("Validation", "Check the Invoice against the ERP and policy: vendor, items, stock, arithmetic, dates, fraud signals, bank account. "
            "When a name cannot be matched, a resolver searches the catalogue and re-checks.",
            "ERP lookups, rule engine, catalogue and vendor search tools", "Only when the rules find an item or vendor they cannot match. Items are "
            "corrected only if the pick exists and is textually very close; vendors are never corrected, only flagged with a suggestion.", "Reviewer"),
    Charter("Reviewer", "Make the approve / escalate / reject call. Policy sets the floor; the model may only make it stricter.",
            "policy engine, rule-based critic, Grok draft + challenger; Grok may call read-only tools (inventory, vendor master, vendor payment history, limits)", "On every invoice policy "
            "would approve, i.e. before any money moves (never on rejects or anything already with a person: it can only tighten, and a person will look). "
            "The challenger runs only if the draft departs from policy.", "Desk or Payment"),
    Charter("Desk", "When a person must decide: pick the right approver, send a one-click signed card, remind, escalate, expire.",
            "routing table, signed tokens, approval ledger", "Never.", "Payment (on approval)"),
    Charter("Payment", "Schedule to the due date or capture a discount, check the payee's bank account, send idempotently, reconcile.",
            "payment ledger, bank adapter", "Never.", "Comms"),
    Charter("Comms", "Tell people what is happening: one question to a vendor when fixable, plain-English notes for the people who must act, "
            "remittance notices, the AP digest.", "templates over the recorded facts, model wording checked against the record",
            "Only to phrase text. The facts and whether anyone is contacted at all are decided by rules; any figure, link or address the model adds "
            "that is not in the record makes us discard its text and use the template.", "(end)"),
]


def event_dict(e: tuple) -> dict:
    kind, agent, tin, tout, _ticks, detail = e
    if kind == "tool":
        return {"kind": "tool", "name": detail["name"], "args": detail["args"], "result": detail["result"]}
    if kind in ("call", "cached"):
        return {"kind": "model" if kind == "call" else "cached", "as": agent, "tokens": [tin, tout], **({"model": detail["model"]} if detail.get("model") else {})}
    if kind == "skip":
        return {"kind": "skipped", "as": agent}
    return detail


@asynccontextmanager
async def step(out: Outcome, agent: str, action: str):
    """Records who did what, for how long, in the invoice's trace."""
    t, ledger = time.perf_counter(), invoice_ledger.get()
    start = len(ledger) if ledger is not None else 0
    try:
        yield
    finally:
        entry = {"agent": agent, "action": action, "ms": round((time.perf_counter() - t) * 1000, 2)}
        events = [event_dict(e) for e in (ledger[start:] if ledger is not None else [])]
        if events:
            entry["events"] = events  # what the model was asked, which tools it called, what it concluded
        out.trace.append(entry)


class IntakeAgent:
    name = "Intake"

    def __init__(self, llm: LLM, gated: bool = True):
        self.llm, self.gated = llm, gated

    async def refine(self, inv: Invoice, text: str) -> Invoice:
        if self.llm.available and inv.channel == "unstructured":
            return await llm_extract.extract(text, inv, self.llm, gate=self.gated)
        return inv


class ValidationAgent:
    """Rules first. If they find names they cannot match and a model is available, a resolver agent searches the catalogue (item typos are
    corrected and re-checked; vendor look-alikes are only flagged for the human), then the rules run again on what was learned."""
    name = "Validation"

    def __init__(self, erp: ErpAdapter, payments, llm: LLM | None = None):
        self.erp, self.payments, self.llm = erp, payments, llm

    async def check(self, inv: Invoice) -> list[Finding]:
        findings = validation.validate(inv, self.erp, self.payments)
        if not (self.llm and self.llm.available) or any(f.code in ("fraud_indicators", "vendor_blocked", "unreadable_document") for f in findings):
            return findings
        try:  # an optional helper: whatever goes wrong inside it, the rules' findings stand
            if any(f.code == "unknown_item" for f in findings):
                fixes = await resolver.correct_items(inv, self.erp, self.llm)
                if fixes:
                    findings = validation.validate(inv, self.erp, self.payments) + fixes  # re-validate what the correction changed
            if any(f.code == "unknown_vendor" for f in findings) and (advice := await resolver.suggest_vendor(inv, self.erp, self.llm)):
                findings = [f.model_copy(update={"message": f"{f.message}; {advice}"}) if f.code == "unknown_vendor" else f for f in findings]
        except Exception as exc:  # noqa: BLE001
            log.warning("resolver failed, using the rules' findings: %s: %s", type(exc).__name__, exc)
        return findings


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

    def __init__(self, llm: LLM):
        self.writer = Comms(llm)

    async def followup(self, out: Outcome) -> str | None:
        return await self.writer.followup(out)

    async def explain(self, out: Outcome) -> str | None:
        return await self.writer.explain(out)
