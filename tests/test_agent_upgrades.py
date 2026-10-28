"""Model-written messages, tool-driven self-correction, second opinion on payments, and the visible trace."""
import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from acme_ap.agents import comms as comms_mod
from acme_ap.agents.comms import Comms, grounding_problem, numbers
from acme_ap.approvals import ApprovalService
from acme_ap.erp import MockNetSuite
from acme_ap.llm import LLM, Meter, make_llm
from acme_ap.models import Approval, Decision, Finding, Invoice, LineItem, Outcome, Severity
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
SELF = ROOT / "data" / "self_correction"
INVOICES = ROOT / "data" / "invoices"


class Fixed(LLM):
    """A model that answers each kind of request with whatever the test says (by the start of the system prompt)."""
    available = True

    def __init__(self, **answers):
        self.answers, self.asked, self.meter, self.unexpected = answers, [], Meter(), []

    async def chat_json(self, system, user):
        self.asked.append(system[:30])
        for prefix, reply in self.answers.items():
            if system.replace(" ", "_").startswith(prefix):
                return reply(user) if callable(reply) else reply
        self.unexpected.append(system[:60])
        raise ValueError(f"unexpected request: {system[:60]}")             # a surprise inside an optional helper must not fail the invoice


def run(erp, llm, paths, approvals=None):
    return asyncio.run(Pipeline(erp, llm, workers=1, approvals=approvals).run([str(p) for p in paths]))


# ---- grounding ----------------------------------------------------------------------------------------
def test_grounding_check():
    facts = '{"invoice":"INV-20","questions":["Is the total 1,200.00 correct?"]}'
    assert grounding_problem("Is the total 1200.00 right? Re invoice INV-20.", facts, "Is the total 1,200.00 correct?") is None
    assert grounding_problem("1. Is the total 1,200.00 correct?\n2. Anything else?", facts) is None            # list numbering is layout
    assert "not in the record" in grounding_problem("Please send 1,500.00", facts)                               # an invented amount
    assert "missing" in grounding_problem("Is the total right?", facts, "Is the total 1,200.00 correct?")        # a figure was dropped
    assert "link" in grounding_problem("see https://pay.example/now", facts)
    assert numbers("INV-1010 on 2026-02-26, $1,234.50") >= {"1010", "2026", "02", "26", "1234.50"}


# ---- the Comms agent ----------------------------------------------------------------------------------
def rejected_math_outcome():
    inv = Invoice(invoice_number="INV-9", vendor="Widgets Inc.", total=Decimal("1100.00"), line_items=[LineItem(item="WidgetA", quantity=4, unit_price=Decimal(250))])
    findings = [Finding(code="total_math", severity=Severity.ERROR, message="line items sum to 1000.00 but total is 1100.00")]
    return Outcome(source_path="x", invoice=inv, findings=findings, approval=Approval(decision=Decision.REJECT, reasoning="Rejected: math"), sender="ap@vendor.example")


def test_model_wording_is_used_when_it_is_faithful():
    o = rejected_math_outcome()
    ask = comms_mod.followup_context(o)["asks"][0][1]
    body = f"Hi, regarding INV-9: {ask} Thanks, Accounts Payable, Acme Corp."
    text = asyncio.run(Comms(Fixed(**{"You_write_short,_polite_emails": {"body": body}})).followup(o))
    assert text.startswith("To: ap@vendor.example\nSubject: Quick question on invoice INV-9") and "Hi, regarding INV-9" in text


@pytest.mark.parametrize("bad_body,why", [("Hello, we will pay 9,999.00 tomorrow if you confirm your total is correct please.", "invented"),
                                          ("Hello, could you confirm everything on your invoice is correct please? Thanks.", "dropped"),
                                          ("Hello, please confirm at https://pay.example/verify your invoice details. Thanks.", "link")])
def test_unfaithful_model_text_falls_back_to_the_template(bad_body, why):
    llm = Fixed(**{"You_write_short,_polite_emails": {"body": bad_body}})
    text = asyncio.run(Comms(llm).followup(rejected_math_outcome()))
    assert "Thanks for your invoice INV-9" in text and "9,999" not in text and "https" not in text, why     # the plain template, every figure intact


def test_a_suspicious_sender_is_never_contacted_whatever_the_model_says():
    o = rejected_math_outcome()
    o.findings.append(Finding(code="fraud_indicators", severity=Severity.ERROR, message="multiple fraud signals"))
    llm = Fixed(**{"You_write_short,_polite_emails": {"body": "Hello! Please send us your bank details. Thanks a lot."}})
    assert asyncio.run(Comms(llm).followup(o)) is None and llm.asked == []                                      # decided by rules; the model is not even asked


