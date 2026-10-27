"""LLM access. Grok (xAI, OpenAI-compatible API) when XAI_API_KEY is set; otherwise agents run
their deterministic paths so the whole system works offline."""
from __future__ import annotations

import json
import os
import re

import httpx


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    available = False

    async def chat_json(self, system: str, user: str) -> dict:  # pragma: no cover - interface
        raise LLMUnavailable("no LLM configured")


class OfflineLLM(LLM):
    pass


class GrokClient(LLM):
    available = True

    def __init__(self, api_key: str, model: str = "grok-3", base_url: str = "https://api.x.ai/v1",
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0):
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def chat_json(self, system: str, user: str) -> dict:
        payload = {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "system", "content": system + "\nRespond with a single JSON object only."},
                         {"role": "user", "content": user}],
        }
        try:
            resp = await self._client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise LLMUnavailable(f"Grok call failed: {exc}") from exc
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise LLMUnavailable("Grok returned no JSON object")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"Grok returned invalid JSON: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def make_llm(offline: bool = False) -> LLM:
    key = os.environ.get("XAI_API_KEY")
    if offline or not key:
        return OfflineLLM()
    return GrokClient(key, model=os.environ.get("XAI_MODEL", "grok-3"))
