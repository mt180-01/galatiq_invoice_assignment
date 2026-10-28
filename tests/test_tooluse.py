"""Function calling, structured replies, self-correction, and the local stand-in model, end to end."""
import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from acme_ap.agents import approval
from acme_ap.agents.tools import reviewer_tools
from acme_ap.erp import MockNetSuite
from acme_ap.llm import GrokClient, LLM, LLMUnavailable, Meter, Tool, as_agent, make_llm, run_tool, structured
from acme_ap.models import Decision, Finding, Invoice, LineItem, Severity
from acme_ap.pipeline import Pipeline
from tests.test_pipeline import EXPECTED, by_number

DATA = Path(__file__).resolve().parent.parent / "data" / "invoices"


def message(content=None, tool_calls=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": m}], "usage": {"prompt_tokens": 50, "completion_tokens": 5}})


def call(name, args, id_="c1"):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args) if not isinstance(args, str) else args}}


def client(replies, seen=None):
    it = iter(replies)

    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return next(it)
    return GrokClient("k", transport=httpx.MockTransport(handler))


ECHO = Tool("echo", "echo back", {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}, lambda x: {"you_said": x})


# ---- running a requested tool is safe ---------------------------------------------------------------
def test_tool_runner_never_raises_and_explains_mistakes():
    tools = {"echo": ECHO}
    assert run_tool(tools, "echo", '{"x": "hi"}') == {"result": {"you_said": "hi"}}
    assert "unknown tool 'rm'" in run_tool(tools, "rm", "{}")["error"] and "echo" in run_tool(tools, "rm", "{}")["error"]
    assert "JSONDecodeError" in run_tool(tools, "echo", "{not json")["error"]
    assert "JSON object" in run_tool(tools, "echo", "[1, 2]")["error"]
    assert "TypeError" in run_tool(tools, "echo", '{"y": 1}')["error"]                      # wrong argument names
    boom = {"b": Tool("b", "", {"type": "object"}, lambda: 1 / 0)}
    assert "ZeroDivisionError" in run_tool(boom, "b", "")["error"]


# ---- the function-calling loop ---------------------------------------------------------------------
def test_model_calls_a_tool_sees_the_result_then_answers():
    seen = []
    llm = client([message(tool_calls=[call("echo", {"x": "ping"})]), message('{"answer": "done"}')], seen)
    with as_agent("reviewer"):
        assert asyncio.run(llm.run_tools("sys", "user", [ECHO])) == {"answer": "done"}
    assert seen[0]["tools"][0]["function"]["name"] == "echo" and seen[0]["tool_choice"] == "auto"
    follow_up = seen[1]["messages"]
    assert follow_up[-2]["tool_calls"][0]["id"] == "c1"                                    # the assistant's request is replayed
    assert follow_up[-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"result": {"you_said": "ping"}}'}
    assert llm.meter.tool_calls["reviewer"] == 1 and llm.meter.calls["reviewer"] == 2


def test_a_wrong_tool_call_is_reported_back_and_the_model_recovers():
    seen = []
    llm = client([message(tool_calls=[call("delete_everything", {})]), message(tool_calls=[call("echo", {"x": "ok"}, "c2")]), message('{"answer": 1}')], seen)
    assert asyncio.run(llm.run_tools("s", "u", [ECHO])) == {"answer": 1}
    assert "unknown tool 'delete_everything'" in seen[1]["messages"][-1]["content"]


def test_a_model_that_never_stops_calling_tools_is_cut_off():
    seen = []
    llm = client([message(tool_calls=[call("echo", {"x": "again"})])] * 10, seen)
    with pytest.raises(LLMUnavailable, match="tool loop"):
        asyncio.run(llm.run_tools("s", "u", [ECHO], max_steps=2))
    assert [r["tool_choice"] for r in seen] == ["auto", "auto", "none"]                     # last turn forces an answer


def test_models_without_tool_support_just_answer():
    class Plain(LLM):
        available = True

        async def chat_json(self, system, user):
            return {"ok": True}
    assert asyncio.run(Plain().run_tools("s", "u", [ECHO])) == {"ok": True}


