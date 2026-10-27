import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from acme_ap.agents import validation
from acme_ap.approvals import ApprovalService
from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import heuristic, structured
from acme_ap.llm import OfflineLLM
from acme_ap.models import Invoice
from acme_ap.payments import MockBank, PaymentService
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
CHANGED = ROOT / "data" / "approvals_demo" / "invoice_3004_changed_bank_account.json"
CLEAN = ROOT / "data" / "approvals_demo" / "invoice_3001.json"


def world(tmp_path):
    db = tmp_path / "b.db"
    erp = MockNetSuite(db)
    svc = PaymentService(db, erp, MockBank(str(db) + ".bank"), today=lambda: date(2026, 10, 27), sleep=lambda s: None)
    return db, erp, svc, ApprovalService(db, erp, payments=svc)


def run(erp, svc, approvals, *paths):
    return asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, approvals=approvals, payments=svc).run([str(p) for p in paths]))


def test_bank_account_is_read_from_every_format():
    assert structured.from_json('{"invoice_number":"INV-1","bank_account":"acct 12 34"}').bank_account == "ACCT1234"
    assert structured.from_json('{"invoice_number":"INV-1","payment_details":{"account":"x-99"}}').bank_account == "X-99"
    assert structured.from_xml("<invoice><header><invoice_number>INV-1</invoice_number></header><bank_account>ab 1</bank_account></invoice>").bank_account == "AB1"
    ubl = ('<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="c"><ID>INV-5</ID>'
           '<cac:PayeeFinancialAccount><ID>DE001234</ID></cac:PayeeFinancialAccount></Invoice>')
    assert structured.from_xml(ubl).bank_account == "DE001234"
    assert heuristic.extract("Invoice INV-1\nVendor: Widgets Inc.\nBank Account No: acct-4455 6677\nTotal: 5").bank_account == "ACCT-44556677"


def test_changed_account_holds_the_invoice_for_a_person(tmp_path):
    db, erp, svc, approvals = world(tmp_path)
    (o,) = run(erp, svc, approvals, CHANGED)
    assert o.status == "AWAITING" and o.awaiting == "AP Manager" and not o.paid
    assert any(f.code == "bank_account_changed" for f in o.findings)
    assert o.followup is None                               # never ask the (possibly fake) sender to confirm new details
    assert "…0000" in o.approval.reasoning or any("…0000" in f.message for f in o.findings)
    assert not svc.bank.payments()
    assert "99990000" not in " ".join(f.message for f in o.findings)   # full account numbers are not printed


def test_approver_cannot_click_through_a_changed_account(tmp_path):
    db, erp, svc, approvals = world(tmp_path)
    (o,) = run(erp, svc, approvals, CHANGED)
    res = approvals.respond(approvals.tokens_for(o.approval_request)["approve"], "ap-manager@acmecorp.example")
    assert not res.ok and "re-check failed" in res.message and not svc.bank.payments()


def test_matching_account_is_paid(tmp_path):
    db, erp, svc, approvals = world(tmp_path)
    on_file = svc._db.execute("SELECT account FROM vendor_accounts WHERE vendor='widgetsinc'").fetchone()[0]
    data = json.loads(CHANGED.read_text()) | {"bank_account": on_file.lower(), "invoice_number": "INV-3005"}
    f = tmp_path / "ok.json"; f.write_text(json.dumps(data))
    (o,) = run(erp, svc, approvals, f)
    assert o.status == "PAID"


def test_zero_and_nonpositive_amounts_never_reach_the_bank(tmp_path):
    db, erp, svc, _ = world(tmp_path)
    zero = Invoice(invoice_number="Z", vendor="Widgets Inc.", total=Decimal(0), line_items=[], date="2026-10-01", due_date="2026-10-02")
    assert any(f.code == "zero_total" for f in validation.validate(zero, erp))
    try:
        svc.pay(zero, "z")
        raise AssertionError("should have refused")
    except ValueError as exc:
        assert "non-positive" in str(exc)
    assert not svc.bank.payments()


def test_reconcile_repairs_an_erp_bill_the_crash_left_unpaid(tmp_path):
    db, erp, svc, _ = world(tmp_path)
    erp.claim_bill("k", "src", "Widgets Inc.", Decimal(500))
    r = svc.pay(Invoice(invoice_number="K", vendor="Widgets Inc.", total=Decimal(500), date="2026-10-01", due_date="2026-10-02"), "k")
    assert r.submitted and erp.bill_status("k") == "received"        # bank paid; we died before settle_bill
    rec = svc.reconcile()
    assert erp.bill_status("k") == "paid" and any("REPAIRED" in i for i in rec.issues)
    assert erp._conn().execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0] == 1
    assert not any("REPAIRED" in i for i in svc.reconcile().issues)  # and it is idempotent
