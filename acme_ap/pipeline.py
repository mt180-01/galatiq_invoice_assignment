"""Concurrent, CPU-aware pipeline: the stages one invoice goes through (ingest, validate, decide, pay, notify, settle). Reading documents lives in parsepool.py.

Work is split by what it is bound on:
  * CPU-bound  (PDF text extraction, regex/JSON/XML parsing)  -> ProcessPoolExecutor, one worker per core
  * I/O-bound  (Grok calls, ERP/SQLite)                       -> asyncio + bounded concurrency
Stages are chained per document with no global barrier, so as soon as a worker finishes parsing a
file the next stage starts for it while the cores move on to the next file: cores stay busy while
network calls are in flight, instead of the batch waiting stage by stage.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from .agents.team import CommsAgent, DeskAgent, IntakeAgent, PaymentAgent, ReviewerAgent, ValidationAgent, event_dict, step
from .erp import ErpAdapter
from .approvals.tokens import evidence_hash
from .identity import bill_key
from .ingestion import heuristic
from .intake import Origin
from .llm import LLM, invoice_ledger
from .parsepool import ParsePool, plan_workers
from .payments import default_payments
from .models import Approval, Decision, Finding, Outcome, Severity

log = logging.getLogger("acme.pipeline")
decision_log = logging.getLogger("acme.decision")  # one line per invoice: shown by default, the rest only with -v

class Pipeline:
    def __init__(self, erp: ErpAdapter, llm: LLM, workers: int | None = None, llm_concurrency: int = 8,
                 origins: dict[str, Origin] | None = None, approvals=None, payments=None, bank_concurrency: int = 8,
                 llm_policy: str = "gated", store=None, sequential: bool = False):
        self.erp, self.llm = erp, llm
        heuristic.set_known_vendors(erp.vendor_names())  # for documents parsed in this process; each worker process is given the same list
        self.origins = origins or {}
        self.approvals = approvals
        self.sequential = sequential  # one document at a time, in the order given: slower, but repeatable (which copy of a duplicate is blocked, what a recording replays)
        self.store = store  # optional: keeps each finished outcome for the web interface
        self.payments = payments or getattr(approvals, "payments", None) or default_payments(erp)
        gated = llm_policy == "gated"  # "always" = ask the model on every invoice (the pre-optimisation behaviour, kept for comparison)
        self.intake, self.validator = IntakeAgent(llm, gated), ValidationAgent(erp, self.payments, llm)
        self.reviewer, self.desk = ReviewerAgent(llm, erp, gated), DeskAgent(approvals)
        self.payer, self.comms = PaymentAgent(self.payments, bank_concurrency), CommsAgent(llm)
        self.workers = workers  # None = decide from the amount of PDF work (see _plan_workers); an int is an explicit choice
        self.llm_sem = asyncio.Semaphore(llm_concurrency)

    def _plan_workers(self, n_pdfs: int) -> int:
        return plan_workers(n_pdfs, self.workers)

    async def run(self, paths: list[str]) -> list[Outcome]:
        parsing = ParsePool(paths, self.llm.available, self.workers, self.erp.vendor_names())
        self.pool_workers = parsing.pool_workers
        futures = parsing.futures
        try:
            if self.sequential:
                return [await self._process(p, *futures[p]) for p in paths]
            return list(await asyncio.gather(*(self._process(p, *futures[p]) for p in paths)))
        finally:
            if self.store is not None:
                await asyncio.to_thread(self.store.flush)  # a batch is not over until everything in it is on disk
            parsing.close()

    async def _write_together(self, out: Outcome) -> tuple[str | None, str | None]:
        """Run the plain-English note and the vendor email at the same time. Each job keeps its own record of model use (a task's context is its own),
        merged in a fixed order afterwards, so the trace shows exactly whose calls were whose and a replay is unaffected by which finished first."""
        async def job(action: str, write):
            mine: list = []
            invoice_ledger.set(mine)
            started = time.perf_counter()
            async with self.llm_sem:
                text = await write(out)
            return action, text, mine, round((time.perf_counter() - started) * 1000, 2)

        jobs = [("drafted vendor follow-up", self.comms.followup)]
        if out.approval.decision is not Decision.APPROVE:
            jobs.insert(0, ("explained in plain English", self.comms.explain))
        parent, texts = invoice_ledger.get(), {}
        for action, text, mine, ms in await asyncio.gather(*(job(a, w) for a, w in jobs)):
            events = [event_dict(e) for e in mine]
            if parent is not None:
                parent.extend(mine)
            if text is not None or events:  # in the trace only if it did something: a model call, or text produced
                out.trace.append({"agent": "Comms", "action": action, "ms": ms, **({"events": events} if events else {})})
            texts[action] = text
        return texts.get("explained in plain English"), texts.get("drafted vendor follow-up")

    async def _process(self, path: str, parse_future: asyncio.Future, idx: int) -> Outcome:
        """One invoice start to finish. The finishing touches (what the model cost, the stored record) happen for every outcome,
        including duplicates and unreadable files, which leave _process_one early."""
        ledger: list = []
        invoice_ledger.set(ledger)  # this task's model use, summed into out.llm at the end
        out = await self._process_one(path, parse_future, idx)
        out.llm = {"calls": sum(e[0] == "call" for e in ledger), "cached": sum(e[0] == "cached" for e in ledger),
                   "skipped": [e[1] for e in ledger if e[0] == "skip"], "tools": sum(e[0] == "tool" for e in ledger),
                   "tokens_in": sum(e[2] for e in ledger), "tokens_out": sum(e[3] for e in ledger),
                   "cost_ticks": sum(e[4] for e in ledger)} if ledger else {}
        if self.store is not None and (out.invoice is not None or out.error):
            try:
                self.store.save(out, bill_key(out.invoice, self.erp) if out.invoice else None)  # buffered: an append, not a database write
            except Exception:  # noqa: BLE001 - keeping a record must never fail an invoice
                log.exception("could not store the outcome")
        return out

    async def _process_one(self, path: str, parse_future: asyncio.Future, idx: int) -> Outcome:
        """The stages in order. Each stage method fills in `out`; one that cannot continue (unreadable file, duplicate) returns None."""
        origin = self.origins.get(path)
        out = Outcome(source_path=path, sender=origin.sender if origin else None, subject=origin.subject if origin else None)
        clock = _Lap(out)
        trace = os.path.basename(path)
        try:
            if (key := await self._ingest(out, path, trace, parse_future, idx, clock)) is None:
                return out
            await self._validate_and_decide(out, trace, clock)
            payment_row, pay_status = await self._pay(out, key)
            await self._notify_and_settle(out, key, payment_row, pay_status, clock)
        except Exception as exc:  # noqa: BLE001 - one bad invoice never stops the batch
            out.error = f"{type(exc).__name__}: {exc}"
            log.exception("unhandled error", extra={"trace": trace, "stage": "pipeline", "detail": out.error})
        return out

    async def _ingest(self, out: Outcome, path: str, trace: str, parse_future: asyncio.Future, idx: int, clock: _Lap) -> str | None:
        """Parse, let the Intake agent refine a doubtful reading, and claim the invoice in the ledger. Returns the ledger key, or None when
        there is nothing more to do (the file is unreadable, or it is a duplicate, which is recorded as a rejection)."""
        kind, payload = (await parse_future)[idx]
        if kind == "err":
            out.error = f"could not read document: {payload}"
            log.error("parse failed", extra={"trace": trace, "stage": "ingest", "detail": payload})
            return None
        inv, text = payload
        clock.lap("parse")
        async with step(out, "Intake", f"read {inv.source_format or 'document'} via {inv.extractor}"):
            async with self.llm_sem:
                inv = await self.intake.refine(inv, text)
        clock.lap("llm_extract")
        out.invoice = inv
        log.info("ingested", extra={"trace": trace, "stage": "ingest", "detail": f"{inv.invoice_number} via {inv.extractor} ({inv.channel} lane)"})
        key = bill_key(inv, self.erp)
        first = await asyncio.to_thread(self.erp.claim_bill, key, path, inv.vendor, inv.total, evidence_hash(inv))
        if first:
            out.findings = [Finding(code="duplicate_invoice", severity=Severity.ERROR,
                                    message=f"already received from {os.path.basename(first)}; not processed twice")]
            out.approval = Approval(decision=Decision.REJECT, reasoning=out.findings[0].message)
            return None
        return key

    async def _validate_and_decide(self, out: Outcome, trace: str, clock: _Lap) -> None:
        inv = out.invoice
        # Local ERP reads take microseconds; a real (network) ERP adapter would be awaited here instead.
        async with step(out, "Validation", "checked against ERP and policy"):
            out.findings = await self.validator.check(inv)
        clock.lap("validate")
        for f in out.findings:
            log.info("finding", extra={"trace": trace, "stage": "validate", "detail": f"{f.severity.value}:{f.code} {f.message}"})
        async with step(out, "Reviewer", "decided"):
            async with self.llm_sem:
                out.approval = await self.reviewer.decide(inv, out.findings)
        clock.lap("approve")
        decision_log.info("decision", extra={"trace": trace, "stage": "approve",
                                             "detail": f"{out.approval.decision.value} after {out.approval.rounds} round(s): {out.approval.reasoning}"})

    async def _pay(self, out: Outcome, key: str) -> tuple[tuple | None, str | None]:
        """Reserve stock (opt-in policy), then schedule or send the payment. Returns the ledger row to record and the ERP status, if any."""
        inv = out.invoice
        payment_row, pay_status = None, None
        if out.approval.decision is Decision.APPROVE and getattr(self.erp, "stock_policy", "cap") == "reserve":
            # Cumulative stock control (opt-in): the units this invoice consumes are set aside atomically before payment, so two invoices that each
            # fit in stock cannot both be approved when together they do not.
            short = await asyncio.to_thread(self.erp.reserve_stock, key, [(li.item, li.quantity) for li in inv.line_items])
            if short:
                out.findings.append(Finding(code="insufficient_stock", severity=Severity.ERROR, message="; ".join(short)))
                out.approval = Approval(decision=Decision.REJECT, reasoning="Rejected: " + "; ".join(short))
        if out.approval.decision is Decision.APPROVE:
            async with step(out, "Payment", "scheduled / sent"):
                res = await self.payer.pay(inv, key)
            out.payment_state, out.release_on, pay_status = res.state, res.release_on, res.erp_status
            if res.submitted:
                out.paid, out.payment_ref, out.payment_amount = True, res.ref, res.amount
                payment_row = (inv.vendor or "", res.amount, res.ref)
        return payment_row, pay_status

    async def _notify_and_settle(self, out: Outcome, key: str, payment_row, pay_status: str | None, clock: _Lap) -> None:
        if self.llm.available:
            # The note for the people who must act and the email to the vendor depend only on the decision, so they are written together.
            out.explanation, out.followup = await self._write_together(out)
        request = None
        if out.approval.decision is Decision.ESCALATE and self.approvals is not None:
            async with step(out, "Desk", "routed to a person"):
                request = await self.desk.request(out, key)
        status = pay_status or ("pending_approval" if request else out.approval.decision.value)
        await asyncio.to_thread(self.erp.settle_bill, key, status, out.approval.reasoning, payment_row)
        clock.lap("settle")
        if not self.llm.available:
            out.followup = await self.comms.followup(out)


class _Lap:
    """Records how long each stage took, into the outcome."""

    def __init__(self, out: Outcome):
        self.out, self.t = out, time.perf_counter()

    def lap(self, stage: str) -> None:
        now = time.perf_counter()
        self.out.timings_ms[stage] = round((now - self.t) * 1000, 2)
        self.t = now