# ---- structured output + self-correction ---------------------------------------------------------------
def test_malformed_reply_is_sent_back_with_the_reason_and_corrected():
    seen = []
    llm = client([message('{"decision": "maybe", "reasoning": "x"}'), message('{"decision": "Approve", "reasoning": "All checks passed, within limit."}')], seen)
    result = asyncio.run(structured(llm, "s", "u", approval.ReviewDecision))
    assert result.decision == "approve"                                                      # case is normalised
    assert "did not match the required schema" in seen[1]["messages"][1]["content"] and "decision" in seen[1]["messages"][1]["content"]


def test_a_model_that_cannot_produce_valid_output_falls_back_to_policy():
    llm = client([message('{"decision": "yes"}')] * 6)
    inv = Invoice(invoice_number="X", vendor="V", total=Decimal(5000), channel="unstructured", extractor="heuristic:text")
    f = [Finding(code="w", severity=Severity.WARN, message="w")]
    out = asyncio.run(approval.decide(inv, f, llm, gate=True))                                # warning: a second opinion is requested, and it is garbage
    assert out.decision is Decision.APPROVE and "Within auto-approval limit" in out.reasoning


def formats_asked(monkeypatch, reject):
    """Run structured requests against a stub that rejects the listed response formats; return what was asked, in order."""
    monkeypatch.setattr(GrokClient, "_format_mode", "json_schema")
    seen = []

    def handler(request):
        body = json.loads(request.content)
        rf = (body.get("response_format") or {}).get("type", "none")
        seen.append(rf)
        if rf in reject:
            return httpx.Response(400, text=f'{{"error": "unsupported response_format type {rf}"}}')
        return message('{"decision": "approve", "reasoning": "All checks passed within the limit."}')
    llm = GrokClient("k", transport=httpx.MockTransport(handler))
    for _ in range(2):
        assert asyncio.run(structured(llm, "s", "u", approval.ReviewDecision)).decision == "approve"
    return seen


def test_a_structured_reply_asks_for_a_json_schema_first(monkeypatch):
    assert formats_asked(monkeypatch, reject=()) == ["json_schema", "json_schema"]


def test_the_format_steps_down_once_and_stays_down_if_the_endpoint_refuses(monkeypatch):
    assert formats_asked(monkeypatch, reject=("json_schema",)) == ["json_schema", "json_object", "json_object"]
    assert formats_asked(monkeypatch, reject=("json_schema", "json_object")) == ["json_schema", "json_object", "none", "none"]


def test_the_schema_sent_is_the_strict_dialect():
    from acme_ap.llm import strict_schema
    schema = strict_schema(approval.ReviewDecision)
    assert schema["additionalProperties"] is False and schema["required"] == ["decision", "reasoning"]
    assert "minLength" not in json.dumps(schema) and "title" not in json.dumps(schema)           # keywords strict mode rejects
    assert schema["properties"]["decision"]["enum"] == ["approve", "reject", "escalate"]
    from acme_ap.agents.resolver import Corrections
    nested = strict_schema(Corrections)
    assert nested["$defs"]["Correction"]["additionalProperties"] is False


@pytest.mark.parametrize("reply,expected", [
    ('{"a": 1}', {"a": 1}),
    ('Sure! Here you go:\n```json\n{"a": 2}\n```\nHope that helps.', {"a": 2}),
    ('The answer is {"a": 3} and that is final.', {"a": 3}),
    ('first {"a": 4} then {"b": 5}', {"a": 4}),                                                  # a greedy pattern would glue these into invalid JSON
    ('{"a": "has } and { braces", "n": {"x": 1}}', {"a": "has } and { braces", "n": {"x": 1}}),
    ('noise {not json} then {"a": 6}', {"a": 6}),
])
def test_json_is_found_by_parsing_not_by_a_greedy_pattern(reply, expected):
    from acme_ap.llm import parse_json_object
    assert parse_json_object(reply) == expected


