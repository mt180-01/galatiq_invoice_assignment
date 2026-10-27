import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from acme_ap.agents import approval
from acme_ap.agents.team import CHARTERS
from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import llm_extract, parse_file
from acme_ap.llm import GrokClient, LLM, LLMCache, LLMUnavailable, Meter, as_agent
from acme_ap.models import Decision, Finding, Invoice, LineItem, Severity
from acme_ap.pipeline import Pipeline
from scripts.measure_llm_cost import run as cost_run

DATA = Path(__file__).resolve().parent.parent / "data" / "invoices"
DEMO = Path(__file__).resolve().parent.parent / "data" / "approvals_demo"


class Counting(LLM):
    """Records every question; answers like a model that agrees with policy."""
    available = True

    def __init__(self, decision="approve", issues=None):
        self.asked, self.decision, self.issues, self.meter = [], decision, issues or [], Meter()

    async def chat_json(self, system, user):
        self.asked.append("extract" if system.startswith("You extract") else "critic" if "auditor" in system else "draft")
        if self.asked[-1] == "extract":
            raise LLMUnavailable("not needed in this test")
        if self.asked[-1] == "critic":
            return {"issues": self.issues}
        return {"decision": self.decision, "reasoning": "Consistent with the recorded findings and policy."}


def clean(total="500", **kw):
    return Invoice(invoice_number="INV-1", vendor="Widgets Inc.", date="2026-02-01", due_date="2026-03-01", total=Decimal(total),
                   line_items=[LineItem(item="WidgetA", quantity=2, unit_price=Decimal(250), amount=Decimal(500))],
                   channel="structured", extractor="json", **kw)


def finding(sev, code="x"):
    return Finding(code=code, severity=sev, message=code)


# ---- Intake: ask only in doubt -------------------------------------------------------------------
def test_confident_regex_reading_costs_no_tokens():
    inv, text = parse_file(str(DATA / "invoice_1001.txt"))
    llm = Counting()
    assert llm_extract.doubt(inv) is None
    out = asyncio.run(llm_extract.extract(text, inv, llm, gate=True))
    assert out is inv and llm.asked == [] and llm.meter.skipped["intake"] == 1


def test_doubtful_reading_is_sent_to_the_model():
    inv, text = parse_file(str(DATA / "invoice_1001.txt"))
    inv.total = None
    assert "missing total" in llm_extract.doubt(inv)
    llm = Counting()
    asyncio.run(llm_extract.extract(text, inv, llm, gate=True))
    assert llm.asked == ["extract"]


@pytest.mark.parametrize("change", [{"parse_warnings": ["unparseable due date"]}, {"invoice_number": None}, {"vendor": None}, {"line_items": []}])
def test_each_kind_of_doubt_is_detected(change):
    inv, _ = parse_file(str(DATA / "invoice_1001.txt"))
    assert llm_extract.doubt(inv.model_copy(update=change)) is not None


def test_compaction_keeps_the_numbers_and_loses_the_padding():
    raw = "INVOICE\n\n\n\nItem          Qty     Price\n" + "-" * 50 + "\nWidgetA        4      $250.00\n" + "=" * 30 + "\nTotal:   $1,000.00\n"
    out = llm_extract.compact(raw)
    assert "WidgetA  4  $250.00" in out and "$1,000.00" in out and "-----" not in out and "\n\n\n" not in out and len(out) < len(raw) * 0.7
    assert llm_extract.compact("x" * 20000).endswith("[truncated]")


# ---- Reviewer: ask only where it can matter --------------------------------------------------------
def decide(inv, findings, llm, gate=True):
    return asyncio.run(approval.decide(inv, findings, llm, gate=gate))


def test_no_tokens_spent_when_policy_has_already_decided():
    for findings, expect in (([finding(Severity.ERROR)], Decision.REJECT), ([finding(Severity.HOLD)], Decision.ESCALATE)):
        llm = Counting(decision="approve")                        # a model that would love to approve
        assert decide(clean(), findings, llm).decision is expect and llm.asked == []


def test_small_clean_structured_invoice_needs_no_second_opinion():
    llm = Counting()
    assert decide(clean("500"), [], llm).decision is Decision.APPROVE and llm.asked == []


@pytest.mark.parametrize("inv,findings", [(clean("500"), [finding(Severity.WARN)]), (clean("6000"), []),
                                          (clean("500").model_copy(update={"channel": "unstructured", "extractor": "heuristic:text"}), [])])
def test_risk_signals_buy_one_draft_and_no_challenger_when_it_agrees(inv, findings):
    llm = Counting(decision="approve")
    assert decide(inv, findings, llm).decision is Decision.APPROVE
    assert llm.asked == ["draft"]                                   # one call, not draft + critic


