import asyncio
import json
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
    assert svc.respond(tok, "vp-finance@acmecorp.example").paid and payments(db) == 1
    again = svc.respond(tok, "vp-finance@acmecorp.example")                           # double-click / replay
    assert not again.ok and "already approved" in again.message and payments(db) == 1


def test_reject_pays_nothing_and_is_recorded(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    res = svc.respond(svc.tokens_for(o.approval_request)["reject"], "vp-finance@acmecorp.example", "wrong PO")
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
    res = svc.respond(tok, "vp-finance@acmecorp.example")
    assert not res.ok and "changed" in res.message and payments(db) == 0


def test_approval_is_rechecked_so_stale_stock_cannot_release_money(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    con = sqlite3.connect(db); con.execute("UPDATE inventory SET stock=0 WHERE item='WidgetA'"); con.commit()
    res = svc.respond(svc.tokens_for(o.approval_request)["approve"], "vp-finance@acmecorp.example")
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
    res = svc.respond(svc.tokens_for(o.approval_request)["approve"], "cfo@acmecorp.example", now=t0 + timedelta(days=6))
    assert not res.ok  # token and request are both dead


def test_card_is_short_and_explains_why(env):
    _, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    card = o.approval_card
    assert "WHY YOU" in card and "SYSTEM RECOMMENDATION" in card and "APPROVE:" in card and "REJECT:" in card
    assert len(card.splitlines()) < 30


def test_many_escalations_at_once_do_not_corrupt_each_other(tmp_path, env):
    """Regression: the shared connection's row type was flipped per call, so parallel escalations crashed (found by the 20,000-doc benchmark)."""
    import json
    db, erp, svc = env
    base = json.loads(DEMO.read_text())
    paths = []
    for i in range(300):
        f = tmp_path / f"invoice_{4000 + i}.json"
        f.write_text(json.dumps(base | {"invoice_number": f"INV-{4000 + i}"}))
        paths.append(f)
    outs = run(erp, svc, paths)
    assert not [o.error for o in outs if o.error]
    assert sorted(o.approval_request for o in outs) == list(range(1, 301))
    assert all(o.approval_card and o.awaiting == "VP Finance" for o in outs)


def test_a_link_is_issued_to_one_mailbox_and_a_typed_name_cannot_override_it(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])                                          # with VP Finance
    tok = svc.tokens_for(o.approval_request)["approve"]
    for claimed in ("attacker@evil.example", "procurement@acmecorp.example", "cfo@acmecorp.example", "vp-finance@acmecorp.example.evil.com"):
        res = svc.respond(tok, claimed)
        assert not res.ok and "was issued to vp-finance@acmecorp.example" in res.message, claimed
    assert payments(db) == 0
    assert any(e[1] != "refused" for e in svc.events(o.approval_request))   # nothing was decided by any of those attempts
    assert svc.respond(tok, "  VP-Finance@AcmeCorp.example ").paid           # the holder may state who they are, case and spacing ignored


def test_the_holder_need_not_type_anything(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    res = svc.respond(svc.tokens_for(o.approval_request)["approve"])
    assert res.paid and svc._row(o.approval_request)["decided_by"] == "vp-finance@acmecorp.example"          # identity came from the link


def test_a_link_for_someone_who_may_not_decide_is_refused_even_if_validly_signed(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    stranger = svc.tokens_for(o.approval_request, approver="procurement@acmecorp.example")["approve"]     # genuine signature, wrong person for a VP request
    res = svc.respond(stranger)
    assert not res.ok and "not authorized" in res.message and payments(db) == 0
    assert any(e[1] == "refused" for e in svc.events(o.approval_request))                                    # and the attempt is on the audit trail
    senior = svc.tokens_for(o.approval_request, approver="CFO@acmecorp.example")["approve"]                  # a more senior person, with a link issued to them
    assert svc.respond(senior).paid


def test_editing_who_a_link_was_issued_to_breaks_its_signature(env):
    import base64
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    body, sig = svc.tokens_for(o.approval_request)["approve"].split(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    data["w"] = "ceo@acmecorp.example"
    forged = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=") + "." + sig
    res = svc.respond(forged)
    assert not res.ok and "signature" in res.message and payments(db) == 0


def test_the_signing_key_is_not_in_the_database_and_other_keys_cannot_mint_links(env, monkeypatch):
    db, erp, svc = env
    run(erp, svc, [DEMO])
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM meta WHERE k='secret'").fetchone()[0] == 0
    assert "test-secret-not-for-real-use" not in Path(db).read_bytes().decode("latin-1")
    monkeypatch.setenv("ACME_APPROVAL_SECRET", "someone-elses-key")
    other = ApprovalService(db, erp, reset=False)
    mint = other.tokens_for(1)["approve"]                                                                    # signed with a different key...
    monkeypatch.setenv("ACME_APPROVAL_SECRET", "test-secret-not-for-real-use")
    assert not ApprovalService(db, erp, reset=False).respond(mint).ok                                         # ...so the real service refuses it


def test_an_old_database_that_kept_the_key_inside_loses_it(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO meta VALUES ('secret', 'deadbeef')")
    con.commit(); con.close()
    ApprovalService(db, MockNetSuite(db), reset=False)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM meta WHERE k='secret'").fetchone()[0] == 0


def test_the_secret_file_is_created_private_when_no_env_key_is_set(tmp_path, monkeypatch):
    import stat
    from acme_ap.approvals.service import load_secret
    monkeypatch.delenv("ACME_APPROVAL_SECRET")
    monkeypatch.setenv("ACME_APPROVAL_SECRET_FILE", str(tmp_path / "cfg" / "key"))
    first = load_secret()
    path = tmp_path / "cfg" / "key"
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert load_secret() == first and len(first) == 32                                                        # stable across processes


def test_after_escalation_the_old_and_the_new_owners_links_both_work_but_only_once(env):
    db, erp, svc = env
    (o,) = run(erp, svc, [DEMO])
    t0 = utcnow()
    vp_link = svc.tokens_for(o.approval_request, t0)["approve"]                                              # the VP's link, issued before escalation
    svc.tick(t0 + timedelta(hours=50))                                                                        # VP Finance -> CFO
    cfo_link = svc.tokens_for(o.approval_request, t0)["approve"]                                              # default now: the CFO's mailbox
    assert svc.respond(cfo_link, now=t0 + timedelta(hours=51)).paid
    again = svc.respond(vp_link, now=t0 + timedelta(hours=52))
    assert not again.ok and "already approved" in again.message and payments(db) == 1


def test_authorization_sets():
    from acme_ap.approvals.routing import authorized
    assert authorized("VP Finance") == {"vp-finance@acmecorp.example", "cfo@acmecorp.example", "ceo@acmecorp.example"}
    assert "vp-finance@acmecorp.example" in authorized("CFO", "VP Finance")        # the original owner keeps the right
    assert "vp-finance@acmecorp.example" not in authorized("CFO")
    assert authorized("CEO") == {"ceo@acmecorp.example"}
