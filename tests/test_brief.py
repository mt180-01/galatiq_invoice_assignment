"""The assignment's four stages, checked one by one against the provided invoices."""
import asyncio
import logging
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import parse_file
from acme_ap.llm import OfflineLLM
from acme_ap.models import Decision
from acme_ap.pipeline import Pipeline

DATA = Path(__file__).resolve().parent.parent / "data" / "invoices"


@pytest.fixture
def run(tmp_path):
    db = tmp_path / "brief.db"

    def go(*names, **kw):
        erp = MockNetSuite(db)
        outs = asyncio.run(Pipeline(erp, kw.get("llm", OfflineLLM()), workers=1).run([str(DATA / n) for n in names]))
        return {Path(o.source_path).name: o for o in outs}, db
    return go


# ---- 1. Ingestion -------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,vendor,total,items,due", [
    ("invoice_1001.txt", "Widgets Inc.", "5000.00", [("WidgetA", 10), ("WidgetB", 5)], "2026-02-01"),
    ("invoice_1011.pdf", "Summit Manufacturing Co.", "3000.00", [("WidgetA", 6), ("WidgetB", 3)], "2026-02-20"),   # a PDF
    ("invoice_1008.txt", "NoProd Industries", "9900.00", [("SuperGizmo", 12), ("MegaSprocket", 6)], "2026-01-20"),
])
def test_vendor_amount_items_and_due_date_are_extracted(name, vendor, total, items, due):
    inv, _ = parse_file(str(DATA / name))
    assert (inv.vendor, inv.total, [(li.item, li.quantity) for li in inv.line_items], inv.due_date) == (vendor, Decimal(total), items, due)


def test_typos_missing_data_and_fraud_are_handled(run):
    typo, _ = parse_file(str(DATA / "invoice_1010.txt"))          # "Vndr"/"Pymnt"-style typos and an OCR-ish layout
    assert typo.vendor == "Consolidated Materials Group" and typo.total == Decimal("7185.00")
    outs, _ = run("invoice_1009.json", "invoice_1003.txt")
    missing = outs["invoice_1009.json"]
    assert {"missing_vendor", "invalid_quantity", "negative_total"} <= {f.code for f in missing.findings}    # missing data, flagged not guessed
    fraud = outs["invoice_1003.txt"]
    assert "fraud_indicators" in {f.code for f in fraud.findings} and fraud.status == "REJECT"


# ---- 2. Validation ------------------------------------------------------------------------------------
def test_validation_uses_the_sqlite_inventory_and_flags_mismatches(run):
    outs, db = run("invoice_1002.txt", "invoice_1008.txt", "invoice_1001.txt")
    assert sqlite3.connect(db).execute("SELECT item, stock FROM inventory WHERE item='GadgetX'").fetchone() == ("GadgetX", 5)
    assert [f.message for f in outs["invoice_1002.txt"].findings if f.code == "insufficient_stock"] == ["GadgetX: 20 billed but only 5 in stock"]
    assert {f.code for f in outs["invoice_1008.txt"].findings} >= {"unknown_item"}
    assert not [f for f in outs["invoice_1001.txt"].findings if f.severity.value == "error"]


# ---- 3. Approval --------------------------------------------------------------------------------------
def test_over_10k_needs_additional_scrutiny(run, tmp_path):
    outs, _ = run("invoice_1013.json", "invoice_1015.csv")        # 22.5K (also has errors) and 6.5K clean
    assert outs["invoice_1015.csv"].approval.decision is Decision.APPROVE
    big = Path(__file__).resolve().parent.parent / "data" / "approvals_demo" / "invoice_3001.json"       # 12.5K, otherwise spotless
    (o,) = asyncio.run(Pipeline(MockNetSuite(tmp_path / "big.db"), OfflineLLM(), workers=1).run([str(big)]))
    assert o.approval.decision is Decision.ESCALATE and "human sign-off required" in o.approval.reasoning and not o.paid


def test_the_reviewer_reflects_and_revises_its_own_draft(run):
    outs, _ = run("invoice_1004_revised.json")
    a = outs["invoice_1004_revised.json"].approval
    assert a.decision is Decision.ESCALATE and a.rounds == 2                 # first read said approve; the critique caught the hold
    assert any("conflicts with blocking findings" in c for c in a.critiques)
    plain, _ = run("invoice_1001.txt")
    assert plain["invoice_1001.txt"].approval.rounds == 1 and not plain["invoice_1001.txt"].approval.critiques   # nothing to correct


# ---- 4. Payment ---------------------------------------------------------------------------------------
def test_mock_payment_is_called_only_for_approved_invoices(run, monkeypatch):
    calls = []
    import acme_ap.payments.bank as bank
    monkeypatch.setattr(bank, "mock_payment", lambda vendor, amount: (calls.append((vendor, amount)), {"status": "success"})[1])
    outs, db = run("invoice_1001.txt", "invoice_1002.txt", "invoice_1004_revised.json")
    assert calls == [("Widgets Inc.", Decimal("5000.00"))]                   # approved: paid; rejected and escalated: not
    assert outs["invoice_1001.txt"].paid and not outs["invoice_1002.txt"].paid and not outs["invoice_1004_revised.json"].paid


def test_rejection_is_logged_with_its_reasoning(run, caplog):
    with caplog.at_level(logging.INFO, logger="acme.pipeline"):
        outs, db = run("invoice_1002.txt")
    assert any("reject" in r.detail and "GadgetX: 20 billed but only 5 in stock" in r.detail for r in caplog.records if hasattr(r, "detail"))
    row = sqlite3.connect(db).execute("SELECT stage, detail FROM audit_log WHERE detail LIKE 'reject%'").fetchone()
    assert row and "only 5 in stock" in row[1]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0] == 0
