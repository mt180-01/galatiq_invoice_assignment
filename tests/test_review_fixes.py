"""Regressions for the problems found in review: the ledger's memory, the duplicate rules, worker planning, Windows, approvals' identity, stock."""
import asyncio
import builtins
import json
import os
import sqlite3
from decimal import Decimal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from acme_ap import runner
from acme_ap.erp import MockNetSuite
from acme_ap.llm import OfflineLLM
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
INV = ROOT / "data" / "invoices"


def cli(tmp_path, *extra, path="data/invoices/invoice_1001.txt"):
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "COLUMNS": "200", "XAI_API_KEY_FILE": "/nonexistent"}
    env.pop("XAI_API_KEY", None)
    p = subprocess.run([sys.executable, "main.py", "--offline", "--db", str(tmp_path / "l.db"), "--outbox", str(tmp_path / "o"), "--invoice_path", path, *extra],
                       cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout + p.stderr


def payments(tmp_path):
    return sqlite3.connect(tmp_path / "l.db").execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0]


# ---- 1. the ledger remembers between runs -------------------------------------------------------------
def test_running_the_cli_twice_never_pays_an_invoice_twice(tmp_path):
    first = cli(tmp_path)
    assert "PAID=1" in first and payments(tmp_path) == 1
    second = cli(tmp_path)
    assert "DUPLICATE=1" in second and "PAID" not in second.split("DUPLICATE=1")[0].split("\n")[-1] and payments(tmp_path) == 1
    assert "already processed in an earlier run" in second                           # and it says why, instead of looking broken


def test_reset_starts_from_an_empty_ledger(tmp_path):
    cli(tmp_path)
    again = cli(tmp_path, "--reset")
    assert "PAID=1" in again and payments(tmp_path) == 1 and "already processed" not in again


def test_the_old_flag_still_parses(tmp_path):
    assert "PAID=1" in cli(tmp_path, "--keep-ledger")


def test_a_benchmark_and_a_demo_server_never_touch_the_real_ledger(tmp_path):
    cli(tmp_path)
    before = payments(tmp_path)
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "XAI_API_KEY_FILE": "/nonexistent"}
    subprocess.run([sys.executable, "main.py", "--offline", "--db", str(tmp_path / "l.db"), "--bench", "300"], cwd=ROOT, capture_output=True, env=env, timeout=300)
    assert payments(tmp_path) == before == 1                                          # the bench used its own scratch database


# ---- 5. the worker planner is live ---------------------------------------------------------------------
def test_a_few_pdfs_do_not_start_a_process_pool(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr("acme_ap.pipeline.ProcessPoolExecutor", lambda *a, **k: started.append(a) or (_ for _ in ()).throw(AssertionError("pool started")))
    erp = MockNetSuite(tmp_path / "w.db")
    pipe = Pipeline(erp, OfflineLLM())                                                   # no explicit workers: auto
    outs = asyncio.run(pipe.run([str(p) for p in sorted(INV.glob("*.pdf"))]))
    assert len(outs) == 3 and not started and pipe.pool_workers == 1


def test_the_plan_scales_with_the_amount_of_pdf_work(tmp_path):
    pipe = Pipeline(MockNetSuite(tmp_path / "w.db"), OfflineLLM())
    assert [pipe._plan_workers(n) for n in (0, 3, 99)] == [0, 0, 0]
    assert pipe._plan_workers(250) == min(os.cpu_count() or 1, 2)
    assert Pipeline(MockNetSuite(tmp_path / "x.db"), OfflineLLM(), workers=4)._plan_workers(1) == 4   # an explicit choice is respected


# ---- 4. Windows: no `resource` module --------------------------------------------------------------------
def test_a_batch_runs_where_the_unix_only_resource_module_does_not_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "resource", None)
    assert runner.cpu_seconds() >= 0
    args = SimpleNamespace(db=str(tmp_path / "win.db"), keep_ledger=False, llm_concurrency=2, offline=True)
    outs, wall, cpu = asyncio.run(runner.run_batch([str(INV / "invoice_1001.txt")], args, 1))
    assert outs[0].status == "PAID" and cpu >= 0


def test_importing_the_modules_does_not_need_resource(tmp_path):
    code = "import sys; sys.modules['resource'] = None; import acme_ap.runner as r; assert r.resource is None; import main"
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONWARNINGS": "ignore"})
    assert p.returncode == 0, p.stderr


