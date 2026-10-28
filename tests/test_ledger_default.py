"""The most important correctness property: running the same invoice twice pays it once. Every way in must keep the ledger unless told otherwise."""
import asyncio
import inspect
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from acme_ap.approvals import ApprovalService
from acme_ap.erp import MockNetSuite
from acme_ap.payments import MockBank, PaymentService
from acme_ap.runner import run_batch

ROOT = Path(__file__).resolve().parent.parent
INVOICE = ROOT / "data" / "invoices" / "invoice_1001.txt"


def count(db, table):
    with sqlite3.connect(db) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def cli(db, *extra):
    p = subprocess.run([sys.executable, "main.py", "--offline", "--db", str(db), "--invoice_path", str(INVOICE), *extra], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


def test_the_command_line_run_twice_pays_once(tmp_path):
    db = tmp_path / "l.db"
    first = cli(db)
    second = cli(db)                                                     # the same command again: no --reset
    assert "PAID" in first and "DUPLICATE" in second and "PAID" not in second
    assert count(db, "vendor_payments") == 1 and count(db, "payments") == 1
    with sqlite3.connect(str(db) + ".bank") as c:                        # and the bank itself was asked once
        assert c.execute("SELECT COUNT(*) FROM bank_payments").fetchone()[0] == 1


def test_reset_is_the_only_way_to_pay_it_again(tmp_path):
    db = tmp_path / "l.db"
    cli(db)
    again = cli(db, "--reset")
    assert "PAID" in again and "DUPLICATE" not in again and count(db, "vendor_payments") == 1   # an empty ledger: the first payment of a new ledger


def test_a_caller_that_says_nothing_about_the_ledger_keeps_it(tmp_path):
    """run_batch without a keep_ledger setting (a script, a test, a new entry point) must not wipe anything."""
    db = str(tmp_path / "l.db")
    args = SimpleNamespace(db=db, llm_concurrency=4, offline=True, simulate=False, grok=False)
    statuses = [asyncio.run(run_batch([str(INVOICE)], args, 1))[0][0].status for _ in range(2)]
    assert statuses == ["PAID", "DUPLICATE"] and count(db, "vendor_payments") == 1


def test_the_classes_default_to_keeping_what_they_hold(tmp_path):
    db = tmp_path / "c.db"
    erp = MockNetSuite(db)
    erp.claim_bill("v|N1|", "a.json", "V", None)
    erp.settle_bill("v|N1|", "paid", "ok", ("V", 5, "ref-1"))
    assert MockNetSuite(db).claim_bill("v|N1|", "b.json", "V", None) == "a.json"        # a second open of the same database still knows it
    assert inspect.signature(MockNetSuite).parameters["keep_ledger"].default is True
    assert inspect.signature(PaymentService).parameters["reset"].default is False
    assert inspect.signature(ApprovalService).parameters["reset"].default is False
    assert inspect.signature(MockBank).parameters["reset"].default is False


def test_the_demo_paths_do_not_wipe_a_real_ledger(tmp_path, monkeypatch):
    """`serve --demo --db <your ledger>` used to replace what the database held. It now only adds to it; only the demo's own scratch file is recreated."""
    db = str(tmp_path / "real.db")
    args = SimpleNamespace(db=db, keep_ledger=True, llm_concurrency=4, offline=True, simulate=False, grok=False)
    asyncio.run(run_batch([str(INVOICE)], args, 1))
    asyncio.run(run_batch([str(ROOT / "data" / "invoices" / "invoice_1004.json")], args, 1))   # what a demo load does on top
    assert count(db, "vendor_payments") == 2
    assert count(db, "vendor_bills") >= 2


def test_nothing_in_the_code_turns_the_wipe_on_by_accident():
    """Wiping the ledger is allowed in exactly the places that mean it. A new `keep_ledger=False` or `reset=True` has to be added here on purpose."""
    allowed = {"main.py": 1}                         # the benchmark's scratch database
    found = {}
    for f in [*(ROOT / "acme_ap").rglob("*.py"), ROOT / "main.py", ROOT / "approve.py", ROOT / "pay.py"]:
        n = len(re.findall(r"keep_ledger\s*=\s*False|reset\s*=\s*True|keep_ledger\s*=\s*[^\n]*,\s*False", f.read_text()))
        if n:
            found[str(f.relative_to(ROOT))] = n
    assert found == {k: v for k, v in allowed.items() if v}, found
