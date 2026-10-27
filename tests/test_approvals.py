import asyncio
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from acme_ap.approvals import ApprovalService, route
from acme_ap.approvals.tokens import TokenError, make_token, read_token, utcnow
from acme_ap.erp import MockNetSuite
from acme_ap.llm import OfflineLLM
from acme_ap.models import Approval, Decision, Finding, Invoice, Outcome, Severity
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "data" / "approvals_demo" / "invoice_3001.json"


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "a.db"
    erp = MockNetSuite(db)
    return db, erp, ApprovalService(db, erp)


def run(erp, svc, paths):
    return asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, approvals=svc).run([str(p) for p in paths]))


def payments(db):
    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0]


def outcome(total, *findings, decision=Decision.ESCALATE):
    inv = Invoice(invoice_number="X", vendor="V", total=Decimal(total))
    return Outcome(source_path="x", invoice=inv, findings=list(findings), approval=Approval(decision=decision, reasoning="r"))


def hold(code):
    return Finding(code=code, severity=Severity.HOLD, message=code)


# ---- routing ------------------------------------------------------------------------------------
def test_routing_by_amount_and_reason():
    assert route(outcome("20000")).role == "VP Finance"
    assert route(outcome("60000")).role == "CFO"
    assert route(outcome("500", hold("unknown_vendor"))).role == "Procurement"
    assert route(outcome("500", hold("foreign_currency"))).role == "Treasury"
    assert route(outcome("500", hold("ocr_source"))).role == "AP Manager"
    assert route(outcome("500", hold("pressure_language"))).role == "AP Manager"


def test_nothing_to_approve_for_rejected_paid_or_unreadable():
    assert route(outcome("500", decision=Decision.REJECT)) is None
    assert route(outcome("500", hold("unreadable_document"))).approvable is False


# ---- tokens -------------------------------------------------------------------------------------
def test_token_tamper_and_expiry():
    now = utcnow()
    tok = make_token(b"k" * 32, 1, "approve", "abc", now + timedelta(days=1))
    assert read_token(b"k" * 32, tok, now)["a"] == "approve"
    with pytest.raises(TokenError):
        read_token(b"x" * 32, tok, now)                      # wrong key
    with pytest.raises(TokenError):
        read_token(b"k" * 32, tok, now + timedelta(days=2))  # expired
    body, sig = tok.split(".")
    with pytest.raises(TokenError):
        read_token(b"k" * 32, body[:-2] + "AA." + sig, now)   # edited payload


# ---- end to end ---------------------------------------------------------------------------------
def test_over_limit_invoice_waits_then_pays_exactly_once(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    assert o.status == "AWAITING" and o.awaiting == "VP Finance" and not o.paid
    assert payments(db) == 0                                   # nothing moves before a person decides
    tok = svc.tokens_for(o.approval_request)["approve"]
    assert svc.respond(tok, "vp@x").paid and payments(db) == 1
    again = svc.respond(tok, "vp@x")                           # double-click / replay
    assert not again.ok and "already approved" in again.message and payments(db) == 1


def test_reject_pays_nothing_and_is_recorded(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    res = svc.respond(svc.tokens_for(o.approval_request)["reject"], "vp@x", "wrong PO")
    assert res.ok and not res.paid and payments(db) == 0
    assert sqlite3.connect(db).execute("SELECT status FROM vendor_bills").fetchone()[0] == "rejected"
    assert any(e[1] == "rejected" for e in svc.events(o.approval_request))


def test_approve_is_refused_if_invoice_changed_after_request(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    tok = svc.tokens_for(o.approval_request)["approve"]
    con = sqlite3.connect(db)  # someone edits the stored invoice after the card was sent
    inv = Invoice.model_validate_json(con.execute("SELECT invoice_json FROM approval_requests").fetchone()[0])
    inv.total = Decimal("99999")
    con.execute("UPDATE approval_requests SET invoice_json=?", (inv.model_dump_json(),)); con.commit()
    res = svc.respond(tok, "vp@x")
    assert not res.ok and "changed" in res.message and payments(db) == 0


def test_approval_is_rechecked_so_stale_stock_cannot_release_money(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    con = sqlite3.connect(db); con.execute("UPDATE inventory SET stock=0 WHERE item='WidgetA'"); con.commit()
    res = svc.respond(svc.tokens_for(o.approval_request)["approve"], "vp@x")
    assert not res.ok and "re-check failed" in res.message and payments(db) == 0


def test_reminder_escalation_and_expiry_never_pay(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    t0 = utcnow()
    assert svc.tick(t0 + timedelta(hours=1)) == []
    assert "reminder" in svc.tick(t0 + timedelta(hours=25))[0]
    assert "escalated VP Finance -> CFO" in svc.tick(t0 + timedelta(hours=49))[0]
    assert "expired" in svc.tick(t0 + timedelta(days=6))[0]
    assert payments(db) == 0
    res = svc.respond(svc.tokens_for(o.approval_request)["approve"], "cfo@x", now=t0 + timedelta(days=6))
    assert not res.ok  # token and request are both dead


def test_card_is_short_and_explains_why(env):
    _, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    card = o.approval_card
    assert "WHY YOU" in card and "SYSTEM RECOMMENDATION" in card and "APPROVE:" in card and "REJECT:" in card
    assert len(card.splitlines()) < 30
