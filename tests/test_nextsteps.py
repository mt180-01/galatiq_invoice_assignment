import asyncio
import re
from pathlib import Path

from acme_ap import nextsteps
from acme_ap.erp import MockNetSuite
from acme_ap.llm import OfflineLLM
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent


def test_every_finding_that_can_stop_an_invoice_has_a_next_step():
    """If a rule is added without telling the person what to do about it, this fails."""
    codes = set()
    for f in (ROOT / "acme_ap").rglob("*.py"):
        codes |= set(re.findall(r'_f\("([a-z_]+)", (?:E|H|Severity\.(?:ERROR|HOLD))', f.read_text()))
        codes |= set(re.findall(r'Finding\(code="([a-z_]+)", severity=Severity\.(?:ERROR|HOLD)', f.read_text()))
    codes -= {"item_corrected"}
    assert codes and codes <= set(nextsteps.STEPS), sorted(codes - set(nextsteps.STEPS))


def test_provided_invoices_each_stopped_one_gets_a_plan_and_paid_ones_do_not(tmp_path):
    outs = asyncio.run(Pipeline(MockNetSuite(tmp_path / "n.db"), OfflineLLM(), workers=1, sequential=True).run(sorted(str(p) for p in (ROOT / "data" / "invoices").glob("*.*") if p.suffix != ".pdf")))
    for o in outs:
        steps = nextsteps.next_steps(o)
        if o.approval and o.approval.decision.value == "approve":
            assert steps == []
        elif o.findings and any(f.severity.value in ("error", "hold") for f in o.findings):
            assert steps, o.source_path
    by_name = {Path(o.source_path).name: o for o in outs}
    assert any("stock" in what or "quantity" in what for _, what in nextsteps.next_steps(by_name["invoice_1002.txt"]))


def test_the_invoice_page_shows_the_plan(tmp_path):
    from acme_ap.web import App
    db = tmp_path / "w.db"
    app = App(db)
    asyncio.run(Pipeline(MockNetSuite(db), OfflineLLM(), workers=1, store=app.store).run([str(ROOT / "data" / "invoices" / "invoice_1002.txt")]))
    app.store.flush()
    assert "What would get this paid" in app.invoice(1).decode()