@pytest.mark.parametrize("reply", ["", None, "I cannot help with that.", "[1, 2, 3]", "{unterminated"])
def test_no_object_means_unavailable_not_a_crash(reply):
    from acme_ap.llm import parse_json_object
    with pytest.raises(LLMUnavailable):
        parse_json_object(reply)


# ---- the reviewer's tools against the real ERP ---------------------------------------------------------
def test_reviewer_tools_answer_from_the_erp(tmp_path):
    erp = MockNetSuite(tmp_path / "t.db")
    tools = {t.name: t for t in reviewer_tools(erp)}
    assert tools["lookup_item"].fn(item="GadgetX") == {"found": True, "name": "GadgetX", "stock": 5, "unit_price": "750.00", "approximate_match": False}
    assert tools["lookup_item"].fn(item="Nope") == {"found": False}
    assert tools["lookup_vendor"].fn(name="Widgets Inc.")["status"] == "approved"
    assert tools["vendor_history"].fn(vendor="Widgets Inc.") == {"bills": 0}
    assert tools["policy_limits"].fn()["auto_approve_up_to_usd"] == "10000"
    assert all(t.spec()["function"]["parameters"]["type"] == "object" for t in tools.values())


def paid_history(erp, vendor, totals):
    for i, total in enumerate(totals):
        key = f"{''.join(c for c in vendor.lower() if c.isalnum())}|HIST-{i}|"
        erp.claim_bill(key, "x", vendor, Decimal(total))
        erp.settle_bill(key, "paid", "earlier")


def test_vendor_history_summarises_paid_bills(tmp_path):
    erp = MockNetSuite(tmp_path / "t.db")
    paid_history(erp, "Widgets Inc.", ["100", "200", "300"])
    assert erp.vendor_history("Widgets Inc.") == {"bills": 3, "avg_total": "200.00", "max_total": "300"}


# ---- the stand-in model, through the whole pipeline ----------------------------------------------------
def run(erp, llm, names):
    return asyncio.run(Pipeline(erp, llm, workers=1).run([str(DATA / n) if not isinstance(n, Path) else str(n) for n in names]))


def test_stand_in_model_reproduces_every_outcome_and_really_uses_tools(tmp_path):
    llm = make_llm(simulate=True)
    outs = run(MockNetSuite(tmp_path / "s.db"), llm, sorted(p.name for p in DATA.iterdir()))
    assert {k: v.status for k, v in by_number(outs).items()} == EXPECTED
    assert not any(o.error for o in outs)
    assert llm.meter.tool_calls["reviewer"] > 0 and llm.meter.calls["reviewer"] > 0         # it called tools, not just prompts
    assert llm.label == "local simulation (not Grok)"
    assert make_llm(offline=True).label == "none"


def test_an_unusually_large_bill_from_a_known_vendor_gets_a_second_opinion_and_goes_to_a_person(tmp_path):
    erp = MockNetSuite(tmp_path / "h.db")
    paid_history(erp, "Widgets Inc.", ["100", "110", "120"])
    f = tmp_path / "invoice_9100.json"
    f.write_text(json.dumps({"invoice_number": "INV-9100", "vendor": "Widgets Inc.", "date": "2026-02-01", "due_date": "2026-03-01",
                             "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250, "amount": 250}],
                             "subtotal": 250, "tax_amount": 0, "total": 250, "currency": "USD", "payment_terms": "Net 30"}))
    plain = run(MockNetSuite(tmp_path / "p.db"), make_llm(simulate=True), [f])               # no history: small, clean, structured -> paid
    assert plain[0].status == "PAID"
    llm = make_llm(simulate=True)
    (o,) = run(erp, llm, [f])
    assert o.status == "ESCALATE" and "Unusual for this vendor" in o.approval.reasoning and "twice the largest" in o.approval.reasoning
    assert llm.meter.tool_calls["reviewer"] >= 3
    offline = run(MockNetSuite(tmp_path / "o.db"), make_llm(offline=True), [f])
    assert offline[0].status == "PAID"                                                      # the rules alone would have paid it


