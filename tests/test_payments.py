import asyncio
import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path


from acme_ap.erp import MockNetSuite
from acme_ap.llm import OfflineLLM
from acme_ap.models import Invoice
from acme_ap.payments import MockBank, PaymentService
from acme_ap.payments.terms import parse_terms
from acme_ap.pipeline import Pipeline

DEMO = Path(__file__).resolve().parent.parent / "data" / "approvals_demo" / "invoice_3001.json"
TODAY = date(2026, 10, 27)


def make(tmp_path, today=TODAY, **bank_kw):
    db = tmp_path / "p.db"
    erp = MockNetSuite(db)
    clock = {"d": today}
    bank = MockBank(str(db) + ".bank", **bank_kw)
    svc = PaymentService(db, erp, bank, today=lambda: clock["d"], sleep=lambda s: None)
    return db, erp, bank, svc, clock


def inv(total="500", due=None, date_="2026-10-01", terms="Net 30", vendor="Widgets Inc."):
    return Invoice(invoice_number="X", vendor=vendor, date=date_, due_date=due, total=Decimal(total), payment_terms=terms)


def test_terms_parsing():
    assert parse_terms("2/10 Net 30").discount_pct == Decimal(2) and parse_terms("2/10 Net 30").net_days == 30
    assert parse_terms("Net 60").net_days == 60
    assert parse_terms("Immediate").net_days == 0 and parse_terms("whenever").net_days is None


def test_same_bill_is_never_paid_twice_and_refs_are_unique(tmp_path):
    _, _, bank, svc, _ = make(tmp_path)
    a = svc.pay(inv(due="2026-10-01"), "k1")
    again = svc.pay(inv(due="2026-10-01"), "k1")
    other = svc.pay(inv(due="2026-10-01"), "k2")          # same vendor, same amount, different bill
    assert a.submitted and again.ref == a.ref and other.ref != a.ref
    assert len(bank.payments()) == 2


def test_payment_waits_for_due_date_then_releases(tmp_path):
    db, erp, bank, svc, clock = make(tmp_path)
    r = svc.pay(inv(due="2026-12-01"), "k")
    assert r.state == "scheduled" and r.release_on == "2026-11-27" and not bank.payments()   # Tue 12-01 minus 2 business days (Thanksgiving is a holiday)
    clock["d"] = date(2026, 11, 25)
    svc.release_due()
    assert not bank.payments()                                   # a day early: still nothing sent
    clock["d"] = date(2026, 11, 27)
    (res,) = svc.release_due()
    assert res.submitted and len(bank.payments()) == 1
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0] == 1


def test_early_payment_discount_is_captured_only_while_open(tmp_path):
    _, _, _, svc, clock = make(tmp_path)
    r = svc.pay(inv("10000", terms="2/10 Net 30", date_="2026-10-20"), "a")
    assert r.submitted and r.amount == Decimal("9800.00") and r.discount == Decimal("200.00")
    late = svc.pay(inv("10000", terms="2/10 Net 30", date_="2026-10-01"), "b")     # window closed 10-11
    assert late.amount == Decimal("10000") and late.discount == 0


def test_wire_for_large_amounts(tmp_path):
    _, _, _, svc, _ = make(tmp_path)
    assert svc.pay(inv("30000", due="2026-10-01"), "w").ref.startswith("WIRE-")
    assert svc.pay(inv("300", due="2026-10-01"), "c").ref.startswith("ACH-")


def test_transient_bank_errors_are_retried_with_the_same_key(tmp_path):
    _, _, bank, svc, _ = make(tmp_path, fail_first=2)
    r = svc.pay(inv(due="2026-10-01"), "k")
    assert r.submitted and bank.calls == 3 and len(bank.payments()) == 1


def test_outage_leaves_payment_scheduled_and_next_run_finishes_it(tmp_path):
    _, _, bank, svc, _ = make(tmp_path, fail_first=4)
    r = svc.pay(inv(due="2026-10-01"), "k")
    assert r.state == "scheduled" and "503" in r.message and not bank.payments()
    (done,) = svc.release_due()
    assert done.submitted and len(bank.payments()) == 1


