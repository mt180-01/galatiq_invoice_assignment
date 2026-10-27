import asyncio
from datetime import date, timedelta
from pathlib import Path

from acme_ap.approvals import ApprovalService
from acme_ap.approvals.tokens import utcnow
from acme_ap.erp import MockNetSuite
from acme_ap.llm import OfflineLLM
from acme_ap.metrics import kpis
from acme_ap.models import Approval, Decision, Invoice, Outcome
from acme_ap.notices import digest, draft_notice
from acme_ap.payments import MockBank, PaymentService
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "approvals_demo"


def world(tmp_path, today=date(2026, 10, 27)):
    db = tmp_path / "s.db"
    erp = MockNetSuite(db)
    svc = PaymentService(db, erp, MockBank(str(db) + ".bank"), today=lambda: today, sleep=lambda s: None)
    return erp, svc, ApprovalService(db, erp, payments=svc)


def run(erp, svc, approvals, paths):
    return asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, approvals=approvals, payments=svc).run([str(p) for p in paths]))


def test_kpis_on_the_provided_invoices(tmp_path):
    erp, svc, approvals = world(tmp_path)
    k = kpis(run(erp, svc, approvals, sorted((ROOT / "data" / "invoices").iterdir())))
    assert (k.invoices, k.hands_off, k.stopped, k.awaiting, k.duplicates_blocked) == (17, 7, 8, 2, 3)
    assert k.reasons["insufficient_stock"] >= 1 and k.median_ms > 0 and k.p95_ms >= k.median_ms


def test_vendor_notices_by_state(tmp_path):
    erp, svc, approvals = world(tmp_path)
    outs = {o.invoice.invoice_number: o for o in run(erp, svc, approvals, sorted(DEMO.iterdir()))}
    awaiting = draft_notice(outs["INV-3001"])
    assert "final sign-off" in awaiting and "within 2 business days" in awaiting
    assert draft_notice(outs["INV-3004"]) is None                     # changed bank account: never contact the sender
    paid = Outcome(source_path="x", invoice=Invoice(invoice_number="A", vendor="V", total=100, currency="USD"),
                   approval=Approval(decision=Decision.APPROVE, reasoning="ok"), paid=True, payment_ref="WIRE-AB12", payment_amount=98)
    note = draft_notice(paid, date(2026, 10, 27))
    assert "98" in note and "WIRE-AB12" in note and "same day" in note and "100.00" not in note   # the discounted amount, not the face value
    sched = paid.model_copy(update={"paid": False, "payment_state": "scheduled", "release_on": "2026-12-01"})
    assert "2026-12-01" in draft_notice(sched)
    asked = paid.model_copy(update={"paid": False, "followup": "a question"})
    assert draft_notice(asked) is None                                 # one email per invoice


def test_waiting_shows_age_owner_and_next_step(tmp_path):
    erp, svc, approvals = world(tmp_path)
    run(erp, svc, approvals, [DEMO / "invoice_3001.json"])
    now = utcnow()
    (w,) = approvals.waiting(now + timedelta(hours=1))
    assert w["role"] == "VP Finance" and w["invoice"] == "INV-3001" and w["next"] == "reminder" and 22 < w["next_in_hours"] < 24
    (w,) = approvals.waiting(now + timedelta(hours=30))
    assert w["next"].startswith("escalates to CFO") and 17 < w["next_in_hours"] < 19
    approvals.tick(now + timedelta(hours=50))
    (w,) = approvals.waiting(now + timedelta(hours=50))
    assert w["role"] == "CFO" and w["escalated_from"] == "VP Finance" and w["next"] == "expires unpaid"


def test_digest_names_what_is_stuck_and_why(tmp_path):
    erp, svc, approvals = world(tmp_path)
    outs = run(erp, svc, approvals, sorted(DEMO.iterdir()))
    text = digest(outs, approvals.waiting(), svc.rows())
    assert "WAITING ON A PERSON" in text and "INV-3001" in text and "with VP Finance" in text and "INV-3004" in text
    assert "…0000" in text and "99990000" not in text      # reasons shown; account numbers masked
