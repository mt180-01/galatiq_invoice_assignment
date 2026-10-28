"""Record real Grok conversations once, replay them forever, offline and free.

  --record-llm FILE   run against the live API as usual and save every request/response pair (never headers, so never the key)
  --replay-llm FILE   answer the same requests from the file: no network, no key, no cost; the cost shown is what xAI reported when it was recorded

A request is matched by a hash of its full body (model, messages, tools, response format). The agents' prompts are deterministic (temperature 0, no
dates or randomness in them), so the same invoices through the same code ask the same questions. If prompts are changed on purpose, a replay finds no
recording for the new questions: those steps fall back to the rules, the miss is counted and shown, and the cure is to record again.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import httpx


def request_key(body: dict) -> str:
    """The model name is left out: a recording made under one name replays for the same questions asked under another."""
    body = {k: v for k, v in body.items() if k != "model"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps the real HTTP transport and appends every chat-completion exchange to a JSON-lines file, failures included: a 429 that was retried or a
    400 that made the client step down its response format is part of what happened, and a faithful replay must reproduce it."""

    def __init__(self, path: str | Path, inner: httpx.AsyncBaseTransport | None = None, note: str = ""):
        self.path, self.inner, self._lock = Path(path), inner or httpx.AsyncHTTPTransport(), threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"cassette": 1, "note": note}) + "\n")
        self.recorded = 0
        self.failures = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            response = await self.inner.handle_async_request(request)
        except httpx.TransportError as exc:  # a timeout or dropped connection: no response exists, but it happened, and the client reacted to it
            if request.method == "POST" and request.url.path.endswith("/chat/completions"):
                with self._lock:
                    with self.path.open("a") as f:
                        f.write(json.dumps({"key": request_key(json.loads(request.content)), "status": 0, "error": type(exc).__name__}) + "\n")
                    self.recorded += 1
                    self.failures += 1
            raise
        content = await response.aread()
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            try:
                body = json.loads(content)
            except json.JSONDecodeError:
                body = {"error": content.decode("utf-8", "replace")[:300]}
            entry = {"key": request_key(json.loads(request.content)), "status": response.status_code, "body": body}
            with self._lock:
                with self.path.open("a") as f:
                    f.write(json.dumps(entry) + "\n")
                self.recorded += 1
                self.failures += response.status_code != 200
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
        return httpx.Response(response.status_code, headers=headers, content=content, request=request)


ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ("docs/live_runs/provided-invoices.cassette.jsonl", "docs/live_runs/red-team.cassette.jsonl", "data/extraction_eval/extraction.cassette.jsonl", "docs/live_runs/self-correction.cassette.jsonl")


def bundled_recordings() -> list[Path]:
    """The recordings of real Grok runs that ship with the repository: the provided invoices, the red-team documents and the extraction corpus."""
    return [ROOT / p for p in BUNDLED if (ROOT / p).exists()]


class ReplayTransport(httpx.AsyncBaseTransport):
    """Serves recorded answers. A question that was never recorded gets a 404 that the client turns into 'model unavailable' (the rules take over)."""

    def __init__(self, path: str | Path | list[str | Path]):
        """One recording, or several merged (a question recorded in more than one is answered by the first)."""
        paths = [Path(p) for p in (path if isinstance(path, (list, tuple)) else [path])]
        self.path, self.answers, self.hits, self.misses = paths[0], {}, 0, 0
        seen_keys: set[str] = set()
        for p in paths:
            fresh: dict[str, list] = {}
            for line in p.read_text().splitlines()[1:]:
                entry = json.loads(line)
                if entry["key"] not in seen_keys:
                    fresh.setdefault(entry["key"], []).append((entry.get("status", 200), entry.get("body") or entry.get("error")))
            self.answers.update(fresh)
            seen_keys |= set(fresh)
        self._seen: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            key = request_key(json.loads(request.content)) if request.content else ""
        except json.JSONDecodeError:
            key = ""
        answers = self.answers.get(key)
        if not answers:
            self.misses += 1
            return httpx.Response(404, json={"error": "cassette miss: this request was not recorded"}, request=request)
        i = self._seen.get(key, 0)
        self._seen[key] = i + 1
        self.hits += 1
        status, body = answers[min(i, len(answers) - 1)]
        if status == 0:  # recorded as a transport failure: raise what the live run raised
            raise getattr(httpx, body, httpx.ReadTimeout)("replayed from the recording", request=request)
        return httpx.Response(status, json=body, request=request)
