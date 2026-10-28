"""The commands people actually type, run as subprocesses against a scratch database."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args, ok=True):
    p = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True, timeout=120,
                       env={"PATH": "/usr/bin:/bin", "PYTHONWARNINGS": "ignore", "COLUMNS": "200"})
    assert (p.returncode == 0) == ok, p.stdout + p.stderr
    return p.stdout + p.stderr


def test_demo_runs_the_whole_story():
    out = run("main.py", "demo")
    for n in range(1, 7):
        assert f"Step {n}." in out
    assert "approved by vp-finance@acmecorp.example; paid" in out
    assert re.search(r"settled \d+, returned 0, still in flight 0; issues: none", out)
    # the claims the narration makes in steps 5 and 6 must be what actually happened (a fraud rule once held the two timed invoices and nothing noticed)
    assert re.search(r"scheduled for later : 1 \(1,500\.00\)", out) and "early-payment discounts captured : 15.00" in out
    assert re.search(r"gadgetsco\|INV-3002\|: release \d{4}-\d{2}-\d{2} via ACH", out) and re.search(r": sent 1 payment\(s\) \(ACH-", out)


def test_main_approver_and_treasury_commands_work_together(tmp_path):
    db, outbox = str(tmp_path / "t.db"), str(tmp_path / "out")
    out = run("main.py", "--offline", "--db", db, "--outbox", outbox, "--invoice_path", "data/approvals_demo")
    assert "AWAITING" in out
    listing = run("approve.py", "--db", db, "list")
    assert "VP Finance" in listing and "AP Manager" in listing
    assert list((tmp_path / "out" / "approvals").glob("request_*.txt"))
    assert run("pay.py", "--db", db, "status").strip() == ""                    # nothing paid before a person decides
    assert "nothing to verify" in run("pay.py", "--db", db, "account", "verify", "Widgets Inc.", "--by", "a@x")
    assert "NOT usable" in run("pay.py", "--db", db, "account", "set", "Widgets Inc.", "acct-NEW1", "--by", "a@x")
    assert "refused" in run("pay.py", "--db", db, "account", "verify", "Widgets Inc.", "--by", "a@x")
    assert "verified by b@x" in run("pay.py", "--db", db, "account", "verify", "Widgets Inc.", "--by", "b@x")


def test_bad_path_fails_cleanly():
    out = run("main.py", "--invoice_path", "does/not/exist", ok=False)
    assert "does not exist" in out and "Traceback" not in out
