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
from .nextsteps import next_steps
from .payments import MockBank, PaymentService

console = Console()
PLAIN = False


def set_plain(on: bool = True) -> None:
    """Output for screen readers, braille displays and low vision: no colour, no table or box-drawing characters,
    one plain sentence per invoice. The same information as the table, in the order a person would read it aloud."""
    global PLAIN
    PLAIN = on
    console.no_color = on


set_plain(bool(os.environ.get("ACME_PLAIN")))
STYLE = {"AWAITING": "cyan", "PAID": "green", "APPROVE": "green", "REJECT": "red", "ESCALATE": "yellow", "DUPLICATE": "dim", "FAILED": "bold red",
         "SCHEDULED": "blue", "PAYMENT_HELD": "magenta", "PAYMENT_FAILED": "bold red"}


def _name(o: Outcome) -> str:
    return Path(o.source_path).parent.name + "/body" if Path(o.source_path).name == "body.txt" else os.path.basename(o.source_path)


def _show_plain(outcomes: list[Outcome]) -> None:
    console.print(f"Results for {len(outcomes)} invoice(s).", markup=False)
    for o in outcomes:
        inv = o.invoice
        why = o.error or (o.approval.reasoning if o.approval else "")
        if o.awaiting:
            why = f"Waiting for {o.awaiting} (request {o.approval_request}). {why}"
        total = f"{inv.currency} {inv.total}" if inv and inv.total is not None else "no total"
        console.print(f"Invoice file {_name(o)}, vendor {(inv.vendor if inv else None) or 'unknown'}, total {total}: {o.status}. {why}".rstrip(), markup=False, soft_wrap=True)
    for o in outcomes:
        steps = next_steps(o)
        if steps:
            name = o.invoice.invoice_number if o.invoice and o.invoice.invoice_number else _name(o)
            console.print(f"To get {name} paid: " + "; ".join(f"{who}: {what}" for who, what in steps[:2]) + (f" (and {len(steps) - 2} more)" if len(steps) > 2 else ""), markup=False, soft_wrap=True)
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    console.print("Totals: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ".", markup=False)


def show(outcomes: list[Outcome]) -> None:
    if PLAIN:
        return _show_plain(outcomes)
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
    todo = [(o, next_steps(o)) for o in outcomes]
    if any(steps for _, steps in todo):
        console.print("[bold]What would get them paid[/]")
        for o, steps in todo:
            if steps:
                name = o.invoice.invoice_number if o.invoice and o.invoice.invoice_number else os.path.basename(o.source_path)
                console.print(f"  {name}: " + "; ".join(f"{who}: {what}" for who, what in steps[:2]) + (f" (+{len(steps) - 2} more)" if len(steps) > 2 else ""), highlight=False)
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    console.print("  ".join(f"[{STYLE.get(k, 'white')}]{k}={v}[/]" for k, v in sorted(counts.items())))


def payment_summary(db: str) -> None:
    """What the treasury side looks like after this run (read from the payment ledger)."""
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


def show_trace(outcomes: list[Outcome], only_ai: bool = True, limit: int | None = None) -> None:
    """The agents at work, invoice by invoice: who did what, which tools the model called, what it concluded."""
    from rich.markup import escape
    shown = 0
    for o in outcomes:
        if not o.invoice or o.status == "DUPLICATE" or (only_ai and not any(t.get("events") for t in o.trace)):
            continue
        if limit is not None and shown >= limit:
            break
        shown += 1
        console.print(f"[bold]{escape(str(o.invoice.invoice_number))}[/] {escape(str(o.invoice.vendor))}  [{STYLE.get(o.status, 'white')}]{o.status}[/]")
        for t in o.trace:
            console.print(f"  [cyan]{t['agent']:<11}[/] {escape(t['action'])} [dim]({t['ms']:.0f} ms)[/]")
            for e in t.get("events", []):
                kind = e["kind"]
                if kind == "tool":
                    line = f"tool {e['name']}({escape(str(e['args'] or ''))}) -> {escape(str(e['result']))}"
                elif kind == "model":
                    line = f"asked the model as {e['as']}: {e['tokens'][0]} tokens in, {e['tokens'][1]} out"
                elif kind == "cached":
                    line = f"model answer for {e['as']} came from the cache"
                elif kind == "skipped":
                    why = {"intake": "the rules' reading was complete", "reviewer": "the rules had already decided", "critic": "the draft agreed with policy"}
                    line = f"model not asked ({e['as']}): {why.get(e['as'], 'nothing it could change')}"
                else:
                    line = f"{kind}: {escape(e.get('text', ''))}"
                console.print(f"                 [dim]-> {line}[/]")
    if not shown:
        console.print("[dim]no invoice involved the model or its tools in this run[/]")


def ai_usage(outcomes: list[Outcome], label: str) -> None:
    """What the model cost and what the agents saved by not asking it."""
    u = llm_usage(outcomes)
    if label == "none":
        console.print("[bold]AI usage[/]  none: --offline, every agent used its deterministic rules")
        return
    skipped = ", ".join(f"{a} {n}" for a, n in sorted(u["skipped"].items())) or "none"
    would = u["calls"] + u["cached"] + sum(u["skipped"].values())
    console.print(f"[bold]AI usage[/]  model: {label}" + ("" if label.startswith("Grok") else "  [dim](free; add --grok to use the real, billed Grok)[/]"))
    console.print(f"  model calls : {u['calls']} made, {u['tools']} tool calls run for the model, {u['cached']} answered from cache, {sum(u['skipped'].values())} avoided by gates ({skipped})"
                  f"  [dim](asking on every invoice would be about {would})[/]")
    if label.startswith("Grok"):
        price = f"spent ${u['cost']:.4f} (reported by xAI{', when recorded' if 'replayed' in label else ''})" if u["cost"] is not None else "cost not reported"
    else:
        price = "cost $0.00 (runs locally)"
    console.print(f"  tokens      : {u['tokens_in']:,} in / {u['tokens_out']:,} out; {price}")


def scorecard(outcomes: list[Outcome]) -> None:
    """How much work did we push onto vendors? (duplicates excluded: they are the same invoice twice)"""
    real = [o for o in outcomes if o.invoice and o.status != "DUPLICATE"]
    if not real:
        return
    n = len(real)
    lanes = {k: sum(bool(o.invoice and o.invoice.lane == k) for o in real) for k in ("structured", "embedded", "ai-extracted")}
    asked = sum(bool(o.followup) for o in real)
    pct = lambda k: f"{k / n:.0%}"  # noqa: E731
    console.print(f"[bold]Vendor effort[/] ({n} invoices)")
    console.print(f"  machine-readable, no AI needed : {lanes['structured'] + lanes['embedded']} ({pct(lanes['structured'] + lanes['embedded'])})"
                  f"  [dim](structured files {lanes['structured']}, e-invoice embedded in PDF {lanes['embedded']})[/]")
    console.print(f"  read by AI/regex from PDF/text : {lanes['ai-extracted']} ({pct(lanes['ai-extracted'])})  [dim](vendor did nothing extra)[/]")
    console.print(f"  needed a follow-up question    : {asked} ({pct(asked)})  [dim](drafted, never auto-sent)[/]")