# ---- found by running against the live API ----------------------------------------------------------------
def test_a_note_in_the_item_name_is_not_a_disagreement():
    """Live Grok returned 'WidgetA (rush order)' where the regex reader reports item WidgetA with a note: same line, same quantity."""
    from acme_ap.ingestion import llm_extract, parse_file
    from tests.test_robustness import Scripted, as_json
    baseline, text = parse_file(str(DATA / "invoice_1010.txt"))
    answer = as_json(baseline)
    answer["line_items"][3]["item"] = "WidgetA (rush order)"
    got = asyncio.run(llm_extract.extract(text, baseline, Scripted(extraction=answer)))
    assert not [w for w in got.parse_warnings if w.startswith("extractor disagreement")]
    assert [(li.item, li.quantity) for li in got.line_items][-1] == ("WidgetA", 4) and got.line_items[-1].note == "rush order"


def test_a_model_the_key_cannot_use_is_replaced_by_one_it_can(caplog):
    seen = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "grok-imagine-image"}, {"id": "grok-4.3"}, {"id": "grok-4.20-0309-reasoning"},
                                                      {"id": "grok-4.20-0309-non-reasoning"}, {"id": "grok-build-0.1"}]})
        body = json.loads(request.content)
        seen.append(body["model"])
        if body["model"] == "grok-3":
            return httpx.Response(404, json={"error": "The model grok-3 does not exist or your team does not have access to it."})
        return message('{"ok": true}')
    llm = GrokClient("k", transport=httpx.MockTransport(handler))
    assert asyncio.run(llm.chat_json("s", "u")) == {"ok": True} and asyncio.run(llm.chat_json("s", "u2")) == {"ok": True}
    assert seen == ["grok-3", "grok-4.20-0309-non-reasoning", "grok-4.20-0309-non-reasoning"]      # asked once, then remembered; cheapest kind chosen
    assert llm.model == "grok-4.20-0309-non-reasoning"


def test_unrelated_errors_do_not_trigger_model_discovery():
    from acme_ap.llm import _model_missing
    assert _model_missing('{"error": "The model foo does not exist"}') and not _model_missing('{"error": "Incorrect API key provided"}')
    assert not _model_missing('{"error": "rate limit exceeded"}')


# ---- patience with failures that clear by themselves (found live: "model is at capacity", HTTP 429) --------
def test_a_busy_api_is_retried_and_the_answer_arrives():
    seen = []
    llm = client([httpx.Response(429, json={"error": "The model is currently at capacity"}), httpx.Response(503, text="try later"), message('{"ok": 1}')], seen)
    assert asyncio.run(llm.chat_json("s", "u")) == {"ok": 1} and len(seen) == 3
    assert llm.meter.calls["unassigned"] == 1                                              # failed attempts are not counted as (or billed as) calls


def test_a_persistently_busy_api_gives_up_after_the_retries_and_keeps_the_model_for_later():
    seen = []
    llm = client([httpx.Response(429, text="at capacity")] * 9, seen)
    with pytest.raises(LLMUnavailable, match="HTTP 429"):
        asyncio.run(llm.chat_json("s", "u"))
    assert len(seen) == 3 and llm.available                                                 # first try + two retries; a busy spell is not a dead key


def test_a_timeout_is_retried_but_a_rejected_key_is_not():
    attempts = []

    def flaky(request):
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectTimeout("slow")
        return message('{"ok": 2}')
    assert asyncio.run(GrokClient("k", transport=httpx.MockTransport(flaky)).chat_json("s", "u")) == {"ok": 2} and len(attempts) == 2
    bad = []
    llm = GrokClient("k", transport=httpx.MockTransport(lambda r: (bad.append(1), httpx.Response(401, text="Incorrect API key provided"))[1]))
    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.chat_json("s", "u"))
    assert len(bad) == 1 and not llm.available                                             # no point retrying a bad key