def test_a_stricter_draft_is_challenged_once_and_policy_still_has_the_last_word():
    llm = Counting(decision="reject", issues=[])
    result = decide(clean("6000"), [], llm)
    assert llm.asked == ["draft", "critic"]                       # it departed from policy, so it was challenged exactly once
    assert result.decision is Decision.REJECT                    # being stricter than policy is allowed
    loose = Counting(decision="approve")
    assert decide(clean("500"), [finding(Severity.ERROR)], loose).decision is Decision.REJECT and loose.asked == []


def test_always_policy_still_asks_everything():
    llm = Counting()
    decide(clean("500"), [], llm, gate=False)
    assert llm.asked[:2] == ["draft", "critic"]


# ---- the client: usage, cache, model per agent, reply caps -----------------------------------------
def grok(handler, **kw):
    return GrokClient("k", transport=httpx.MockTransport(handler), **kw)


def reply(content='{"ok": true}', usage=True):
    body = {"choices": [{"message": {"content": content}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 100, "completion_tokens": 7}
    return httpx.Response(200, json=body)


def test_usage_is_metered_per_agent_and_estimated_when_missing():
    llm = grok(lambda r: reply())
    with as_agent("reviewer"):
        asyncio.run(llm.chat_json("s", "u"))
    assert llm.meter.calls["reviewer"] == 1 and llm.meter.tokens_in["reviewer"] == 100 and llm.meter.tokens_out["reviewer"] == 7
    est = grok(lambda r: reply(usage=False))
    asyncio.run(est.chat_json("system" * 10, "user" * 10))
    assert est.meter.tokens_in["unassigned"] > 0


def test_identical_requests_are_answered_from_cache(tmp_path):
    calls = []
    llm = grok(lambda r: (calls.append(1), reply())[1], cache=LLMCache(tmp_path / "c.db"))
    assert asyncio.run(llm.chat_json("s", "u")) == asyncio.run(llm.chat_json("s", "u")) == {"ok": True}
    assert len(calls) == 1 and llm.meter.cached["unassigned"] == 1
    asyncio.run(llm.chat_json("s", "different"))
    assert len(calls) == 2


def test_each_agent_can_use_a_cheaper_model_and_has_a_reply_cap(monkeypatch):
    seen = []
    llm = grok(lambda r: (seen.append(json.loads(r.content)), reply())[1], )
    monkeypatch.setenv("XAI_MODEL_INTAKE", "cheap-model")
    for agent in ("intake", "reviewer"):
        with as_agent(agent):
            asyncio.run(llm.chat_json("s", agent))
    assert [(s["model"], s["max_tokens"]) for s in seen] == [("cheap-model", 900), ("grok-3", 160)]


def test_cost_is_reported_only_with_prices(monkeypatch):
    m = Meter(price_in=3.0, price_out=15.0)
    m.record("intake", 1_000_000, 100_000)
    assert m.cost() == pytest.approx(4.5)
    monkeypatch.delenv("XAI_PRICE_IN", raising=False)
    monkeypatch.delenv("XAI_PRICE_OUT", raising=False)
    assert Meter().cost() is None                                # no tariff configured: tokens only, never a guessed price


# ---- the team ---------------------------------------------------------------------------------------
def test_every_agent_has_a_charter_and_only_two_may_use_the_model():
    assert [c.name for c in CHARTERS] == ["Intake", "Validation", "Reviewer", "Desk", "Payment", "Comms"]
    assert [c.name for c in CHARTERS if not c.model_use.startswith("Never")] == ["Intake", "Reviewer"]


def test_trace_shows_which_agent_did_what(tmp_path):
    erp = MockNetSuite(tmp_path / "t.db")
    outs = {Path(o.source_path).name: o for o in asyncio.run(Pipeline(erp, Counting(), workers=1).run(
        [str(DATA / "invoice_1001.txt"), str(DATA / "invoice_1002.txt"), str(DEMO / "invoice_3001.json")]))}
    assert [t["agent"] for t in outs["invoice_1001.txt"].trace] == ["Intake", "Validation", "Reviewer", "Payment"]
    assert [t["agent"] for t in outs["invoice_1002.txt"].trace] == ["Intake", "Validation", "Reviewer"]      # rejected: no money agent
    assert all(t["ms"] >= 0 for o in outs.values() for t in o.trace)


def test_gating_cuts_calls_and_tokens_without_changing_a_single_outcome():
    always, a_out = cost_run("always", False)
    gated, g_out = cost_run("gated", True)
    assert sorted(a_out.values()) == sorted(g_out.values())
    assert gated.total_calls <= always.total_calls * 0.25
    tokens = lambda m: sum(m.tokens_in.values()) + sum(m.tokens_out.values())  # noqa: E731
    assert tokens(gated) <= tokens(always) * 0.25
