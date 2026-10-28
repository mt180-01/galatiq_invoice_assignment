"""LLM access. Grok (xAI, OpenAI-compatible API) when XAI_API_KEY is set; otherwise agents run
their deterministic paths so the whole system works offline.

Cost controls live here, so every agent gets them without knowing about them:
  * a `Meter` counts calls, tokens and (if prices are configured) money per agent, plus the calls agents chose not to make;
  * identical requests are answered from a local cache instead of the API;
  * each agent can use its own model (XAI_MODEL_<AGENT>) and has a cap on reply length.
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
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
AGENT_TIMEOUT = {"comms": 12.0}  # seconds. Wording is optional: a slow reply must never hold up an invoice, so it gets a short leash and no retries
MAX_OUT = {"intake": 900, "reviewer": 160, "critic": 120, "resolver": 200, "comms": 260}  # reply-length caps (tokens) by agent


@contextmanager
def as_agent(name: str):
    """Everything the model is asked inside this block is billed to `name`."""
    token = _agent.set(name)
    try:
        yield
    finally:
        _agent.reset(token)


log_model = logging.getLogger("acme.llm")


def _model_missing(body: str) -> bool:
    text = body.lower()
    return "model" in text and any(w in text for w in ("not found", "does not exist", "not available", "no access", "invalid model", "unknown model"))


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


def parse_json_object(content: str | None) -> dict:
    """The first complete JSON object in a model's reply: the whole text if it is JSON, else a ```fenced``` block, else the first balanced
    {...} found by actually parsing (not a greedy pattern, which swallows everything between the first '{' and the last '}')."""
    text = (content or "").strip()
    candidates = [text, *re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)]
    for cand in candidates:
        try:
            data = json.loads(cand.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                data, _ = decoder.raw_decode(text[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    raise LLMUnavailable("the reply contained no JSON object" if "{" not in text else "the reply contained no valid JSON object")


_UNSUPPORTED = {"title", "default", "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum", "examples", "description"}


def strict_schema(model: type[BaseModel]) -> dict:
    """The model's JSON Schema in the strict dialect structured-output APIs accept: every property required, no extra properties, and none of the
    keywords strict mode rejects. (Pydantic still enforces the full rules, lengths included, on what comes back.)"""
    def clean(node):
        if isinstance(node, dict):
            out = {k: clean(v) for k, v in node.items() if k not in _UNSUPPORTED or k == "properties"}
            if out.get("type") == "object" or "properties" in out:
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}))
            return out
        if isinstance(node, list):
            return [clean(v) for v in node]
        return node
    schema = model.model_json_schema()
    defs = schema.pop("$defs", None)
    schema = clean(schema)
    if defs:
        schema["$defs"] = {k: clean(v) for k, v in defs.items()}
    return schema


_wanted_schema: contextvars.ContextVar[tuple[str, dict] | None] = contextvars.ContextVar("wanted_schema", default=None)


class Meter:
    """What the model cost. Prices are USD per million tokens from XAI_PRICE_IN / XAI_PRICE_OUT; with none set, only
    tokens are reported (this repo does not guess a tariff)."""

    def __init__(self, price_in: float | None = None, price_out: float | None = None):
        self.price_in = price_in if price_in is not None else _env_float("XAI_PRICE_IN")
        self.price_out = price_out if price_out is not None else _env_float("XAI_PRICE_OUT")
        self.calls, self.cached, self.skipped, self.tool_calls = Counter(), Counter(), Counter(), Counter()
        self.tokens_in, self.tokens_out = Counter(), Counter()
        self.reported_ticks = 0  # cost as reported by the API itself (1 USD = 10,000,000,000 ticks), the exact figure

    def record(self, agent: str, tin: int, tout: int, cached: bool = False, ticks: int = 0, model: str = "") -> None:
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("cached" if cached else "call", agent, tin, tout, ticks, {"model": model}))
        if cached:
            self.cached[agent] += 1
            return
        self.calls[agent] += 1
        self.tokens_in[agent] += tin
        self.tokens_out[agent] += tout
        self.reported_ticks += ticks

    def tool(self, agent: str, name: str, args=None, result=None) -> None:
        self.tool_calls[agent] += 1
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("tool", agent, 0, 0, 0, {"name": name, "args": args, "result": str(result)[:160] if result is not None else None}))

    def event(self, agent: str, kind: str, text: str) -> None:
        """Something an agent concluded, for the invoice's visible trace (not a model call)."""
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("event", agent, 0, 0, 0, {"kind": kind, "text": text[:240]}))

    def skip(self, agent: str, n: int = 1) -> None:
        """A call this agent could have made and chose not to (the gate found it could not change the result)."""
        self.skipped[agent] += n
        if (sink := invoice_ledger.get()) is not None:
            sink.append(("skip", agent, 0, 0, 0, {}))

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    @property
    def spent_usd(self) -> float:
        return self.reported_ticks / 10_000_000_000

    def cost(self) -> float | None:
        if self.reported_ticks:
            return self.spent_usd  # the API's own number beats any tariff we could configure
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
                 cache: LLMCache | None = None, meter: Meter | None = None, budget_usd: float | None = None,
                 retry_delays: tuple[float, ...] | None = None, auto_model: bool = False):
        self.model, self.cache, self.meter = model, cache, meter or Meter()
        self.budget_usd = budget_usd  # hard cap on what this client may spend; None = unlimited (only for the key-free stand-in)
        self._key = api_key
        self.auto_model, self._model_lock = auto_model, asyncio.Lock()  # auto: no model was chosen, so pick one this key can use (see _resolve_model)
        self.retry_delays = self.DEFAULT_RETRY if retry_delays is None else retry_delays  # seconds to wait before each retry of a transient failure (429/5xx/timeouts)
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def model_for(self, agent: str) -> str:
        return os.environ.get(f"XAI_MODEL_{agent.upper()}") or self.model

    label = "Grok"
    DEFAULT_RETRY = (1.0, 3.0)
    _model_checked = False
    # How hard to ask for well-formed JSON, strongest first: a JSON Schema ("json_schema"), a JSON object ("json_object"), or just ask nicely
    # ("none"). If the endpoint rejects the parameter we step down one level for good, so a refusal costs one retry, once.
    _format_mode = "json_schema"
    _FORMATS = ("json_schema", "json_object", "none")

    async def _send(self, payload: dict) -> httpx.Response:
        """POST with a couple of patient retries for the failures that clear by themselves (rate limit, 'at capacity', 5xx, timeouts)."""
        agent = _agent.get()
        delays = () if agent in AGENT_TIMEOUT else self.retry_delays  # optional work is not retried
        for attempt in range(len(delays) + 1):
            try:
                resp = await self._client.post("/chat/completions", json=payload, **({"timeout": AGENT_TIMEOUT[agent]} if agent in AGENT_TIMEOUT else {}))
            except httpx.TransportError:
                if attempt == len(delays):
                    raise
            else:
                if resp.status_code not in (429, 500, 502, 503, 504) or attempt == len(delays):
                    return resp
            await asyncio.sleep(delays[attempt])
        raise AssertionError("unreachable")

    async def _post(self, payload: dict, agent: str) -> dict:
        """One API call with usage metering. Raises LLMUnavailable on any transport or protocol failure."""
        if not self.available:
            raise LLMUnavailable("Grok disabled for this run: the API key was rejected or the budget was reached")
        if self.budget_usd is not None and self.meter.spent_usd >= self.budget_usd:
            self.available = False
            log_model.warning("spending cap of $%.2f reached ($%.4f spent): the rest of this run uses the free rules", self.budget_usd, self.meter.spent_usd)
            raise LLMUnavailable(f"budget of ${self.budget_usd:.2f} reached")
        try:
            resp = await self._send(payload)
            if resp.status_code in (401, 403) or (resp.status_code == 400 and "api key" in resp.text.lower()):
                self.available = False  # stop asking: every later call would fail the same way and every invoice would pay for the attempt
                raise LLMUnavailable(f"Grok rejected the API key (HTTP {resp.status_code}: {resp.text[:160].replace(self._key, '***')}); "
                                     "falling back to the rules for the rest of this run")
            if resp.status_code in (400, 403, 404) and not self._model_checked and _model_missing(resp.text):
                self._model_checked = True
                if found := await self._discover_model():
                    log_model.warning("model %s is not available to this key; using %s", payload["model"], found)
                    self.model = found
                    payload = {**payload, "model": found}
                    resp = await self._send(payload)
            if resp.status_code in (400, 422) and "response_format" in payload and any(w in resp.text.lower() for w in ("response_format", "json_schema", "schema", "format")):
                lower = self._FORMATS[min(self._FORMATS.index(type(self)._format_mode) + 1, 2)]
                type(self)._format_mode = lower  # this endpoint does not take the stronger form: step down and carry on
                payload = {k: v for k, v in payload.items() if k != "response_format"}
                if lower == "json_object":
                    payload["response_format"] = {"type": "json_object"}
                resp = await self._send(payload)
                if resp.status_code in (400, 422) and "response_format" in payload:  # not even json_object: ask without
                    type(self)._format_mode = "none"
                    payload = {k: v for k, v in payload.items() if k != "response_format"}
                    resp = await self._send(payload)
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
        self.meter.record(agent, int(usage.get("prompt_tokens") or sent // 4), int(usage.get("completion_tokens") or got // 4),
                          ticks=int(usage.get("cost_in_usd_ticks") or 0), model=str(body.get("model") or payload.get("model") or ""))
        return body

    async def _resolve_model(self) -> None:
        """If the user did not name a model, use a plain (non-reasoning) one when the account offers it. A name like "grok-3" can be served by a larger
        reasoning model that bills hidden thinking tokens and takes several times longer, which routine extraction and review do not need
        (measured on one account: 4.7-5.3 s and ~10,000,000 ticks per note against 1.0-1.9 s and ~3,000,000)."""
        if not self.auto_model:
            return
        async with self._model_lock:  # everyone else waits here until the lookup is finished, so no call goes out under the old name
            if self.auto_model:
                found = await self._discover_model()
                if found and "non-reasoning" in found:
                    self.model = found
                self.auto_model = False  # only now is it settled

    async def _discover_model(self) -> str | None:
        """Ask the API which models this key can use and take the first plain chat model, preferring non-reasoning ones
        (cheaper: reasoning models bill hidden thinking tokens, which routine invoice work does not need)."""
        try:
            resp = await self._client.get("/models")
            ids = [m["id"] for m in resp.json().get("data", [])]
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return None
        chat = [i for i in ids if i.startswith("grok-") and not any(x in i for x in ("imagine", "video", "image", "build", "multi-agent", "vision"))]
        plain = [i for i in chat if "non-reasoning" in i]
        return (plain or chat or [None])[0]

    @staticmethod
    def _json_from(content: str) -> dict:
        return parse_json_object(content)

    async def chat_json(self, system: str, user: str) -> dict:
        agent = _agent.get()
        await self._resolve_model()
        model = self.model_for(agent)
        system = system + "\nRespond with a single JSON object only."
        key = hashlib.sha256(f"{model}\x00{system}\x00{user}".encode()).hexdigest()
        if self.cache and (hit := self.cache.get(key)) is not None:
            self.meter.record(agent, 0, 0, cached=True)
            return hit
        payload = {"model": model, "temperature": 0, "max_tokens": MAX_OUT.get(agent, 600),
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        wanted = _wanted_schema.get()
        if type(self)._format_mode == "json_schema" and wanted:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": wanted[0], "schema": wanted[1], "strict": True}}
        elif type(self)._format_mode != "none":
            payload["response_format"] = {"type": "json_object"}
        data = self._json_from((await self._post(payload, agent))["choices"][0]["message"].get("content"))
        if self.cache:
            self.cache.put(key, data)
        return data

    async def run_tools(self, system: str, user: str, tools: list[Tool], max_steps: int = 4) -> dict:
        """Function-calling loop: the model asks for tools, we run them, it sees the results, and finally answers in JSON."""
        agent, by_name = _agent.get(), {t.name: t for t in tools}
        await self._resolve_model()
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
                outcome = run_tool(by_name, fn.get("name", ""), fn.get("arguments", ""))
                self.meter.tool(agent, fn.get("name", "?"), fn.get("arguments", ""), outcome)
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(outcome, default=str)[:2000]})
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


