"""LLM access. Grok (xAI, OpenAI-compatible API) when XAI_API_KEY is set; otherwise agents run
their deterministic paths so the whole system works offline.

Cost controls live here, so every agent gets them without knowing about them:
  * a `Meter` counts calls, tokens and (if prices are configured) money per agent, plus the calls agents chose not to make;
  * identical requests are answered from a local cache instead of the API;
  * each agent can use its own model (XAI_MODEL_<AGENT>) and has a cap on reply length.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

import httpx

_agent = contextvars.ContextVar("agent", default="unassigned")
invoice_ledger: contextvars.ContextVar[list | None] = contextvars.ContextVar("invoice_ledger", default=None)  # per-invoice usage sink
MAX_OUT = {"intake": 900, "reviewer": 160, "critic": 120}  # reply-length caps (tokens) by agent


@contextmanager
def as_agent(name: str):
    """Everything the model is asked inside this block is billed to `name`."""
    token = _agent.set(name)
    try:
        yield
    finally:
        _agent.reset(token)


class LLMUnavailable(RuntimeError):
    pass


class Meter:
    """What the model cost. Prices are USD per million tokens from XAI_PRICE_IN / XAI_PRICE_OUT; with none set, only
    tokens are reported (this repo does not guess a tariff)."""

    def __init__(self, price_in: float | None = None, price_out: float | None = None):
        self.price_in = price_in if price_in is not None else _env_float("XAI_PRICE_IN")
        self.price_out = price_out if price_out is not None else _env_float("XAI_PRICE_OUT")
        self.calls, self.cached, self.skipped = Counter(), Counter(), Counter()
        self.tokens_in, self.tokens_out = Counter(), Counter()

    def record(self, agent: str, tin: int, tout: int, cached: bool = False) -> None:
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("cached" if cached else "call", agent, tin, tout))
        if cached:
            self.cached[agent] += 1
            return
        self.calls[agent] += 1
        self.tokens_in[agent] += tin
        self.tokens_out[agent] += tout

    def skip(self, agent: str, n: int = 1) -> None:
        """A call this agent could have made and chose not to (the gate found it could not change the result)."""
        self.skipped[agent] += n
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("skip", agent, 0, 0))

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def cost(self) -> float | None:
        if self.price_in is None or self.price_out is None:
            return None
        return (sum(self.tokens_in.values()) * self.price_in + sum(self.tokens_out.values()) * self.price_out) / 1_000_000


def _env_float(name: str) -> float | None:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


class LLMCache:
    """Same request, same answer, no second charge. Stores invoice text on disk: keep the file private."""

    def __init__(self, path: str | Path):
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, response TEXT)")
            self._db.commit()

    def get(self, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT response FROM llm_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, response: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?)", (key, json.dumps(response)))
            self._db.commit()


class LLM:
    available = False
    meter: Meter | None = None

    async def chat_json(self, system: str, user: str) -> dict:  # pragma: no cover - interface
        raise LLMUnavailable("no LLM configured")


class OfflineLLM(LLM):
    pass


class GrokClient(LLM):
    available = True

    def __init__(self, api_key: str, model: str = "grok-3", base_url: str = "https://api.x.ai/v1",
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0,
                 cache: LLMCache | None = None, meter: Meter | None = None):
        self.model, self.cache, self.meter = model, cache, meter or Meter()
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def model_for(self, agent: str) -> str:
        return os.environ.get(f"XAI_MODEL_{agent.upper()}") or self.model

    async def chat_json(self, system: str, user: str) -> dict:
        agent = _agent.get()
        model = self.model_for(agent)
        system = system + "\nRespond with a single JSON object only."
        key = hashlib.sha256(f"{model}\x00{system}\x00{user}".encode()).hexdigest()
        if self.cache and (hit := self.cache.get(key)) is not None:
            self.meter.record(agent, 0, 0, cached=True)
            return hit
        payload = {"model": model, "temperature": 0, "max_tokens": MAX_OUT.get(agent, 600),
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMUnavailable(f"Grok call failed: {exc}") from exc
        usage = body.get("usage") or {}
        self.meter.record(agent, int(usage.get("prompt_tokens") or (len(system) + len(user)) // 4),
                          int(usage.get("completion_tokens") or len(content) // 4))
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise LLMUnavailable("Grok returned no JSON object")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"Grok returned invalid JSON: {exc}") from exc
        if self.cache:
            self.cache.put(key, data)
        return data

    async def aclose(self) -> None:
        await self._client.aclose()


def make_llm(offline: bool = False, cache_path: str | Path | None = None) -> LLM:
    key = os.environ.get("XAI_API_KEY")
    if offline or not key:
        return OfflineLLM()
    return GrokClient(key, model=os.environ.get("XAI_MODEL", "grok-3"), cache=LLMCache(cache_path) if cache_path else None)
