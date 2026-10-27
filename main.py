"""CLI: python main.py --invoice_path=data/invoices/invoice_1001.txt   (a file or a directory)"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import resource
import shutil
import sys
import tempfile
import time
import warnings
from pathlib import Path

from rich.console import Console
from rich.table import Table

from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import SUPPORTED
from acme_ap.llm import make_llm
from acme_ap.models import Outcome
from acme_ap.pipeline import Pipeline

warnings.filterwarnings("ignore")
console = Console()
STYLE = {"PAID": "green", "APPROVE": "green", "REJECT": "red", "ESCALATE": "yellow", "DUPLICATE": "dim", "FAILED": "bold red"}


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}"
        extra = " ".join(f'{k}="{getattr(record, k)}"' for k in ("trace", "stage", "detail") if hasattr(record, k))
        return f"{base} {extra or record.getMessage()}"


def collect(path: str) -> list[str]:
    p = Path(path)
    if p.is_dir():
        return sorted(str(f) for f in p.iterdir() if f.suffix.lower() in SUPPORTED)
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


async def run_batch(paths: list[str], args: argparse.Namespace, workers: int | None) -> tuple[list[Outcome], float, float]:
    erp = MockNetSuite(args.db, keep_ledger=args.keep_ledger)
    llm = make_llm(args.offline)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime
    k = resource.getrusage(resource.RUSAGE_CHILDREN)
    kids_before = k.ru_utime + k.ru_stime
    t0 = time.perf_counter()
    outcomes = await Pipeline(erp, llm, workers=workers, llm_concurrency=args.llm_concurrency).run(paths)
    wall = time.perf_counter() - t0
    own = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = own.ru_utime + own.ru_stime - before + kids.ru_utime + kids.ru_stime - kids_before
    if hasattr(llm, "aclose"):
        await llm.aclose()
    return outcomes, wall, cpu


def show(outcomes: list[Outcome]) -> None:
    table = Table(title="Invoice results", show_lines=False)
    for col in ("File", "Vendor", "Total", "Status", "Why"):
        table.add_column(col, overflow="fold")
    for o in outcomes:
        inv = o.invoice
        why = o.error or (o.approval.reasoning if o.approval else "")
        table.add_row(os.path.basename(o.source_path),
                      (inv.vendor if inv else "-") or "-", f"{inv.currency} {inv.total}" if inv and inv.total is not None else "-",
                      f"[{STYLE.get(o.status, 'white')}]{o.status}[/]", why[:110] + ("…" if len(why) > 110 else ""))
    console.print(table)
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    console.print("  ".join(f"[{STYLE.get(k, 'white')}]{k}={v}[/]" for k, v in sorted(counts.items())))


def main() -> None:
    ap = argparse.ArgumentParser(description="Acme AP invoice automation")
    ap.add_argument("--invoice_path", default="data/invoices", help="invoice file or directory")
    ap.add_argument("--db", default="inventory.db")
    ap.add_argument("--keep-ledger", action="store_true", help="keep bills/payments from earlier runs (duplicate detection across runs)")
    ap.add_argument("--workers", type=int, default=None, help="CPU worker processes (default: auto-sized from the PDF count; 1 = serial)")
    ap.add_argument("--pdf-share", type=float, default=0.5, help="share of PDFs in the --bench corpus (real AP inboxes are mostly PDF)")
    ap.add_argument("--llm-concurrency", type=int, default=8)
    ap.add_argument("--offline", action="store_true", help="ignore XAI_API_KEY and use the deterministic paths")
    ap.add_argument("--json", metavar="FILE", help="write full results as JSON")
    ap.add_argument("--bench", type=int, metavar="N", help="throughput benchmark over N generated documents, serial vs parallel")
    ap.add_argument("-v", "--verbose", action="store_true", help="show per-stage structured logs")
    args = ap.parse_args()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(KeyValueFormatter())
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, handlers=[handler])
    paths = collect(args.invoice_path)
    ncpu = os.cpu_count() or 1
    console.print(f"[bold]{len(paths)} document(s)[/], {ncpu} CPU cores, LLM: "
                  f"{'Grok (' + os.environ.get('XAI_MODEL', 'grok-3') + ')' if make_llm(args.offline).available else 'offline (deterministic agents)'}")

    if args.bench:
        corpus, tmp = make_bench_corpus(paths, args.bench, args.pdf_share)
        logging.getLogger().setLevel(logging.ERROR)
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

    outcomes, wall, cpu = asyncio.run(run_batch(paths, args, args.workers))
    show(outcomes)
    console.print(f"[dim]{wall:.2f}s wall, {cpu:.2f}s CPU, {len(paths) / wall:,.1f} docs/s[/]")
    if args.json:
        Path(args.json).write_text(json.dumps([o.model_dump(mode="json") | {"status": o.status} for o in outcomes], indent=2))
        console.print(f"results written to {args.json}")


if __name__ == "__main__":
    main()
