"""Stock that really leaves the shelf: the `decrement` policy, and the opening stock surviving between runs."""
import asyncio
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from acme_ap.erp import MockNetSuite
from acme_ap.llm import OfflineLLM
from acme_ap.payments import MockBank, PaymentService
from acme_ap.pipeline import Pipeline
from acme_ap.redteam import invoice

ROOT = Path(__file__).resolve().parent.parent


def on_hand(erp, item="WidgetA"):
    return next(r for r in erp.stock_levels() if r["item"] == item)


def test_stock_is_kept_between_runs_and_restored_by_reset(tmp_path):
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="decrement")
    erp.claim_bill("v|N1|", "a", "V", None)
    assert erp.reserve_stock("v|N1|", [("WidgetA", 10)]) == []
    erp.settle_bill("v|N1|", "paid", "ok")
    assert on_hand(erp)["on_hand"] == 5
    again = MockNetSuite(tmp_path / "s.db", keep_ledger=True)                  # the next run: nothing re-seeded, nothing paid twice
    assert on_hand(again)["on_hand"] == 5 and again.stock_policy == "decrement"
    fresh = MockNetSuite(tmp_path / "s.db", keep_ledger=False)                                    # --reset: an empty ledger starts from the opening stock
    assert on_hand(fresh)["on_hand"] == 15


def test_a_scheduled_payment_holds_the_units_and_paying_it_takes_them(tmp_path):
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="decrement")
    erp.claim_bill("v|N1|", "a", "V", None)
    erp.reserve_stock("v|N1|", [("WidgetA", 10)])
    erp.settle_bill("v|N1|", "scheduled", "due in 30 days")
    row = on_hand(erp)
    assert (row["on_hand"], row["reserved"], row["available"]) == (15, 10, 5)  # not yet paid: still on the shelf, but spoken for
    erp.settle_bill("v|N1|", "paid", "released")
    row = on_hand(erp)
    assert (row["on_hand"], row["reserved"], row["available"]) == (5, 0, 5)


def test_settling_twice_takes_the_units_once(tmp_path):
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="decrement")
    erp.claim_bill("v|N1|", "a", "V", None)
    erp.reserve_stock("v|N1|", [("WidgetA", 4)])
    for _ in range(3):
        erp.settle_bill("v|N1|", "paid", "ok")
    assert on_hand(erp)["on_hand"] == 11


def test_a_returned_payment_puts_the_units_back_and_a_failed_one_never_took_them(tmp_path):
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="decrement")
    for n, status in (("N1", "payment_returned"), ("N2", "payment_failed")):
        erp.claim_bill(f"v|{n}|", "a", "V", None)
        erp.reserve_stock(f"v|{n}|", [("WidgetB", 3)])
        if n == "N1":
            erp.settle_bill(f"v|{n}|", "paid", "ok")
            assert on_hand(erp, "WidgetB")["on_hand"] == 7
        else:
            erp.release_stock(f"v|{n}|")                                       # what the payment service does for a payment that fails for good
        erp.settle_bill(f"v|{n}|", status, "bank said no")
    assert on_hand(erp, "WidgetB")["on_hand"] == 10 and on_hand(erp, "WidgetB")["reserved"] == 0


def test_the_next_invoice_sees_what_the_last_one_used(tmp_path):
    """The point of the policy: 10 WidgetA paid, so an invoice for 8 more cannot be approved against the 15 we started with."""
    for name, number, qty in (("a.txt", "INV-4001", 10), ("b.txt", "INV-4002", 8)):
        (tmp_path / name).write_text(invoice(number, lines=[("WidgetA", qty, None)]))
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="decrement")
    svc = PaymentService(tmp_path / "s.db", erp, MockBank(str(tmp_path / "s.db.bank")), today=lambda: date(2026, 10, 27), sleep=lambda s: None)
    first, second = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, sequential=True, payments=svc).run([str(tmp_path / "a.txt")] + [str(tmp_path / "b.txt")]))
    assert first.approval.decision.value == "approve"
    assert second.approval.decision.value == "reject" and any(f.code == "insufficient_stock" for f in second.findings)
    assert on_hand(erp)["available"] == 5                                     # the first is scheduled (not yet paid): its 10 are spoken for


def test_the_default_policy_is_unchanged_and_deducts_nothing(tmp_path):
    erp = MockNetSuite(tmp_path / "s.db")
    erp.claim_bill("v|N1|", "a", "V", None)
    assert erp.reserve_stock("v|N1|", [("WidgetA", 10)]) == []
    erp.settle_bill("v|N1|", "paid", "ok")
    assert on_hand(erp)["on_hand"] == 15 and erp.stock_policy == "cap"


def test_the_stock_command_reports_what_is_left(tmp_path):
    db = tmp_path / "c.db"
    run = lambda *a: subprocess.run([sys.executable, "main.py", *a], cwd=ROOT, capture_output=True, text=True, timeout=120)  # noqa: E731
    run("--offline", "--db", str(db), "--reset", "--stock-policy", "decrement", "--invoice_path", "data/invoices/invoice_1001.txt")
    out = run("stock", "--db", str(db)).stdout
    assert "decrement" in out and "WidgetA" in out and "5" in out


def test_an_unknown_policy_is_refused(tmp_path):
    with pytest.raises(ValueError):
        MockNetSuite(tmp_path / "s.db", stock_policy="subtract")
