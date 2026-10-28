"""CLI: python main.py --invoice_path=data/invoices/invoice_1001.txt   (a file or a directory); python main.py demo"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

from rich.table import Table

from acme_ap.demo import demo
from acme_ap.ingestion import SUPPORTED
from acme_ap.intake import expand
from acme_ap.llm import make_llm
from acme_ap.report import ai_usage, console, impact, payment_summary, scorecard, show, show_trace
from acme_ap.runner import auto_vp, ledger_view, run_batch, write_cards, write_notices, write_outbox

warnings.filterwarnings("ignore")


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}"
        extra = " ".join(f'{k}="{getattr(record, k)}"' for k in ("trace", "stage", "detail") if hasattr(record, k))
        return f"{base} {extra or record.getMessage()}"


def collect(path: str) -> list[str]:
    p = Path(path)
    if p.is_dir():
        return sorted(str(f) for f in p.iterdir() if f.suffix.lower() in SUPPORTED | {".eml"})
    if not p.exists():
        sys.exit(f"error: {path} does not exist")
    return [str(p)]


def make_bench_corpus(src: list[str], n: int, pdf_share: float) -> tuple[list[str], Path]:
    """n documents from the samples; text-like files get fresh invoice numbers so they are not dupes."""
    tmp = Path(tempfile.mkdtemp(prefix="acme_bench_"))
    pdfs = [s for s in src if s.endswith(".pdf")]
    others = [s for s in src if not s.endswith(".pdf")]
    out = []
    for i in range(n):
        f = Path(pdfs[i % len(pdfs)] if (i % 100) < pdf_share * 100 else others[i % len(others)])
        dest = tmp / f"{i:06d}_{f.name}"
        if f.suffix.lower() == ".pdf":
            shutil.copy(f, dest)
        else:
            text = f.read_text()
            digits = "".join(c for c in f.stem if c.isdigit())[:4]
            dest.write_text(text.replace(digits, f"9{i:05d}") if digits else text)
        out.append(str(dest))
    return out, tmp


def serve_ui(argv: list[str]) -> None:
    """`python main.py serve [--db inventory.db] [--port 8765] [--demo]`: the local web interface."""
    from acme_ap.web import serve

    ap = argparse.ArgumentParser(prog="main.py serve")
    ap.add_argument("--db", default="inventory.db")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="first process the sample invoices (provided, self-correction, approval demo) into --db with the free local model")
    a = ap.parse_args(argv)
    if a.demo:
        from types import SimpleNamespace
        paths = collect("data/invoices") + collect("data/self_correction") + collect("data/approvals_demo")
        args = SimpleNamespace(db=a.db, keep_ledger=False, llm_concurrency=8, offline=False, simulate=True, grok=False)
        outcomes, _, _ = asyncio.run(run_batch(paths, args, 1))
        console.print(f"[dim]loaded {len(outcomes)} sample documents into {a.db}[/]")
    serve(a.db, port=a.port)


def agents() -> None:
    """`python main.py agents`: who does what, and when each may spend model tokens."""
    from acme_ap.agents.team import CHARTERS

    t = Table(title="The agent team", show_lines=True)
    for col in ("Agent", "Job", "Tools", "May call the model", "Hands to"):
        t.add_column(col, overflow="fold")
    for c in CHARTERS:
        t.add_row(f"[bold]{c.name}[/]", c.job, c.tools, c.model_use, c.hands_to)
    console.print(t)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        return demo()
    if len(sys.argv) > 1 and sys.argv[1] == "agents":
        return agents()
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        return serve_ui(sys.argv[2:])
    ap = argparse.ArgumentParser(description="Acme AP invoice automation")
    ap.add_argument("--invoice_path", default="data/invoices", help="invoice file or directory")
    ap.add_argument("--db", default="inventory.db")
    ap.add_argument("--keep-ledger", action="store_true", help="keep bills/payments from earlier runs (duplicate detection across runs)")
    ap.add_argument("--workers", type=int, default=None, help="CPU worker processes (default: auto-sized from the PDF count; 1 = serial)")
    ap.add_argument("--pdf-share", type=float, default=0.5, help="share of PDFs in the --bench corpus (real AP inboxes are mostly PDF)")
    ap.add_argument("--auto-vp", action="store_true", help="DEMO: simulate a VP approving requests that are only over the amount limit")
    ap.add_argument("--outbox", default="outbox", help="where drafted vendor follow-up emails are written")
    ap.add_argument("--llm-concurrency", type=int, default=8)
    ap.add_argument("--llm-policy", choices=("gated", "always"), default="gated",
                    help="gated (default): ask the model only where it can change the result; always: ask on every invoice (costly)")
    ap.add_argument("--trace", action="store_true", help="show, per invoice, what each agent did: model calls, tool calls, conclusions")
    ap.add_argument("--offline", action="store_true", help="no model at all: rules only")
    ap.add_argument("--simulate", action="store_true", help="use the local stand-in model (this is already the default)")
    ap.add_argument("--grok", action="store_true", help="use the real Grok API: billed per token by xAI, capped per run by XAI_BUDGET_USD (default $0.25)")
    ap.add_argument("--json", metavar="FILE", help="write full results as JSON")
    ap.add_argument("--bench", type=int, metavar="N", help="throughput benchmark over N generated documents, serial vs parallel")
    ap.add_argument("-v", "--verbose", action="store_true", help="show per-stage structured logs")
    args = ap.parse_args()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(KeyValueFormatter())
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, handlers=[handler])
    for name in ("acme.decision", "acme.payment"):  # the brief asks for structured logs: decisions and payments always show
        logging.getLogger(name).setLevel(logging.INFO)
    paths = collect(args.invoice_path)
    ncpu = os.cpu_count() or 1
    llm_label = make_llm(args.offline, simulate=args.simulate, live=args.grok).label
    console.print(f"[bold]{f'{args.bench:,} generated documents (from {len(paths)} samples)' if args.bench else f'{len(paths)} document(s)'}[/], {ncpu} CPU cores, LLM: "
                  f"{llm_label + (' (' + os.environ.get('XAI_MODEL', 'grok-3') + ')' if llm_label == 'Grok' else '')}")

    if args.bench:
        corpus, tmp = make_bench_corpus(paths, args.bench, args.pdf_share)
        args.no_store = True  # a benchmark does not need 20,000 records kept
        logging.getLogger().setLevel(logging.ERROR)
        for name in ("acme.decision", "acme.payment"):
            logging.getLogger(name).setLevel(logging.ERROR)
        try:
            import io, contextlib
            rows = []
            for label, w in (("serial (1 process)", 1), (f"parallel ({ncpu} workers)", ncpu), ("auto (PDFs to pool if it pays)", None)):
                with contextlib.redirect_stdout(io.StringIO()):  # silence mock bank prints
                    _, wall, cpu = asyncio.run(run_batch(corpus, args, w))
                rows.append((label, wall, cpu))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        t = Table(title=f"Benchmark: {args.bench} documents")
        for c in ("Mode", "Wall s", "Docs/s", "CPU-seconds", "Core utilisation"):
            t.add_column(c)
        for label, wall, cpu in rows:
            t.add_row(label, f"{wall:.2f}", f"{args.bench / wall:,.0f}", f"{cpu:.2f}", f"{cpu / wall / ncpu:.0%}")
        console.print(t)
        return

    intake_dir = Path(tempfile.mkdtemp(prefix="acme_intake_"))
    try:
        paths, origins = expand(paths, intake_dir)
        outcomes, wall, cpu = asyncio.run(run_batch(paths, args, args.workers, origins))
    finally:
        shutil.rmtree(intake_dir, ignore_errors=True)
    drafts = write_outbox(outcomes, Path(args.outbox))
    cards = write_cards(outcomes, Path(args.outbox))
    simulated = auto_vp(args, outcomes) if args.auto_vp else []
    show(outcomes)
    for line in simulated:
        console.print(f"[yellow]SIMULATED VP[/] {line}")
    waiting, pay_rows = ledger_view(args.db)
    notices = write_notices(outcomes, Path(args.outbox), waiting, pay_rows)
    impact(outcomes, waiting)
    ai_usage(outcomes, llm_label)
    if args.trace:
        show_trace(outcomes)
    scorecard(outcomes)
    payment_summary(args.db)
    if cards:
        console.print(f"[dim]{cards} approval request(s) awaiting a person; decision cards in {args.outbox}/approvals/. "
                      f"Respond with: python approve.py list | respond[/]")
    console.print(f"[dim]{notices} vendor status notice(s) in {args.outbox}/notices/ and the AP digest in {args.outbox}/daily_digest.txt[/]")
    if drafts:
        console.print(f"[dim]{drafts} follow-up draft(s) in {args.outbox}/ (review before sending; nothing is sent automatically)[/]")
    console.print(f"[dim]{wall:.2f}s wall, {cpu:.2f}s CPU, {len(paths) / wall:,.1f} docs/s[/]")
    if args.json:
        Path(args.json).write_text(json.dumps([o.model_dump(mode="json") | {"status": o.status} for o in outcomes], indent=2))
        console.print(f"results written to {args.json}")


if __name__ == "__main__":
    main()