def test_permanent_rejection_stops_retrying(tmp_path):
    db, _, bank, svc, _ = make(tmp_path, reject_accounts=set())
    acct = svc._db.execute("SELECT account FROM vendor_accounts WHERE vendor='widgetsinc'").fetchone()[0]
    bank.reject_accounts.add(acct)
    r = svc.pay(inv(due="2026-10-01"), "k")
    assert r.state == "failed" and bank.calls == 1


def test_crash_between_bank_and_ledger_is_resolved_by_asking_the_bank(tmp_path):
    db, erp, bank, svc, _ = make(tmp_path)
    svc.pay(inv(due="2026-12-25"), "k")                    # scheduled, nothing at the bank
    row = svc._row("k")
    receipt = bank.submit("k", row["vendor"], row["account"], Decimal(row["amount"]), "USD", "ach", TODAY)   # bank took it...
    svc._db.execute("UPDATE payments SET state='submitting' WHERE bill_key='k'"); svc._db.commit()          # ...then we died
    assert svc.recover()
    assert svc._row("k")["state"] == "submitted" and svc._row("k")["bank_ref"] == receipt.ref
    assert len(bank.payments()) == 1                       # and a resubmit would not create a second one
    assert bank.submit("k", "v", "a", Decimal(1), "USD", "ach", TODAY).ref == receipt.ref


def test_daily_limit_defers_the_overflow(tmp_path):
    db, erp, bank, _, clock = make(tmp_path)
    svc = PaymentService(db, erp, bank, reset=False, today=lambda: clock["d"], sleep=lambda s: None, daily_limit=Decimal("1000"))
    a, b = svc.pay(inv("700", due="2026-10-01"), "a"), svc.pay(inv("700", due="2026-10-01"), "b")
    assert a.submitted and b.state == "scheduled" and b.release_on == "2026-10-28"


def test_changed_bank_account_needs_a_second_person_and_holds_payments(tmp_path):
    _, _, bank, svc, _ = make(tmp_path)
    svc.pay(inv(due="2026-12-25"), "k")
    assert "1 scheduled" in svc.set_account("Widgets Inc.", "acct-NEW", by="ap@x")
    assert svc.pay(inv(due="2026-10-01"), "k2").state == "held" and not bank.payments()
    assert svc.verify_account("Widgets Inc.", by="ap@x").startswith("refused")
    assert "2 held" in svc.verify_account("Widgets Inc.", by="controller@x")
    assert svc._row("k")["state"] == svc._row("k2")["state"] == "scheduled"
    assert [r.submitted for r in svc.release_due()] == [True]      # k2 is due now; k waits for its date


def test_unknown_payee_account_is_held(tmp_path):
    _, _, bank, svc, _ = make(tmp_path)
    r = svc.pay(inv(due="2026-10-01", vendor="Nobody Ltd"), "n")
    assert r.state == "held" and not bank.payments()


def test_reconciliation_settles_flags_returns_and_unknown_bank_items(tmp_path):
    db, erp, bank, svc, clock = make(tmp_path, return_vendors={"Gadgets Co."})
    for k in ("ok", "bad"):
        erp.claim_bill(k, "src", "v", Decimal(1))
    svc.pay(inv(due="2026-10-01"), "ok"); svc.pay(inv(due="2026-10-01", vendor="Gadgets Co."), "bad")
    bank.submit("ghost", "Mallory", "acct", Decimal(99), "USD", "ach", TODAY)
    assert svc.reconcile().pending == 2
    clock["d"] = date(2026, 10, 30)
    rec = svc.reconcile()
    assert (rec.settled, rec.returned) == (1, 1)
    assert any("RETURNED" in i for i in rec.issues) and any("no record" in i for i in rec.issues)
    assert erp._conn().execute("SELECT status FROM vendor_bills WHERE bill_key='bad'").fetchone()[0] == "payment_returned"


