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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

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


@dataclass(frozen=True)
class Tool:
    """A function the model may ask us to run (OpenAI-style function calling, which xAI's API follows).
    `fn` runs locally and must be read-only; its JSON-serialisable result goes back to the model."""
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def run_tool(tools: dict[str, Tool], name: str, raw_args: str | dict) -> dict:
    """Execute one model-requested call. Never raises: a bad call becomes an error message the model can correct itself from."""
    tool = tools.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}; available: {sorted(tools)}"}
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        return {"result": tool.fn(**args)}
    except Exception as exc:  # noqa: BLE001 - the model gets the message, the run continues
        return {"error": f"{type(exc).__name__}: {exc}"}


M = TypeVar("M", bound=BaseModel)


class Meter:
    """What the model cost. Prices are USD per million tokens from XAI_PRICE_IN / XAI_PRICE_OUT; with none set, only
    tokens are reported (this repo does not guess a tariff)."""

    def __init__(self, price_in: float | None = None, price_out: float | None = None):
        self.price_in = price_in if price_in is not None else _env_float("XAI_PRICE_IN")
        self.price_out = price_out if price_out is not None else _env_float("XAI_PRICE_OUT")
        self.calls, self.cached, self.skipped, self.tool_calls = Counter(), Counter(), Counter(), Counter()
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

    def tool(self, agent: str, name: str) -> None:
        self.tool_calls[agent] += 1
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("tool", agent, 0, 0))

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

    label = "none"

    async def chat_json(self, system: str, user: str) -> dict:  # pragma: no cover - interface
        raise LLMUnavailable("no LLM configured")

    async def run_tools(self, system: str, user: str, tools: list[Tool], max_steps: int = 4) -> dict:
        """Let the model call `tools` until it answers. Models without tool support just answer."""
        return await self.chat_json(system, user)


class OfflineLLM(LLM):
    pass


class GrokClient(LLM):
    available = True

    def __init__(self, api_key: str, model: str = "grok-3", base_url: str = "https://api.x.ai/v1",
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0,
                 cache: LLMCache | None = None, meter: Meter | None = None):
        self.model, self.cache, self.meter = model, cache, meter or Meter()
        self._key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def model_for(self, agent: str) -> str:
        return os.environ.get(f"XAI_MODEL_{agent.upper()}") or self.model

    label = "Grok"
    _json_mode = True  # ask the API for a JSON object; switched off for good if the endpoint rejects the parameter

    async def _post(self, payload: dict, agent: str) -> dict:
        """One API call with usage metering. Raises LLMUnavailable on any transport or protocol failure."""
        if not self.available:
            raise LLMUnavailable("Grok disabled for this run: the API key was rejected")
        try:
            resp = await self._client.post("/chat/completions", json=payload)
            if resp.status_code in (401, 403) or (resp.status_code == 400 and "api key" in resp.text.lower()):
                self.available = False  # stop asking: every later call would fail the same way and every invoice would pay for the attempt
                raise LLMUnavailable(f"Grok rejected the API key (HTTP {resp.status_code}: {resp.text[:160].replace(self._key, '***')}); "
                                     "falling back to the rules for the rest of this run")
            if resp.status_code in (400, 422) and "response_format" in payload and "response_format" in resp.text:
                type(self)._json_mode = False  # this endpoint does not take it: carry on without
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            body = resp.json()
            body["choices"][0]["message"]
        except LLMUnavailable:
            raise
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:160].replace(self._key, "***")
            raise LLMUnavailable(f"Grok call failed: HTTP {exc.response.status_code}: {detail}") from exc
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMUnavailable(f"Grok call failed: {str(exc).replace(self._key, '***')}") from exc
        usage = body.get("usage") or {}
        sent = sum(len(json.dumps(m)) for m in payload["messages"])
        got = len(json.dumps(body["choices"][0]["message"]))
        self.meter.record(agent, int(usage.get("prompt_tokens") or sent // 4), int(usage.get("completion_tokens") or got // 4))
        return body

    @staticmethod
    def _json_from(content: str) -> dict:
        match = re.search(r"\{.*\}", content or "", re.S)
        if not match:
            raise LLMUnavailable("Grok returned no JSON object")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"Grok returned invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailable("Grok returned JSON that is not an object")
        return data

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
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._json_from((await self._post(payload, agent))["choices"][0]["message"].get("content"))
        if self.cache:
            self.cache.put(key, data)
        return data

    async def run_tools(self, system: str, user: str, tools: list[Tool], max_steps: int = 4) -> dict:
        """Function-calling loop: the model asks for tools, we run them, it sees the results, and finally answers in JSON."""
        agent, by_name = _agent.get(), {t.name: t for t in tools}
        messages: list[dict] = [{"role": "system", "content": system + "\nWhen you have what you need, answer with a single JSON object only."},
                                {"role": "user", "content": user}]
        for step in range(max_steps + 1):
            payload = {"model": self.model_for(agent), "temperature": 0, "max_tokens": MAX_OUT.get(agent, 600), "messages": messages,
                       "tools": [t.spec() for t in tools], "tool_choice": "auto" if step < max_steps else "none"}
            message = (await self._post(payload, agent))["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                return self._json_from(message.get("content"))
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            for call in calls:
                fn = call.get("function") or {}
                self.meter.tool(agent, fn.get("name", "?"))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": json.dumps(run_tool(by_name, fn.get("name", ""), fn.get("arguments", "")), default=str)[:2000]})
        raise LLMUnavailable("tool loop did not reach an answer")

    async def aclose(self) -> None:
        await self._client.aclose()


def read_api_key() -> str | None:
    """XAI_API_KEY from the environment, else from a private file outside the repo (XAI_API_KEY_FILE, default ~/.config/acme_ap/xai_key).
    The key is never written into the project, logs, caches or outputs."""
    if key := os.environ.get("XAI_API_KEY"):
        return key.strip()
    path = Path(os.environ.get("XAI_API_KEY_FILE") or Path.home() / ".config" / "acme_ap" / "xai_key")
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def make_llm(offline: bool = False, cache_path: str | Path | None = None, simulate: bool = False) -> LLM:
    """offline: no model at all (rules only). XAI_API_KEY set: Grok. Otherwise (or simulate=True): the local stand-in, so the full
    agent protocol still runs with no internet."""
    key = read_api_key()
    if offline:
        return OfflineLLM()
    if key and not simulate:
        return GrokClient(key, model=os.environ.get("XAI_MODEL", "grok-3"), cache=LLMCache(cache_path) if cache_path else None)
    from . import sim_llm
    client = GrokClient("local-simulation", transport=sim_llm.transport())
    client.label = sim_llm.LABEL
    return client


async def structured(llm: LLM, system: str, user: str, schema: type[M], tools: list[Tool] | None = None, repairs: int = 1) -> M:
    """Ask for an answer that must fit `schema`. If it does not, tell the model exactly what is wrong and ask again (self-correction),
    then give up with LLMUnavailable so the caller falls back to the deterministic path."""
    prompt, last = user, None
    for _ in range(repairs + 1):
        data = await (llm.run_tools(system, prompt, tools) if tools else llm.chat_json(system, prompt))
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            last = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in exc.errors()[:4])
            prompt = f"{user}\nYour previous reply did not match the required schema ({last}). Reply again with a valid JSON object."
    raise LLMUnavailable(f"reply did not match the schema after {repairs + 1} attempts: {last}")