# ---- 3. what counts as the same bill ------------------------------------------------------------------
def invoice_file(tmp_path, name, **over):
    data = {"invoice_number": "INV-7001", "vendor": "Widgets Inc.", "date": "2026-02-01", "due_date": "2026-03-01", "currency": "USD", "payment_terms": "Net 30",
            "line_items": [{"item": "WidgetA", "quantity": 2, "unit_price": 250, "amount": 500}], "subtotal": 500, "tax_amount": 0, "total": 500}
    data.update(over)
    f = tmp_path / name
    f.write_text(json.dumps(data))
    return f


def process(erp, *files):
    return asyncio.run(Pipeline(erp, OfflineLLM(), workers=1).run([str(f) for f in files]))


def test_a_corrected_resubmission_of_a_rejected_invoice_is_processed(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db", keep_ledger=True)
    bad = invoice_file(tmp_path, "first.json", line_items=[{"item": "WidgetC", "quantity": 2, "unit_price": 250, "amount": 500}])      # unknown item: rejected
    assert process(erp, bad)[0].status == "REJECT"
    good = invoice_file(tmp_path, "second.json")                                                                                           # same number, corrected
    assert process(erp, good)[0].status == "PAID"
    assert process(erp, invoice_file(tmp_path, "third.json"))[0].status == "DUPLICATE"                                                    # but once it is paid, it is paid
    audit = [r[0] for r in erp._conn().execute("SELECT detail FROM audit_log WHERE stage='resubmitted'")]
    assert len(audit) == 1 and "first.json" in audit[0] and "second.json" in audit[0]


def test_an_invoice_still_in_progress_or_paid_is_never_reclaimed(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db")
    assert erp.claim_bill("v|N|", "a.json", "V", None) is None
    for status in ("received", "paid", "scheduled", "pending_approval", "payment_held", "payment_returned"):
        erp.settle_bill("v|N|", status, "x")
        assert erp.claim_bill("v|N|", "b.json", "V", None) == "a.json", status
    for status in ("reject", "rejected", "blocked", "expired"):
        erp.settle_bill("v|N|", status, "x")
        assert erp.claim_bill("v|N|", "c.json", "V", None) is None, status


def test_a_vendors_other_name_does_not_get_a_duplicate_past_the_check(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db", keep_ledger=True)
    first = invoice_file(tmp_path, "a.json", vendor="QuickShip Distributers", invoice_number="INV-8001")
    assert process(erp, first)[0].status == "PAID"
    again = invoice_file(tmp_path, "b.json", vendor="FastShip Ltd.", invoice_number="INV-8001")                                              # the vendor's alias
    assert process(erp, again)[0].status == "DUPLICATE"


def test_a_lookalike_vendor_spelling_with_the_same_number_and_total_goes_to_a_person(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db", keep_ledger=True)
    assert process(erp, invoice_file(tmp_path, "a.json", invoice_number="INV-8002"))[0].status == "PAID"
    (o,) = process(erp, invoice_file(tmp_path, "b.json", invoice_number="INV-8002", vendor="Widget Inc"))                                  # one letter off
    codes = {f.code for f in o.findings}
    assert o.status == "ESCALATE" and "possible_duplicate" in codes and not o.paid
    assert o.followup is None                                                                                                               # the sender is not asked to confirm
    different_total = invoice_file(tmp_path, "c.json", invoice_number="INV-8002", vendor="Widget Inc", total=900, subtotal=900,
                                   line_items=[{"item": "WidgetA", "quantity": 2, "unit_price": 450, "amount": 900}])
    assert "possible_duplicate" not in {f.code for f in process(erp, different_total)[0].findings}                                         # a different bill is not accused


def test_the_duplicate_lookup_uses_an_index_not_a_scan(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db")
    plan = " ".join(r[3] for r in erp._conn().execute("EXPLAIN QUERY PLAN SELECT bill_key FROM vendor_bills WHERE number = ? AND bill_key != ?", ("N", "k")))
    assert "vendor_bills_number" in plan


def test_an_old_ledger_without_the_number_column_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE vendor_bills (bill_key TEXT PRIMARY KEY, source TEXT, vendor TEXT, total TEXT, status TEXT DEFAULT 'received', reasoning TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    con.execute("INSERT INTO vendor_bills (bill_key, source, vendor, total, status) VALUES ('widgetsinc|INV-9|', 'old.json', 'Widgets Inc.', '500', 'paid')")
    con.commit(); con.close()
    erp = MockNetSuite(db, keep_ledger=True)
    assert erp.similar_bills("INV-9", Decimal("500"), "Widget Inc", "other|INV-9|")[0]["vendor"] == "Widgets Inc."                         # the old row is findable


def test_a_claim_left_behind_by_a_crashed_run_does_not_block_the_invoice_forever(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db")
    assert erp.claim_bill("v|N|", "crashed.json", "V", None) is None                     # claimed, then the process died: status stays 'received'
    assert erp.claim_bill("v|N|", "retry.json", "V", None) == "crashed.json"            # a moment later it is genuinely in progress: duplicate
    erp._conn().execute("UPDATE vendor_bills SET created_at = datetime('now', '-30 minutes') WHERE bill_key='v|N|'")
    erp._conn().commit()
    assert erp.claim_bill("v|N|", "retry.json", "V", None) is None                      # half an hour on, nothing is working on it: reclaimed
    assert erp._conn().execute("SELECT detail FROM audit_log WHERE stage='resubmitted'").fetchone()[0].startswith("earlier attempt (crashed.json) ended abandoned")


def test_an_identical_copy_of_a_rejected_invoice_is_a_duplicate_not_a_second_attempt(tmp_path):
    """Found by the end-to-end run: invoice_1013 arrives as a JSON and a PDF of the same bill; once the first is rejected the second must not be reprocessed."""
    erp = MockNetSuite(tmp_path / "d.db", keep_ledger=True)
    bad = dict(line_items=[{"item": "WidgetC", "quantity": 2, "unit_price": 250, "amount": 500}])
    first, same_again = invoice_file(tmp_path, "a.json", **bad), invoice_file(tmp_path, "b.json", **bad)
    assert process(erp, first)[0].status == "REJECT"
    assert process(erp, same_again)[0].status == "DUPLICATE"                                       # nothing changed, so nothing to reconsider
    corrected = invoice_file(tmp_path, "c.json")                                                    # something changed: now it is a new attempt
    assert process(erp, corrected)[0].status == "PAID"
    assert erp._conn().execute("SELECT COUNT(*) FROM audit_log WHERE stage='resubmitted'").fetchone()[0] == 1


def test_a_crashed_claim_is_retried_even_with_identical_content(tmp_path):
    erp = MockNetSuite(tmp_path / "d.db")
    erp.claim_bill("v|N|", "a.json", "V", None, content="same")
    erp._conn().execute("UPDATE vendor_bills SET created_at = datetime('now', '-30 minutes')")
    erp._conn().commit()
    assert erp.claim_bill("v|N|", "b.json", "V", None, content="same") is None                      # an unfinished run proves nothing about the content


def test_a_pdf_and_a_json_of_the_same_invoice_have_the_same_content_hash():
    from acme_ap.approvals.tokens import evidence_hash
    from acme_ap.ingestion import parse_file
    for stem in ("invoice_1013", ):
        a, _ = parse_file(str(INV / f"{stem}.json"))
        b, _ = parse_file(str(INV / f"{stem}.pdf"))
        assert evidence_hash(a) == evidence_hash(b), "250.0 versus 250.00 must not make two copies of one bill look different"
    from acme_ap.models import Invoice
    assert evidence_hash(Invoice(invoice_number="X", vendor="V", total=Decimal("100.00"))) == evidence_hash(Invoice(invoice_number="X", vendor="V", total=Decimal("100")))
    assert evidence_hash(Invoice(invoice_number="X", vendor="V", total=Decimal("100"))) != evidence_hash(Invoice(invoice_number="X", vendor="V", total=Decimal("101")))


# ---- 2. stock: per-invoice by default (the brief's table), cumulative on request -------------------------------
def widget(tmp_path, name, number, qty=2, item="WidgetA", price=250):
    return invoice_file(tmp_path, name, invoice_number=number, line_items=[{"item": item, "quantity": qty, "unit_price": price, "amount": qty * price}],
                        subtotal=qty * price, total=qty * price)


def test_the_default_checks_each_invoice_on_its_own_as_the_brief_expects(tmp_path):
    """The brief lists INV-1001, 1004 and 1006 as 'within stock', yet they bill 10 + 3 + 5 = 18 WidgetA against 15 on hand: stock is a per-invoice cap."""
    erp = MockNetSuite(tmp_path / "s.db")
    assert erp.stock_policy == "cap"
    outs = {o.invoice.invoice_number: o.status for o in process(erp, INV / "invoice_1001.txt", INV / "invoice_1004.json", INV / "invoice_1006.csv")}
    assert outs == {"INV-1001": "PAID", "INV-1004": "PAID", "INV-1006": "PAID"}


def test_under_the_reserve_policy_earlier_approvals_use_up_stock(tmp_path):
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="reserve")
    status = {}
    for f in (INV / "invoice_1001.txt", INV / "invoice_1004.json", INV / "invoice_1006.csv"):               # one after another, as a person would
        (o,) = process(erp, f)
        status[o.invoice.invoice_number] = o
    assert [status[n].status for n in ("INV-1001", "INV-1004", "INV-1006")] == ["PAID", "PAID", "REJECT"]
    assert "WidgetA: 5 billed but only 2 in stock" in status["INV-1006"].approval.reasoning and not status["INV-1006"].paid      # validation sees only what is unclaimed
    assert erp.lookup_item("WidgetA").stock == 2                                                              # 15 on hand, 13 spoken for


def test_racing_invoices_cannot_both_take_the_last_units(tmp_path):
    """12 invoices want 2 WidgetA each (24) from 15 on hand, all at once. Exactly 7 can be approved, whatever the timing."""
    erp = MockNetSuite(tmp_path / "s.db", stock_policy="reserve")
    files = [widget(tmp_path, f"w{i}.json", f"INV-9{i:03d}") for i in range(12)]
    outs = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, llm_concurrency=12, bank_concurrency=12).run([str(f) for f in files]))
    assert sum(o.status == "PAID" for o in outs) == 7 and sum(o.status == "REJECT" for o in outs) == 5
    assert erp._conn().execute("SELECT SUM(qty) FROM stock_reservations").fetchone()[0] == 14                 # never more than on hand
    assert erp._conn().execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0] == 7


def test_the_policy_is_stored_with_the_database_so_other_tools_follow_it(tmp_path):
    MockNetSuite(tmp_path / "s.db", stock_policy="reserve")
    assert MockNetSuite(tmp_path / "s.db", keep_ledger=True).stock_policy == "reserve"                         # what approve.py, pay.py and the web page construct
    assert MockNetSuite(tmp_path / "s.db", keep_ledger=True, stock_policy="cap").stock_policy == "cap"        # and it can be changed on purpose
    with pytest.raises(ValueError):
        MockNetSuite(tmp_path / "s.db", stock_policy="decrement")


def test_a_person_approving_cannot_spend_stock_an_earlier_approval_already_took(tmp_path):
    from acme_ap.approvals import ApprovalService
    db = tmp_path / "s.db"
    erp = MockNetSuite(db, stock_policy="reserve")
    svc = ApprovalService(db, erp)
    big = lambda n: invoice_file(tmp_path, f"big{n}.json", invoice_number=f"INV-95{n}", line_items=[{"item": "WidgetA", "quantity": 15, "unit_price": 250, "amount": 3750},
                                 {"item": "WidgetB", "quantity": 10, "unit_price": 500, "amount": 5000}, {"item": "GadgetX", "quantity": 5, "unit_price": 750, "amount": 3750}],
                                 subtotal=12500, total=12500)                                                    # over the sign-off limit: both wait for the VP, both fit stock alone
    outs = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, approvals=svc).run([str(big(1)), str(big(2))]))
    assert all(o.status == "AWAITING" for o in outs)
    first, second = sorted(outs, key=lambda o: o.approval_request)
    assert svc.respond(svc.tokens_for(first.approval_request)["approve"]).paid
    res = svc.respond(svc.tokens_for(second.approval_request)["approve"])
    assert not res.ok and "re-check failed" in res.message and "stock" in res.message                          # the stock is gone: blocked, not paid
    assert erp._conn().execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0] == 1


def test_a_payment_that_fails_for_good_gives_its_stock_back(tmp_path):
    from acme_ap.payments import MockBank, PaymentService
    db = tmp_path / "s.db"
    erp = MockNetSuite(db, stock_policy="reserve")
    bank = MockBank(str(db) + ".bank")
    svc = PaymentService(db, erp, bank, sleep=lambda s: None)
    bank.reject_accounts.add(svc._db.execute("SELECT account FROM vendor_accounts WHERE vendor='widgetsinc'").fetchone()[0])          # the bank will refuse this vendor
    (o,) = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, payments=svc).run([str(widget(tmp_path, "a.json", "INV-9901", qty=10))]))
    assert o.status == "PAYMENT_FAILED"
    assert erp.lookup_item("WidgetA").stock == 15                                                               # the 10 units are free again


def test_stock_policy_flag_reaches_the_database_from_the_cli(tmp_path):
    out = cli(tmp_path, "--stock-policy", "reserve", path="data/invoices/invoice_1001.txt")
    assert "PAID=1" in out
    assert sqlite3.connect(tmp_path / "l.db").execute("SELECT v FROM erp_settings WHERE k='stock_policy'").fetchone()[0] == "reserve"