DEFAULT_BUDGET_USD = 0.25  # per run; XAI_BUDGET_USD overrides. A full pass over the 17 sample invoices costs a few cents.


def make_llm(offline: bool = False, cache_path: str | Path | None = None, simulate: bool = False, live: bool = False,
             record_to: str | Path | None = None, replay_from: str | Path | None = None) -> LLM:
    """Which model answers:
      offline        no model at all (rules only), free
      default        the local stand-in, free
      live=True      Grok, billed per token by xAI, capped by a per-run budget. Never chosen unless asked for (--grok or ACME_USE_GROK=1),
                     so a saved key cannot cost money by accident."""
    if offline:
        return OfflineLLM()
    if replay_from:  # a recorded live run, answered from the file: no network, no key, no cost
        from .cassette import ReplayTransport
        transport = ReplayTransport(replay_from)
        GrokClient._format_mode, GrokClient._model_checked = "json_schema", False  # start from the same state a fresh live run did
        client = GrokClient("replay", transport=transport, retry_delays=(0, 0), budget_usd=None)  # recorded 429s are replayed, so retries follow the same path, instantly
        client.label, client.replay = f"Grok (replayed from {Path(replay_from).name})", transport
        return client
    live = live or bool(record_to) or os.environ.get("ACME_USE_GROK") == "1"
    key = read_api_key() if live else None
    if key and not simulate:
        budget = _env_float("XAI_BUDGET_USD")
        transport = None
        if record_to:  # recording must see every request, so the answer cache is off
            from .cassette import RecordingTransport
            transport = RecordingTransport(record_to, note=f"recorded {__import__('datetime').date.today()} against model {os.environ.get('XAI_MODEL', 'grok-3')}")
        chosen = os.environ.get("XAI_MODEL")
        client = GrokClient(key, model=chosen or "grok-3", auto_model=not chosen, transport=transport, cache=None if record_to else (LLMCache(cache_path) if cache_path else None),
                            budget_usd=DEFAULT_BUDGET_USD if budget is None else budget)
        if record_to:
            client.label, client.recording = "Grok (recording)", transport
        return client
    from . import sim_llm
    client = GrokClient("local-simulation", transport=sim_llm.transport())
    client.label = sim_llm.LABEL
    return client


async def structured(llm: LLM, system: str, user: str, schema: type[M], tools: list[Tool] | None = None, repairs: int = 1) -> M:
    """Ask for an answer that must fit `schema`. If it does not, tell the model exactly what is wrong and ask again (self-correction),
    then give up with LLMUnavailable so the caller falls back to the deterministic path."""
    prompt, last = user, None
    for _ in range(repairs + 1):
        token = _wanted_schema.set((schema.__name__, strict_schema(schema)))  # lets a capable endpoint enforce the shape itself
        try:
            data = await (llm.run_tools(system, prompt, tools) if tools else llm.chat_json(system, prompt))
        finally:
            _wanted_schema.reset(token)
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            last = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in exc.errors()[:4])
            prompt = f"{user}\nYour previous reply did not match the required schema ({last}). Reply again with a valid JSON object."
    raise LLMUnavailable(f"reply did not match the schema after {repairs + 1} attempts: {last}")
