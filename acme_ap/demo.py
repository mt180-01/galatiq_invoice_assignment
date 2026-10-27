"""`python main.py demo`: the whole story offline, in a scratch folder (nothing in the repo is touched)."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from .approvals import ApprovalService
from .erp import MockNetSuite
from .payments import MockBank, PaymentService
from .notices import digest, draft_notice
from .report import STYLE, console, impact, payment_summary, scorecard
from .runner import ledger_view, run_batch

DATA = Path(__file__).resolve().parent.parent / "data"


def demo() -> None:
    """Invoices -> follow-up -> VP approval -> timed payment -> reconcile."""
    root = Path(tempfile.mkdtemp(prefix="acme_demo_"))
    try:
        today = date.today()
        src = root / "in"
        shutil.copytree(DATA / "invoices", src)
        base = json.loads((DATA / "approvals_demo" / "invoice_3001.json").read_text())
        (src / "invoice_3001.json").write_text(json.dumps(base))                       # over the 10,000 sign-off limit
        one = dict(base, invoice_number="INV-3002", date=today.isoformat(), due_date=(today + timedelta(days=45)).isoformat(),
                   line_items=base["line_items"][:1], subtotal=3750, total=3750, payment_terms="Net 45")
        (src / "invoice_3002.json").write_text(json.dumps(one))                        # due in 45 days: should wait
        (src / "invoice_3003.json").write_text(json.dumps(dict(one, invoice_number="INV-3003", payment_terms="2/10 Net 30",
                                                               due_date=(today + timedelta(days=30)).isoformat())))  # discount
        db = str(root / "demo.db")
        args = SimpleNamespace(db=db, keep_ledger=False, llm_concurrency=8, offline=True)
        step = lambda n, text: console.print(f"\n[bold cyan]Step {n}.[/] {text}")  # noqa: E731

        step(1, "Invoices arrive in all sorts of formats; each is read, checked against the ERP and routed.")
        outcomes, _, _ = asyncio.run(run_batch(sorted(str(p) for p in src.iterdir()), args, 1))
        by_no = {o.invoice.invoice_number: o for o in outcomes if o.invoice and o.invoice.invoice_number}
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        console.print("  " + "  ".join(f"[{STYLE.get(k, 'white')}]{k}={v}[/]" for k, v in sorted(counts.items())))
        impact(outcomes, ledger_view(db)[0])
        scorecard(outcomes)

        step(2, "Fixable problems become a short drafted question to the vendor (never sent automatically). First one:")
        draft = next((o.followup for o in outcomes if o.followup), None)
        console.print("[dim]" + "\n".join("  | " + line for line in (draft or "(none)").splitlines()) + "[/]")

        step(3, "INV-3001 is over the 10,000 limit, so the VP gets a decision card instead of an email chain:")
        vp = by_no["INV-3001"]
        console.print("[dim]" + "\n".join("  | " + line for line in vp.approval_card.splitlines()[:14]) + "\n  | ...[/]")

        step(4, "The VP clicks Approve. The invoice is re-checked first, then paid (its due date has passed).")
        erp = MockNetSuite(db, keep_ledger=True)
        svc = ApprovalService(db, erp, reset=False)
        res = svc.respond(svc.tokens_for(vp.approval_request)["approve"], "vp-finance@acmecorp.example", "ok")
        console.print(f"  {res.message}")

        step(5, "Payments are timed, not instant: INV-3003 took its 2% early-pay discount now, INV-3002 waits for its due date.")
        payment_summary(db)

        scheduled = [date.fromisoformat(r["release_on"]) for r in ledger_view(db)[1] if r["state"] == "scheduled"]
        later = min(scheduled) if scheduled else today + timedelta(days=44)
        step(6, f"Time passes to {later}: due payments are released, then reconciled with the bank a few days after.")
        for day, label in ((later, "release"), (later + timedelta(days=5), "reconcile")):
            svc = PaymentService(db, erp, MockBank(db + ".bank"), reset=False, today=lambda d=day: d)
            if label == "release":
                sent = [r for r in svc.release_due() if r.submitted]
                console.print(f"  {day}: sent {len(sent)} payment(s) ({', '.join(r.ref for r in sent)})")
            else:
                rec = svc.reconcile()
                console.print(f"  {day}: settled {rec.settled}, returned {rec.returned}, still in flight {rec.pending}; issues: {rec.issues or 'none'}")
        step(7, "Nobody has to chase. Vendors get a remittance notice; the AP manager gets a digest of what is stuck and why.")
        console.print("[dim]" + "\n".join("  | " + line for line in draft_notice(by_no["INV-3003"]).splitlines()[:6]) + "[/]")
        waiting, rows = ledger_view(db)
        console.print("[dim]" + "\n".join("  | " + line for line in digest(outcomes, waiting, rows).splitlines()[:8]) + "\n  | ...[/]")
        console.print("\n[dim]Next: python main.py --help, python approve.py --help, python pay.py --help, docs/ARCHITECTURE.md[/]")
    finally:
        shutil.rmtree(root, ignore_errors=True)