def test_explanations_only_for_outcomes_that_need_a_person():
    llm = Fixed(**{"You_write_a_plain-English_note": {"summary": "Invoice INV-9 was rejected: line items sum to 1000.00 but total is 1100.00. Ask the vendor."}})
    assert "INV-9 was rejected" in asyncio.run(Comms(llm).explain(rejected_math_outcome()))
    approved = rejected_math_outcome()
    approved.approval = Approval(decision=Decision.APPROVE, reasoning="fine, fine, fine, fine")
    llm.asked.clear()
    assert asyncio.run(Comms(llm).explain(approved)) is None and llm.asked == []
    liar = Fixed(**{"You_write_a_plain-English_note": {"summary": "Invoice INV-9 was rejected because the vendor owes us 5,000 dollars."}})
    assert asyncio.run(Comms(liar).explain(rejected_math_outcome())) is None                                    # invented figure: use the recorded reasoning


def test_the_approver_card_carries_the_plain_english_summary(tmp_path):
    db = tmp_path / "c.db"
    erp = MockNetSuite(db)
    svc = ApprovalService(db, erp)
    (o,) = run(erp, make_llm(simulate=True), [ROOT / "data" / "approvals_demo" / "invoice_3001.json"], svc)
    assert o.explanation and "IN SHORT" in o.approval_card and o.explanation in o.approval_card


# ---- tool-driven self-correction ------------------------------------------------------------------------
def test_a_mistyped_item_is_found_by_search_corrected_and_revalidated(tmp_path):
    erp = MockNetSuite(tmp_path / "r.db")
    llm = make_llm(simulate=True)
    (o,) = run(erp, llm, [SELF / "invoice_3201.json"])
    assert o.status in ("PAID", "SCHEDULED") and o.invoice.line_items[0].item == "WidgetB" and o.invoice.line_items[0].note == "as written: Wdgt B"
    assert any(f.code == "item_corrected" and f.severity is Severity.WARN for f in o.findings) and not any(f.code == "unknown_item" for f in o.findings)
    assert llm.meter.tool_calls["resolver"] == 1
    (offline,) = run(MockNetSuite(tmp_path / "o.db"), make_llm(offline=True), [SELF / "invoice_3201.json"])
    assert offline.status == "REJECT"                                                                           # without the agent this invoice is lost


def test_a_similar_looking_but_different_product_is_not_corrected(tmp_path):
    (o,) = run(MockNetSuite(tmp_path / "r.db"), make_llm(simulate=True), [SELF / "invoice_3202.json"])
    assert o.status == "REJECT" and o.invoice.line_items[0].item == "GadgetY"


def test_the_guard_refuses_a_model_that_corrects_to_something_dissimilar_or_nonexistent(tmp_path):
    for correct in ("WidgetA", "GadgetZ9000"):                                                                    # exists but unrelated / does not exist
        llm = Fixed(**{"You_resolve_item_names": {"corrections": [{"written": "SuperGizmo", "correct": correct, "reason": "sounds right"}]},
                       "The_vendor_on_an_invoice": {"match": None, "reason": "none close"},
                       "You_write_a_plain-English_note": {"summary": "Rejected: SuperGizmo is not in the catalogue, nothing to pay yet."},
                       "You_write_short,_polite_emails": {"body": "Hello, which PO number and PO line does SuperGizmo belong to? Thanks, AP."}})
        (o,) = run(MockNetSuite(tmp_path / f"{correct}.db"), llm, [INVOICES / "invoice_1008.txt"])
        assert o.status == "REJECT" and [li.item for li in o.invoice.line_items][0] == "SuperGizmo", correct
        assert not llm.unexpected


def test_a_lookalike_vendor_is_flagged_for_a_person_and_never_changed(tmp_path):
    erp = MockNetSuite(tmp_path / "v.db")
    (o,) = run(erp, make_llm(simulate=True), [SELF / "invoice_3203.json"], ApprovalService(tmp_path / "v.db", erp))
    assert o.invoice.vendor == "Widget Inc" and o.status == "AWAITING" and not o.paid
    hold = next(f for f in o.findings if f.code == "unknown_vendor")
    assert "possible match: approved vendor 'Widgets Inc.'" in hold.message and "confirm by calling a known number" in hold.message


# ---- every payment gets a second opinion ------------------------------------------------------------
def test_every_paid_invoice_was_looked_at_by_the_model(tmp_path):
    llm = make_llm(simulate=True)
    outs = run(MockNetSuite(tmp_path / "p.db"), llm, sorted(INVOICES.iterdir()))
    paid = [o for o in outs if o.status == "PAID"]
    assert len(paid) == 7
    for o in paid:
        reviewer = next(t for t in o.trace if t["agent"] == "Reviewer")
        assert any(e["kind"] == "model" for e in reviewer["events"]), o.invoice.invoice_number                   # a model call happened before the money moved


