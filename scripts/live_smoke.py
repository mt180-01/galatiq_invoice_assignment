"""Does the real Grok API behave the way the client assumes? Five small calls, PASS/FAIL for each.

    python scripts/live_smoke.py

The key comes from XAI_API_KEY or the private file ~/.config/acme_ap/xai_key (see acme_ap/llm.py). It is never printed: error text is
scrubbed of it before display. Costs a few thousand tokens.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acme_ap.agents import approval  # noqa: E402
from acme_ap.ingestion import heuristic, llm_extract, parse_file  # noqa: E402
from acme_ap.llm import GrokClient, LLMUnavailable, Tool, as_agent, read_api_key, structured  # noqa: E402

KEY = read_api_key()
MODEL = os.environ.get("XAI_MODEL", "grok-3")


def scrub(text: object) -> str:
    return str(text).replace(KEY or "\0", "***")[:300]


async def main() -> int:
    if not KEY:
        print("No key: set XAI_API_KEY or put it in ~/.config/acme_ap/xai_key")
        return 2
    llm = GrokClient(KEY, model=MODEL)
    results: list[tuple[str, bool, str]] = []

    async def check(name, coro):
        try:
            ok, note = await coro
        except (LLMUnavailable, Exception) as exc:  # noqa: BLE001
            ok, note = False, scrub(exc)
        results.append((name, ok, note))
        print(("PASS " if ok else "FAIL ") + name + (f"   [{note}]" if note else ""))

    async def auth():
        with as_agent("reviewer"):
            reply = await llm.chat_json("Reply with the JSON object {\"ok\": true}.", "ping")
        return reply.get("ok") is True, f"model {MODEL} answered; JSON mode {'accepted' if GrokClient._json_mode else 'rejected, client dropped it'}"

    async def tools():
        calls = []
        tool = Tool("get_stock", "Stock on hand for an item.", {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]},
                    lambda item: calls.append(item) or {"item": item, "stock": 5})
        with as_agent("reviewer"):
            reply = await llm.run_tools("You answer using the tools.", 'How many GadgetX are in stock? Use the tool, then reply as JSON {"stock": <number>}.', [tool])
        return bool(calls) and int(reply.get("stock", -1)) == 5, f"tool called with {calls}, model answered {reply}"

    async def schema():
        with as_agent("reviewer"):
            r = await structured(llm, "You approve invoices. Reply as JSON {\"decision\": \"approve|reject|escalate\", \"reasoning\": \"...\"}.",
                                 "A clean $500 invoice from an approved vendor, all items in stock. Decide.", approval.ReviewDecision)
        return r.decision in ("approve", "reject", "escalate"), f"decision={r.decision}"

    async def extraction():
        inv, text = parse_file(str(ROOT / "data/invoices/invoice_1010.txt"))      # the typo-heavy one
        with as_agent("intake"):
            got = await llm_extract.extract(text, inv, llm, gate=False)
        warn = [w for w in got.parse_warnings if w.startswith("extractor disagreement")]
        return got.extractor.startswith("grok") and not warn and got.total == inv.total, f"total {got.total}, items {len(got.line_items)}, disagreements: {warn or 'none'}"

    async def reviewer_end_to_end():
        from acme_ap.erp import MockNetSuite
        import tempfile
        from acme_ap.agents.tools import reviewer_tools
        from acme_ap.models import Finding, Severity
        erp = MockNetSuite(Path(tempfile.mkdtemp()) / "s.db")
        inv, _ = parse_file(str(ROOT / "data/invoices/invoice_1001.txt"))
        warn = [Finding(code="price_variance", severity=Severity.WARN, message="unit price differs from list")]
        out = await approval.decide(inv, warn, llm, gate=True, tools=reviewer_tools(erp))
        return out.decision.value in ("approve", "escalate", "reject"), f"{out.decision.value} after {out.rounds} round(s); model made {llm.meter.tool_calls['reviewer']} tool call(s)"

    await check("1. authentication, model name and JSON mode", auth())
    await check("2. function calling (model calls our tool, uses the result)", tools())
    await check("3. structured reply fits the schema", schema())
    await check("4. invoice extraction agrees with the regex reading", extraction())
    await check("5. reviewer agent end to end with tools", reviewer_end_to_end())

    m = llm.meter
    print(f"\nUsed {sum(m.calls.values())} calls, {sum(m.tokens_in.values()):,} tokens in / {sum(m.tokens_out.values()):,} out, {sum(m.tool_calls.values())} tool calls")
    if not results[0][1]:
        try:
            async with httpx.AsyncClient(base_url="https://api.x.ai/v1", headers={"Authorization": f"Bearer {KEY}"}, timeout=15) as c:
                r = await c.get("/models")
                ids = [m_["id"] for m_ in r.json().get("data", [])][:12] if r.status_code == 200 else []
                print(f"Models this key can see: {ids}" if ids else f"/models returned HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print("Could not list models:", scrub(exc))
    await llm.aclose()
    failed = [n for n, ok, _ in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
