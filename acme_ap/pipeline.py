"""Concurrent, CPU-aware pipeline.

Work is split by what it is bound on:
  * CPU-bound  (PDF text extraction, regex/JSON/XML parsing)  -> ProcessPoolExecutor, one worker per core
  * I/O-bound  (Grok calls, ERP/SQLite)                       -> asyncio + bounded concurrency
Stages are chained per document with no global barrier, so as soon as a worker finishes parsing a
file the next stage starts for it while the cores move on to the next file: cores stay busy while
network calls are in flight, instead of the batch waiting stage by stage.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal

from .agents.team import CommsAgent, DeskAgent, IntakeAgent, PaymentAgent, ReviewerAgent, ValidationAgent, step
from .erp import ErpAdapter
from .ingestion import parse_file
from .intake import Origin
from .llm import LLM, invoice_ledger
from .payments import default_payments
from .models import Approval, Decision, Finding, Invoice, Outcome, Severity

log = logging.getLogger("acme.pipeline")
decision_log = logging.getLogger("acme.decision")  # one line per invoice: shown by default, the rest only with -v
PDFS_PER_WORKER = 100


def _warm() -> None:  # pre-import heavy modules once per worker
    import pymupdf  # noqa: F401


def _parse_chunk(paths: list[str], want_text: bool) -> list[tuple[str, object]]:
    """Runs in a worker process. Never raises: a bad file must not kill the batch."""
    out: list[tuple[str, object]] = []
    for p in paths:
        try:
            inv, text = parse_file(p)
            out.append(("ok", (inv, text if want_text else "")))
        except Exception as exc:  # noqa: BLE001 - isolate per-file failures
            out.append(("err", f"{type(exc).__name__}: {exc}"))
    return out


def bill_key(inv: Invoice) -> str:
    if inv.invoice_number:
        vendor = "".join(c for c in (inv.vendor or "").lower() if c.isalnum())
        return f"{vendor}|{inv.invoice_number}|{inv.revision or ''}"
    return "anon|" + hashlib.sha1(repr(inv.model_dump(mode="json", exclude={"source_path", "source_format"})).encode()).hexdigest()[:12]


class Pipeline:
    def __init__(self, erp: ErpAdapter, llm: LLM, workers: int | None = None, llm_concurrency: int = 8,
                 origins: dict[str, Origin] | None = None, approvals=None, payments=None, bank_concurrency: int = 8,
                 llm_policy: str = "gated"):
        self.erp, self.llm = erp, llm
        self.origins = origins or {}
        self.approvals = approvals
        self.payments = payments or getattr(approvals, "payments", None) or default_payments(erp)
        gated = llm_policy == "gated"  # "always" = ask the model on every invoice (the pre-optimisation behaviour, kept for comparison)
        self.intake, self.validator = IntakeAgent(llm, gated), ValidationAgent(erp, self.payments, llm)
        self.reviewer, self.desk = ReviewerAgent(llm, erp, gated), DeskAgent(approvals)
        self.payer, self.comms = PaymentAgent(self.payments, bank_concurrency), CommsAgent(llm)
        self.workers = workers if workers is not None else (os.cpu_count() or 1)
        self.llm_sem = asyncio.Semaphore(llm_concurrency)

    def _plan_workers(self, n_pdfs: int) -> int:
        """Worker processes cost ~0.7s each to start (imports); they pay back only with enough PDF work.
        In a long-lived service the pool is reused, so this only matters for one-shot CLI runs."""
        if self.workers is not None:
            return self.workers
        return min(os.cpu_count() or 1, n_pdfs // PDFS_PER_WORKER)

    async def run(self, paths: list[str]) -> list[Outcome]:
        loop = asyncio.get_running_loop()
        want_text = self.llm.available
        heavy = [p for p in paths if p.lower().endswith(".pdf")]  # CPU-heavy: layout analysis
        workers = self._plan_workers(len(heavy))
        pool = ProcessPoolExecutor(workers, initializer=_warm) if workers > 1 and heavy else None
        self.pool_workers = workers if pool else 1
        # Cheap formats (txt/csv/json/xml, ~0.05 ms each) are parsed inline: shipping them to a worker
        # would cost more in IPC than the parse itself.
        futures: dict[str, tuple[asyncio.Future, int]] = {}
        chunk = max(1, min(32, len(heavy) // (workers * 4) or 1)) if pool else 1
        for i in range(0, len(heavy), chunk):
            part = heavy[i:i + chunk]
            fut = loop.run_in_executor(pool, _parse_chunk, part, want_text)
            for j, p in enumerate(part):
                futures[p] = (fut, j)
        for p in paths:
            if p not in futures:
                done = loop.create_future()
                done.set_result(_parse_chunk([p], want_text))
                futures[p] = (done, 0)
        try:
            return list(await asyncio.gather(*(self._process(p, *futures[p]) for p in paths)))
        finally:
            if pool:
                pool.shutdown(wait=True)

    async def _comms(self, out: Outcome, action: str, write):
        """Run a writing task; keep it in the trace only if it did something (a model call, or text produced)."""
        mark = len(out.trace)
        async with step(out, "Comms", action):
            async with self.llm_sem:
                text = await write(out)
        if text is None and not out.trace[mark].get("events"):
            out.trace.pop(mark)
        return text

    async def _process(self, path: str, parse_future: asyncio.Future, idx: int) -> Outcome:
        origin = self.origins.get(path)
        out, t = Outcome(source_path=path, sender=origin.sender if origin else None,
                         subject=origin.subject if origin else None), time.perf_counter()
        trace = os.path.basename(path)

        def lap(stage: str) -> None:
            nonlocal t
            now = time.perf_counter()
            out.timings_ms[stage] = round((now - t) * 1000, 2)
            t = now

        ledger: list = []
        invoice_ledger.set(ledger)  # this task's model use, summed into out.llm at the end
        try:
            kind, payload = (await parse_future)[idx]
            if kind == "err":
                out.error = f"could not read document: {payload}"
                log.error("parse failed", extra={"trace": trace, "stage": "ingest", "detail": payload})
                return out
            inv, text = payload
            lap("parse")
            async with step(out, "Intake", f"read {inv.source_format or 'document'} via {inv.extractor}"):
                async with self.llm_sem:
                    inv = await self.intake.refine(inv, text)
            lap("llm_extract")
            out.invoice = inv
            log.info("ingested", extra={"trace": trace, "stage": "ingest",
                                        "detail": f"{inv.invoice_number} via {inv.extractor} ({inv.channel} lane)"})

            key = bill_key(inv)
            first = await asyncio.to_thread(self.erp.claim_bill, key, path, inv.vendor, inv.total)
            if first:
                out.findings = [Finding(code="duplicate_invoice", severity=Severity.ERROR,
                                        message=f"already received from {os.path.basename(first)}; not processed twice")]
                out.approval = Approval(decision=Decision.REJECT, reasoning=out.findings[0].message)
                return out

            # Local ERP reads take microseconds; a real (network) ERP adapter would be awaited here instead.
            async with step(out, "Validation", "checked against ERP and policy"):
                out.findings = await self.validator.check(inv)
            lap("validate")
            for f in out.findings:
                log.info("finding", extra={"trace": trace, "stage": "validate", "detail": f"{f.severity.value}:{f.code} {f.message}"})

            async with step(out, "Reviewer", "decided"):
                async with self.llm_sem:
                    out.approval = await self.reviewer.decide(inv, out.findings)
            lap("approve")
            decision_log.info("decision", extra={"trace": trace, "stage": "approve",
                                        "detail": f"{out.approval.decision.value} after {out.approval.rounds} round(s): {out.approval.reasoning}"})

            payment_row, pay_status = None, None
            if out.approval.decision is Decision.APPROVE:
                async with step(out, "Payment", "scheduled / sent"):
                    res = await self.payer.pay(inv, key)
                out.payment_state, out.release_on, pay_status = res.state, res.release_on, res.erp_status
                if res.submitted:
                    out.paid, out.payment_ref, out.payment_amount = True, res.ref, res.amount
                    payment_row = (inv.vendor or "", res.amount, res.ref)
            if out.approval.decision is not Decision.APPROVE and self.llm.available:
                out.explanation = await self._comms(out, "explained in plain English", self.comms.explain)
            request = None
            if out.approval.decision is Decision.ESCALATE and self.approvals is not None:
                async with step(out, "Desk", "routed to a person"):
                    request = await self.desk.request(out, key)
            status = pay_status or ("pending_approval" if request else out.approval.decision.value)
            await asyncio.to_thread(self.erp.settle_bill, key, status, out.approval.reasoning, payment_row)
            lap("settle")
            out.followup = await self._comms(out, "drafted vendor follow-up", self.comms.followup) if self.llm.available else await self.comms.followup(out)
        except Exception as exc:  # noqa: BLE001 - one bad invoice never stops the batch
            out.error = f"{type(exc).__name__}: {exc}"
            log.exception("unhandled error", extra={"trace": trace, "stage": "pipeline", "detail": out.error})
        out.llm = {"calls": sum(e[0] == "call" for e in ledger), "cached": sum(e[0] == "cached" for e in ledger),
                   "skipped": [e[1] for e in ledger if e[0] == "skip"], "tools": sum(e[0] == "tool" for e in ledger),
                   "tokens_in": sum(e[2] for e in ledger), "tokens_out": sum(e[3] for e in ledger),
                   "cost_ticks": sum(e[4] for e in ledger)} if ledger else {}
        return out