# ---- the visible trace ----------------------------------------------------------------------------------
def test_the_trace_records_model_calls_tool_calls_and_conclusions(tmp_path):
    (o,) = run(MockNetSuite(tmp_path / "t.db"), make_llm(simulate=True), [INVOICES / "invoice_1001.txt"])
    events = next(t for t in o.trace if t["agent"] == "Reviewer")["events"]
    kinds = [e["kind"] for e in events]
    assert kinds.count("tool") >= 3 and "model" in kinds and "decision" in kinds
    assert {e["name"] for e in events if e["kind"] == "tool"} >= {"lookup_vendor", "vendor_history", "lookup_item"}
    json.dumps(o.trace)                                                                                          # serialisable for --json


def test_the_trace_flag_prints_it(tmp_path):
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "COLUMNS": "220", "XAI_API_KEY_FILE": "/nonexistent"}
    env.pop("XAI_API_KEY", None)
    p = subprocess.run([sys.executable, "main.py", "--trace", "--db", str(tmp_path / "t.db"), "--outbox", str(tmp_path / "out"), "--invoice_path", "data/self_correction"],
                       cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
    out = p.stdout + p.stderr
    assert p.returncode == 0 and "tool search_items" in out and "corrected: 'Wdgt B' -> 'WidgetB'" in out and "suggested: vendor 'Widget Inc'" in out


def test_a_surprise_inside_an_optional_ai_step_never_fails_the_invoice(tmp_path):
    llm = Fixed()                                                       # answers nothing it is asked: every optional step blows up
    (o,) = run(MockNetSuite(tmp_path / "s.db"), llm, [INVOICES / "invoice_1007.csv"])
    assert o.status == "REJECT" and not o.error and "Thanks for your invoice INV-1007" in o.followup   # the rules' answer, and the template follow-up, still arrive
    assert llm.unexpected


def test_the_note_and_the_vendor_email_are_written_at_the_same_time(tmp_path):
    """Both depend only on the decision. With a model that takes 0.3 s per call, writing them one after the other would take 0.6 s; together, about 0.3 s."""
    import time

    class Slow(Fixed):
        async def chat_json(self, system, user):
            await asyncio.sleep(0.3)
            return await super().chat_json(system, user)

    llm = Slow(**{"You_write_a_plain-English_note": {"summary": "Invoice INV-1007 was rejected: its line items do not add up to the total. Ask the vendor for a corrected invoice."},
                  "You_write_short,_polite_emails": {"body": "Hello, thanks for invoice INV-1007. The line items do not add up to the total (subtotal 14750.00 + tax 885.00 + shipping 0 = 15635.00, invoice total is 15525.00); could you send a corrected invoice? Thanks, Accounts Payable"},
                  "The_vendor_on_an_invoice": {"match": None}, "You_resolve_item_names": {"corrections": []}})
    started = time.perf_counter()
    (o,) = run(MockNetSuite(tmp_path / "c.db"), llm, [INVOICES / "invoice_1007.csv"])
    took = time.perf_counter() - started
    assert o.explanation and "To: " in (o.followup or "") and took < 0.55, f"{took:.2f}s: the two writing jobs ran one after the other"
    comms = [t for t in o.trace if t["agent"] == "Comms"]
    assert [t["action"] for t in comms] == ["explained in plain English", "drafted vendor follow-up"]                 # fixed order in the trace
    assert all([e["kind"] for e in t["events"]] == ["wrote"] for t in comms)                                             # each job keeps its own record, and only its own
    assert sum(e["kind"] == "wrote" for t in comms for e in t["events"]) == 2                                           # nothing is counted twice or lost


def test_a_sibling_product_is_never_taken_for_a_typo():
    """Real Grok corrected 'GadgetY' to 'GadgetX' (similarity 0.86). One changed last character or digit is how a different product differs, not a typo."""
    from acme_ap.agents.resolver import is_other_variant
    assert is_other_variant("GadgetY", "GadgetX") and is_other_variant("Part-17", "Part-18") and is_other_variant("WidgetA", "WidgetB")
    for written, correct in (("Wdgt B", "WidgetB"), ("Widgit B", "WidgetB"), ("widgetb", "WidgetB"), ("Widget  B", "WidgetB"), ("GadgetXX", "GadgetX"), ("Gadgte X", "GadgetX")):
        assert not is_other_variant(written, correct), (written, correct)