# ---- optional work must not stall an invoice (found live: a courtesy email held one up for 94 seconds) -----
def test_wording_is_not_retried_and_gets_a_short_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions.get("timeout"))
        return httpx.Response(429, text="at capacity")
    llm = GrokClient("k", transport=httpx.MockTransport(handler), retry_delays=(5.0, 5.0))
    with as_agent("comms"):
        with pytest.raises(LLMUnavailable):
            asyncio.run(llm.chat_json("s", "u"))
    assert len(seen) == 1 and seen[0]["read"] == 12.0                                       # one try, twelve seconds, no waiting for retries
    seen.clear()
    llm2 = GrokClient("k", transport=httpx.MockTransport(handler), retry_delays=(0, 0))
    with as_agent("reviewer"):
        with pytest.raises(LLMUnavailable):
            asyncio.run(llm2.chat_json("s", "u"))
    assert len(seen) == 3                                                                   # decisions that matter still get their retries


# ---- the default model: a plain one when the account has it (found live: "grok-3" was served by a slower, pricier reasoning model) ----------------------
def model_client(ids, explicit=None, seen=None):
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        seen.append(json.loads(request.content)["model"])
        return message('{"ok": true}')
    return GrokClient("k", model=explicit or "grok-3", auto_model=not explicit, transport=httpx.MockTransport(handler))


def test_with_no_model_chosen_a_non_reasoning_one_is_used_when_offered():
    seen = []
    llm = model_client(["grok-4.3", "grok-4.20-0309-reasoning", "grok-4.20-0309-non-reasoning", "grok-imagine-image"], seen=seen)
    for _ in range(3):
        assert asyncio.run(llm.chat_json("s", "u")) == {"ok": True}
    assert seen == ["grok-4.20-0309-non-reasoning"] * 3 and llm.model == "grok-4.20-0309-non-reasoning"


def test_an_explicit_model_is_always_respected_and_never_looked_up():
    seen, looked_up = [], []
    llm = GrokClient("k", model="grok-4.3", auto_model=False, transport=httpx.MockTransport(
        lambda r: (looked_up.append(1), httpx.Response(200, json={"data": []}))[1] if r.url.path.endswith("/models") else (seen.append(json.loads(r.content)["model"]), message('{"ok": 1}'))[1]))
    asyncio.run(llm.chat_json("s", "u"))
    assert seen == ["grok-4.3"] and not looked_up


def test_without_a_non_reasoning_model_the_default_name_stays():
    seen = []
    llm = model_client(["grok-4.3", "grok-4.5"], seen=seen)
    asyncio.run(llm.chat_json("s", "u"))
    assert seen == ["grok-3"]


def test_concurrent_first_calls_all_wait_for_the_model_lookup_and_none_uses_the_old_name():
    """Found live: calls that arrived while the lookup was in flight went out under the old name. The lookup here is slow, as a real one is."""
    lookups, seen = [], []

    class SlowLookup(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/models"):
                lookups.append(1)
                await asyncio.sleep(0.05)
                return httpx.Response(200, json={"data": [{"id": "grok-4.20-0309-non-reasoning"}]}, request=request)
            seen.append(json.loads(request.content)["model"])
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}]}, request=request)
    llm = GrokClient("k", model="grok-3", auto_model=True, transport=SlowLookup())

    async def burst():
        await asyncio.gather(*(llm.chat_json("s", f"u{i}") for i in range(8)))
    asyncio.run(burst())
    assert len(lookups) == 1 and seen == ["grok-4.20-0309-non-reasoning"] * 8


def test_a_recording_replays_whatever_model_name_is_asked():
    from acme_ap.cassette import request_key
    base = {"messages": [{"role": "user", "content": "x"}], "temperature": 0}
    assert request_key({**base, "model": "grok-3"}) == request_key({**base, "model": "grok-4.20-0309-non-reasoning"})
    assert request_key({**base, "model": "m"}) != request_key({**base, "messages": [{"role": "user", "content": "y"}], "model": "m"})
