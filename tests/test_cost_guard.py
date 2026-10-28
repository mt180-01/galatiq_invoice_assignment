"""Nothing here may ever spend real money by accident."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from acme_ap.llm import GrokClient, LLMUnavailable, as_agent, make_llm
from acme_ap.metrics import llm_usage
from acme_ap.models import Outcome

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def saved_key(tmp_path, monkeypatch):
    f = tmp_path / "key"
    f.write_text("xai-example-not-a-real-key-4444\n")
    monkeypatch.setenv("XAI_API_KEY_FILE", str(f))
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("ACME_USE_GROK", raising=False)
    return f


def test_a_saved_key_is_not_used_unless_you_ask_for_it(saved_key, monkeypatch):
    assert make_llm().label == "local simulation (not Grok)"                       # key exists, nobody asked: free stand-in
    assert make_llm(live=True).label == "Grok"                                      # --grok
    monkeypatch.setenv("ACME_USE_GROK", "1")
    assert make_llm().label == "Grok"                                               # explicit environment opt-in
    assert make_llm(offline=True).label == "none"                                   # --offline always wins
    assert make_llm(live=True, simulate=True).label == "local simulation (not Grok)"


def test_a_live_client_always_has_a_budget(saved_key, monkeypatch):
    monkeypatch.delenv("XAI_BUDGET_USD", raising=False)
    assert make_llm(live=True).budget_usd == 0.25
    monkeypatch.setenv("XAI_BUDGET_USD", "0.5")
    assert make_llm(live=True).budget_usd == 0.5


def priced_reply(ticks):
    return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}],
                                     "usage": {"prompt_tokens": 100, "completion_tokens": 5, "cost_in_usd_ticks": ticks}})


def test_spend_is_the_api_reported_cost_and_the_budget_is_a_hard_stop():
    calls = []
    llm = GrokClient("k", transport=httpx.MockTransport(lambda r: (calls.append(1), priced_reply(1_500_000))[1]), budget_usd=0.0003)   # each call $0.00015
    for _ in range(2):
        assert asyncio.run(llm.chat_json("s", f"u{len(calls)}")) == {"ok": True}
    assert llm.meter.spent_usd == pytest.approx(0.0003) and llm.meter.cost() == pytest.approx(0.0003)
    with pytest.raises(LLMUnavailable, match="budget"):
        asyncio.run(llm.chat_json("s", "one more"))
    assert len(calls) == 2 and not llm.available                                    # the third request never left the machine
    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.chat_json("s", "and another"))
    assert len(calls) == 2


def test_cost_is_summed_per_invoice_and_reported_exactly():
    llm = GrokClient("k", transport=httpx.MockTransport(lambda r: priced_reply(2_000_000)), budget_usd=1)
    from acme_ap.llm import invoice_ledger
    ledger = []
    invoice_ledger.set(ledger)
    with as_agent("reviewer"):
        asyncio.run(llm.chat_json("s", "a"))
        asyncio.run(llm.chat_json("s", "b"))
    out = Outcome(source_path="x", llm={"calls": 2, "cost_ticks": sum(e[4] for e in ledger)})
    assert llm_usage([out])["cost"] == pytest.approx(0.0004)


def test_the_stand_in_never_costs_anything_or_has_a_cost():
    llm = make_llm(simulate=True)
    asyncio.run(llm.chat_json("You extract invoice data. {}", "Invoice text:\nInvoice INV-1\nVendor: Widgets Inc.\nTotal: $5.00\n"))
    assert llm.meter.spent_usd == 0


def test_a_normal_run_with_a_saved_key_stays_free(saved_key, tmp_path):
    env = {**os.environ, "XAI_API_KEY_FILE": str(saved_key), "PYTHONWARNINGS": "ignore", "COLUMNS": "200"}
    env.pop("XAI_API_KEY", None)
    env.pop("ACME_USE_GROK", None)
    p = subprocess.run([sys.executable, "main.py", "--db", str(tmp_path / "f.db"), "--outbox", str(tmp_path / "o"), "--invoice_path", "data/invoices/invoice_1001.txt"],
                       cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
    out = p.stdout + p.stderr
    assert p.returncode == 0 and "local simulation (not Grok)" in out and "cost $0.00" in out and "xai-example" not in out
