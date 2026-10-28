"""Record a live run once, replay it offline forever: the evidence is checkable by anyone, for free."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from acme_ap.cassette import RecordingTransport, ReplayTransport
from acme_ap.llm import GrokClient, LLMUnavailable, make_llm

ROOT = Path(__file__).resolve().parent.parent
CASSETTE = ROOT / "docs" / "live_runs" / "provided-invoices.cassette.jsonl"
RECORDED = [json.loads(l) for l in CASSETTE.read_text().splitlines()[1:]]
RECORDED_COST = sum(r["body"]["usage"]["cost_in_usd_ticks"] for r in RECORDED) / 1e10          # what xAI charged when it was recorded
SECRET = "xai-example-not-a-real-key-5555"


def ok(content='{"ok": true}', cost=1_000_000):
    return httpx.Response(200, json={"model": "m", "choices": [{"message": {"role": "assistant", "content": content}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost_in_usd_ticks": cost}})


def record(path, handler, calls, retry_delays=(0, 0)):
    """Run `calls` (async fns taking a client) against a stub 'live' API through the recorder."""
    transport = RecordingTransport(path, inner=httpx.MockTransport(handler))
    client = GrokClient(SECRET, transport=transport, retry_delays=retry_delays)

    async def go():
        return [await c(client) for c in calls]
    return asyncio.run(go()), client, transport


def replay_client(path):
    client = make_llm(replay_from=path)
    return client


def test_a_recorded_exchange_is_replayed_without_a_network_and_with_the_recorded_cost(tmp_path):
    path = tmp_path / "c.jsonl"
    answers, live, rec = record(path, lambda r: ok(), [lambda c: c.chat_json("sys", "one"), lambda c: c.chat_json("sys", "two")])
    assert answers == [{"ok": True}] * 2 and rec.recorded == 2 and live.meter.spent_usd == pytest.approx(0.0002)
    replayed = replay_client(path)
    assert asyncio.run(replayed.chat_json("sys", "one")) == {"ok": True} and asyncio.run(replayed.chat_json("sys", "two")) == {"ok": True}
    assert replayed.replay.hits == 2 and replayed.replay.misses == 0
    assert replayed.meter.spent_usd == pytest.approx(0.0002)                                     # the cost shown is what the live run was charged
    assert replayed.label.startswith("Grok (replayed from c.jsonl")


def test_a_question_that_was_never_recorded_is_a_clean_miss_not_a_guess(tmp_path):
    path = tmp_path / "c.jsonl"
    record(path, lambda r: ok(), [lambda c: c.chat_json("sys", "asked")])
    replayed = replay_client(path)
    with pytest.raises(LLMUnavailable, match="HTTP 404"):
        asyncio.run(replayed.chat_json("sys", "never asked"))
    assert replayed.replay.misses == 1 and replayed.available                                    # that step falls back to the rules; the model is not "down"


def test_failures_are_recorded_and_replayed_so_retries_and_timeouts_happen_again(tmp_path):
    path = tmp_path / "c.jsonl"
    seen = []

    def flaky(request):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(429, json={"error": "at capacity"})
        if len(seen) == 2:
            raise httpx.ReadTimeout("slow", request=request)
        return ok()
    answers, _, rec = record(path, flaky, [lambda c: c.chat_json("sys", "q")])
    assert answers == [{"ok": True}] and rec.recorded == 3 and rec.failures == 2                 # a 429, a timeout, then the answer
    lines = [json.loads(l) for l in path.read_text().splitlines()[1:]]
    assert [l["status"] for l in lines] == [429, 0, 200]
    replayed = replay_client(path)
    assert asyncio.run(replayed.chat_json("sys", "q")) == {"ok": True}                           # the client retried through the same failures
    assert replayed.replay.hits == 3 and replayed.replay.misses == 0


def test_a_recording_never_contains_the_key_or_any_header(tmp_path):
    path = tmp_path / "c.jsonl"
    record(path, lambda r: ok(), [lambda c: c.chat_json("sys", "q")])
    text = path.read_text()
    assert SECRET not in text and "authorization" not in text.lower() and "bearer" not in text.lower()


def test_the_model_name_is_not_part_of_what_is_matched(tmp_path):
    path = tmp_path / "c.jsonl"
    live = GrokClient(SECRET, model="grok-3", transport=RecordingTransport(path, inner=httpx.MockTransport(lambda r: ok())))
    asyncio.run(live.chat_json("sys", "q"))
    other = GrokClient("replay", model="a-different-model", transport=ReplayTransport(path), retry_delays=())
    assert asyncio.run(other.chat_json("sys", "q")) == {"ok": True}


# ---- the committed recording of a real Grok run ---------------------------------------------------------
def replay_cli(tmp_path, *extra):
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "COLUMNS": "200", "XAI_API_KEY_FILE": "/nonexistent"}
    env.pop("XAI_API_KEY", None)
    p = subprocess.run([sys.executable, "main.py", "--replay-llm", str(CASSETTE), "--db", str(tmp_path / "r.db"), "--outbox", str(tmp_path / "o"), "--json", str(tmp_path / "r.json"),
                        "--invoice_path", "data/invoices", *extra], cwd=ROOT, capture_output=True, text=True, env=env, timeout=180)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout + p.stderr


def test_the_committed_live_run_replays_offline_with_no_key_and_no_cost(tmp_path):
    out = replay_cli(tmp_path)
    assert "LLM: Grok (replayed from provided-invoices.cassette.jsonl)" in out
    assert f"replay: {len(RECORDED)} model call(s) answered from the recording, 0 not recorded" in out
    assert f"spent ${RECORDED_COST:.4f} (reported by xAI, when recorded)" in out
    assert "AWAITING=2" in out and "PAID=7" in out and "REJECT=8" in out and "DUPLICATE=3" in out
    res = json.loads((tmp_path / "r.json").read_text())
    assert not any(o.get("error") for o in res)
    assert sum(o["llm"].get("calls", 0) for o in res) == len(RECORDED) and sum(o["llm"].get("tools", 0) for o in res) >= 4


def test_the_replay_reproduces_what_the_live_run_wrote(tmp_path):
    replay_cli(tmp_path)
    got = {o["invoice"]["invoice_number"] + str(o["invoice"].get("revision") or ""): o for o in json.loads((tmp_path / "r.json").read_text()) if o["status"] != "DUPLICATE"}
    live = {o["invoice"]["invoice_number"] + str(o["invoice"].get("revision") or ""): o
            for o in json.loads((ROOT / "docs/live_runs/2026-10-27-provided-invoices.json").read_text()) if o["status"] != "DUPLICATE"}
    assert got.keys() == live.keys() and len(got) == 17
    for k in live:
        assert (got[k]["status"], got[k]["explanation"], got[k]["followup"]) == (live[k]["status"], live[k]["explanation"], live[k]["followup"]), k


def test_the_committed_recording_is_clean_and_complete():
    assert json.loads(CASSETTE.read_text().splitlines()[0])["cassette"] == 1 and len(RECORDED) >= 30
    assert all(r["status"] == 200 for r in RECORDED)                                              # a healthy run: every exchange succeeded
    assert {r["body"]["model"] for r in RECORDED} == {"grok-4.20-0309-non-reasoning"}              # served by the plain model, not a reasoning one
    assert 0.005 < RECORDED_COST < 0.05                                                            # cents, as claimed
    text = CASSETTE.read_text().lower()
    assert "authorization" not in text and "bearer" not in text and "xai-" not in text


def test_replay_and_record_flags_are_listed_in_help():
    p = subprocess.run([sys.executable, "main.py", "--help"], cwd=ROOT, capture_output=True, text=True, env={**os.environ, "COLUMNS": "200"})
    assert "--record-llm" in p.stdout and "--replay-llm" in p.stdout


def test_the_evaluation_document_is_current_and_says_what_the_two_runs_found():
    """docs/EVALUATION.md is generated from a rules-only run and the replayed live run. If prompts change, the replay stops matching and this fails:
    re-record (python main.py --record-llm ...) and regenerate (python scripts/evaluate.py)."""
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "XAI_API_KEY_FILE": "/nonexistent"}
    p = subprocess.run([sys.executable, "scripts/evaluate.py", "--check"], cwd=ROOT, capture_output=True, text=True, env=env, timeout=240)
    assert p.returncode == 0, p.stdout + p.stderr
    text = (ROOT / "docs" / "EVALUATION.md").read_text()
    assert text.count("Matches the brief's stated outcome | 8 of 8") == 2 and text.count("Matches the developer's label | 17 of 17") == 2
    assert text.count("Wrongly paid (paid, but label says it should not be) | 0") == 2 and "Decisions identical on **17 of 17**" in text
    assert "Run 1: rules only" in text and "Run 2: LLM on" in text and f"${RECORDED_COST:.4f}" in text and "not** an independent test" in text
    assert "**NO**" not in text