def test_pipeline_schedules_future_due_invoice(tmp_path):
    data = json.loads(DEMO.read_text())
    data.update(date="2026-10-20", due_date="2027-01-15", invoice_number="INV-3002", total=3750, subtotal=3750)
    data["line_items"] = data["line_items"][:1]
    f = tmp_path / "invoice_3002.json"; f.write_text(json.dumps(data))
    db, erp, bank, svc, clock = make(tmp_path)
    (o,) = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, payments=svc).run([str(f)]))
    assert o.status == "SCHEDULED" and o.release_on == "2027-01-13" and not o.paid
    assert erp._conn().execute("SELECT status FROM vendor_bills").fetchone()[0] == "scheduled"
    clock["d"] = date(2027, 1, 13)
    svc.release_due()
    assert erp._conn().execute("SELECT status FROM vendor_bills").fetchone()[0] == "paid"


def test_cost_per_payment_does_not_grow_with_volume(tmp_path):
    """Regression: the daily-limit check once re-read every payment sent that day, so 20,000 invoices took minutes."""
    import time
    db, erp, bank, _, clock = make(tmp_path)
    svc = PaymentService(db, erp, bank, reset=False, today=lambda: clock["d"], sleep=lambda s: None, daily_limit=Decimal("1e12"))

    def batch(start, n):
        t = time.perf_counter()
        for i in range(start, start + n):
            svc.pay(inv("10", due="2026-10-01"), f"k{i}")
        return (time.perf_counter() - t) / n

    small = batch(0, 200)
    batch(200, 1500)
    large = batch(1700, 200)          # same batch size, 1,700 payments already sent today
    assert large < small * 4, f"{small * 1000:.2f} ms/payment at the start vs {large * 1000:.2f} ms after 1,700"
    assert len(bank.payments()) == 1900


def test_daily_limit_headroom_is_returned_when_the_bank_refuses(tmp_path):
    db, erp, bank, _, clock = make(tmp_path, fail_first=3)
    svc = PaymentService(db, erp, bank, reset=False, today=lambda: clock["d"], sleep=lambda s: None, daily_limit=Decimal("1000"))
    assert svc.pay(inv("900", due="2026-10-01"), "a").state == "scheduled"      # bank down: nothing left
    assert svc.pay(inv("900", due="2026-10-01"), "b").submitted                 # so the limit is still free


def test_business_day_calendar():
    from acme_ap.payments.calendar import add_business_days, holidays, is_business_day, next_business_day, sub_business_days
    assert not is_business_day(date(2026, 11, 28)) and not is_business_day(date(2026, 11, 26))      # Saturday; Thanksgiving
    assert not is_business_day(date(2026, 12, 25)) and not is_business_day(date(2026, 7, 3))         # Christmas; Jul 4 is a Saturday: observed Friday
    assert date(2026, 5, 25) in holidays(2026) and date(2026, 9, 7) in holidays(2026)                # Memorial Day, Labor Day
    assert next_business_day(date(2026, 11, 28)) == date(2026, 11, 30)
    assert add_business_days(date(2026, 11, 25), 2) == date(2026, 11, 30)                            # skips Thanksgiving and the weekend
    assert sub_business_days(date(2026, 12, 1), 2) == date(2026, 11, 27)


def test_nothing_is_sent_on_a_day_the_banks_are_closed(tmp_path):
    _, _, bank, svc, clock = make(tmp_path, today=date(2026, 11, 28))                                # a Saturday
    r = svc.pay(inv(due="2026-10-01"), "k")                                                           # overdue: would be "pay now"
    assert r.state == "scheduled" and r.release_on == "2026-11-30" and not bank.payments()
    clock["d"] = date(2026, 11, 30)
    assert svc.release_due()[0].submitted


def test_a_due_date_on_a_weekend_is_met_by_paying_before_it(tmp_path):
    from acme_ap.payments.calendar import add_business_days
    _, _, _, svc, _ = make(tmp_path)
    plan = svc.plan(inv(due="2026-12-26"), date(2026, 10, 27))                                       # due Saturday; Christmas Friday
    assert plan.release_on == date(2026, 12, 22)                                                      # must arrive Thu 12-24; ACH takes 2 business days
    assert add_business_days(plan.release_on, 2) == date(2026, 12, 24)
