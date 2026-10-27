"""Terminal reporting: result table, vendor-effort scorecard, payments summary."""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .erp import MockNetSuite
from .metrics import kpis, llm_usage
from .models import Outcome
from .payments import MockBank, PaymentService

console = Console()
STYLE = {"AWAITING": "cyan", "PAID": "green", "APPROVE": "green", "REJECT": "red", "ESCALATE": "yellow", "DUPLICATE": "dim", "FAILED": "bold red",
         "SCHEDULED": "blue", "PAYMENT_HELD": "magenta", "PAYMENT_FAILED": "bold red"}


def show(outcomes: list[Outcome]) -> None:
    table = Table(title="Invoice results", show_lines=False)
    for col in ("File", "Vendor", "Total", "Status", "Why"):
        table.add_column(col, overflow="fold")
    for o in outcomes:
        inv = o.invoice
        why = o.error or (o.approval.reasoning if o.approval else "")
        if o.awaiting:
            why = f"-> {o.awaiting} (request #{o.approval_request}). {why}"
        if o.approval and o.approval.rounds > 1:
            why += f" [draft revised after critique: {o.approval.critiques[0]}]"
        table.add_row(Path(o.source_path).parent.name + "/body" if Path(o.source_path).name == "body.txt" else os.path.basename(o.source_path),
                      (inv.vendor if inv else "-") or "-", f"{inv.currency} {inv.total}" if inv and inv.total is not None else "-",
                      f"[{STYLE.get(o.status, 'white')}]{o.status}[/]", why[:110] + ("…" if len(why) > 110 else ""))
    console.print(table)
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    console.print("  ".join(f"[{STYLE.get(k, 'white')}]{k}={v}[/]" for k, v in sorted(counts.items())))


def payment_summary(db: str) -> None:
    """What the treasury side looks like after this run (read from the payment ledger)."""
    from decimal import Decimal
    erp = MockNetSuite(db, keep_ledger=True)
    rows = PaymentService(db, erp, MockBank(db + ".bank"), reset=False).rows()
    if not rows:
        return
    amt = lambda r: Decimal(r["amount"])  # noqa: E731
    sent = [r for r in rows if r["state"] in ("submitted", "settled")]
    later = [r for r in rows if r["state"] == "scheduled"]
    held = [r for r in rows if r["state"] in ("held", "failed")]
    console.print("[bold]Payments[/]")
    console.print(f"  sent to bank now : {len(sent)} ({sum(map(amt, sent)):,.2f})   scheduled for later : {len(later)} ({sum(map(amt, later)):,.2f})"
                  f"   held/failed : {len(held)}")
    discounts = sum(Decimal(r["discount"]) for r in rows)
    if discounts:
        console.print(f"  early-payment discounts captured : {discounts:,.2f}")
    for r in later:
        console.print(f"  [dim]{r['bill_key']}: release {r['release_on']} via {r['rail'].upper()}[/]")
    for r in held:
        console.print(f"  [yellow]{r['bill_key']}: {r['state']}: {r['last_error']}[/]")


def impact(outcomes: list[Outcome], waiting: list[dict]) -> None:
    """The three pain points, measured on this batch: errors, delay, who is left waiting."""
    k = kpis(outcomes)
    if not k.invoices:
        return
    console.print(f"[bold]Impact[/] ({k.invoices} invoices)")
    console.print(f"  no human touch needed     : {k.hands_off} ({k.hands_off_rate:.0%}); in the system a median {k.median_ms:.0f} ms, 95% under {k.p95_ms:.0f} ms"
                  f"  [dim](baseline in the brief: ~5 days each)[/]")
    top = ", ".join(f"{c} x{n}" for c, n in k.reasons.most_common(4))
    console.print(f"  stopped before the bank   : {k.stopped} rejected + {k.duplicates_blocked} duplicates blocked"
                  + (f"  [dim]({top})[/]" if top else ""))
    if waiting:
        oldest = max(waiting, key=lambda w: w["age_hours"])
        console.print(f"  waiting on a person       : {len(waiting)}; oldest {oldest['age_hours']:.0f}h with {oldest['role']}, "
                      f"{oldest['next']} in {oldest['next_in_hours']:.0f}h  [dim](never silent: reminder 24h, escalation 48h, expiry 5d)[/]")
    else:
        console.print("  waiting on a person       : 0")


def ai_usage(outcomes: list[Outcome], label: str) -> None:
    """What the model cost and what the agents saved by not asking it."""
    u = llm_usage(outcomes)
    if label == "none":
        console.print("[bold]AI usage[/]  none: --offline, every agent used its deterministic rules")
        return
    skipped = ", ".join(f"{a} {n}" for a, n in sorted(u["skipped"].items())) or "none"
    would = u["calls"] + u["cached"] + sum(u["skipped"].values())
    console.print(f"[bold]AI usage[/]  model: {label}" + ("" if label == "Grok" else "  [dim](set XAI_API_KEY to use Grok)[/]"))
    console.print(f"  model calls : {u['calls']} made, {u['tools']} tool calls run for the model, {u['cached']} answered from cache, {sum(u['skipped'].values())} avoided by gates ({skipped})"
                  f"  [dim](asking on every invoice would be about {would})[/]")
    price = f"est. cost ${u['cost']:.4f}" if u["cost"] is not None else "set XAI_PRICE_IN / XAI_PRICE_OUT (USD per 1M tokens) to price it"
    console.print(f"  tokens      : {u['tokens_in']:,} in / {u['tokens_out']:,} out; {price}")


def scorecard(outcomes: list[Outcome]) -> None:
    """How much work did we push onto vendors? (duplicates excluded: they are the same invoice twice)"""
    real = [o for o in outcomes if o.invoice and o.status != "DUPLICATE"]
    if not real:
        return
    n = len(real)
    lanes = {k: sum(o.invoice.lane == k for o in real) for k in ("structured", "embedded", "ai-extracted")}
    asked = sum(bool(o.followup) for o in real)
    pct = lambda k: f"{k / n:.0%}"  # noqa: E731
    console.print(f"[bold]Vendor effort[/] ({n} invoices)")
    console.print(f"  machine-readable, no AI needed : {lanes['structured'] + lanes['embedded']} ({pct(lanes['structured'] + lanes['embedded'])})"
                  f"  [dim](structured files {lanes['structured']}, e-invoice embedded in PDF {lanes['embedded']})[/]")
    console.print(f"  read by AI/regex from PDF/text : {lanes['ai-extracted']} ({pct(lanes['ai-extracted'])})  [dim](vendor did nothing extra)[/]")
    console.print(f"  needed a follow-up question    : {asked} ({pct(asked)})  [dim](drafted, never auto-sent)[/]")
