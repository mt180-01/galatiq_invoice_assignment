"""Running a batch end to end, plus the files and simulated actions around it."""
from __future__ import annotations

import argparse
import resource
import time
from pathlib import Path

from .approvals import ApprovalService
from .approvals.routing import DIRECTORY
from .erp import MockNetSuite
from .llm import make_llm
from .models import Outcome
from .notices import digest, draft_notice
from .payments import MockBank, PaymentService
from .pipeline import Pipeline
from .store import OutcomeStore


async def run_batch(paths: list[str], args: argparse.Namespace, workers: int | None, origins=None) -> tuple[list[Outcome], float, float]:
    erp = MockNetSuite(args.db, keep_ledger=args.keep_ledger)
    payments = PaymentService(args.db, erp, MockBank(args.db + ".bank", reset=not args.keep_ledger), reset=not args.keep_ledger)
    approvals = ApprovalService(args.db, erp, reset=not args.keep_ledger, payments=payments)
    llm = make_llm(args.offline, cache_path=None if args.offline else args.db + ".llmcache", simulate=getattr(args, "simulate", False), live=getattr(args, "grok", False))
    before = resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime
    k = resource.getrusage(resource.RUSAGE_CHILDREN)
    kids_before = k.ru_utime + k.ru_stime
    t0 = time.perf_counter()
    outcomes = await Pipeline(erp, llm, workers=workers, llm_concurrency=args.llm_concurrency, origins=origins,
                                approvals=approvals, payments=payments, store=None if getattr(args, "no_store", False) else OutcomeStore(args.db, reset=not args.keep_ledger),
                              llm_policy=getattr(args, "llm_policy", "gated")).run(paths)
    wall = time.perf_counter() - t0
    own = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = own.ru_utime + own.ru_stime - before + kids.ru_utime + kids.ru_stime - kids_before
    if hasattr(llm, "aclose"):
        await llm.aclose()
    return outcomes, wall, cpu


def write_outbox(outcomes: list[Outcome], outbox: Path) -> int:
    outbox.mkdir(parents=True, exist_ok=True)
    for old in outbox.glob("*.txt"):
        old.unlink()
    count = 0
    for o in outcomes:
        if o.followup:
            (outbox / f"{(o.invoice.invoice_number or 'unknown')}_{Path(o.source_path).stem}.txt").write_text(o.followup + "\n")
            count += 1
    return count


def write_cards(outcomes: list[Outcome], outbox: Path) -> int:
    folder = outbox / "approvals"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.txt"):
        old.unlink()
    n = 0
    for o in outcomes:
        if o.approval_card:
            (folder / f"request_{o.approval_request:03d}_{o.invoice.invoice_number}.txt").write_text(o.approval_card + "\n")
            n += 1
    return n


def ledger_view(db: str) -> tuple[list[dict], list]:
    """(requests still waiting on a person, payment rows) as the database stands now."""
    erp = MockNetSuite(db, keep_ledger=True)
    return ApprovalService(db, erp, reset=False).waiting(), PaymentService(db, erp, MockBank(db + ".bank"), reset=False).rows()


def write_notices(outcomes: list[Outcome], outbox: Path, waiting: list[dict], payments: list | None = None) -> int:
    """Vendor status notices (outbox/notices/) and the internal digest (outbox/daily_digest.txt)."""
    folder = outbox / "notices"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.txt"):
        old.unlink()
    n = 0
    for o in outcomes:
        if text := draft_notice(o):
            (folder / f"{o.invoice.invoice_number}_{Path(o.source_path).stem}.txt").write_text(text + "\n")
            n += 1
    (outbox / "daily_digest.txt").write_text(digest(outcomes, waiting, payments) + "\n")
    return n


def auto_vp(args: argparse.Namespace, outcomes: list[Outcome]) -> list[str]:
    """DEMO ONLY: a stand-in VP that approves requests whose only reason is the amount limit (nothing else
    flagged). Everything else stays pending for a person, to show the workflow end to end offline."""
    erp = MockNetSuite(args.db, keep_ledger=True)
    svc = ApprovalService(args.db, erp, reset=False)
    log = []
    for o in outcomes:
        if o.approval_request and o.awaiting == "VP Finance" and not any(f.severity.value == "hold" for f in o.findings):
            res = svc.respond(svc.tokens_for(o.approval_request)["approve"], DIRECTORY["VP Finance"]["email"], "demo auto-approval (simulated)")
            log.append(f"request #{o.approval_request} ({o.invoice.invoice_number}): {res.message}")
            if res.paid:
                o.paid, o.payment_ref, o.payment_amount, o.awaiting = True, res.payment_ref, res.amount, None
    return log
