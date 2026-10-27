"""A local stand-in for Grok, so the whole agentic path (prompts, function calling, structured replies, self-correction) runs with no
internet and no key. It speaks the same chat-completions protocol, plugged in as the HTTP transport of the real client, so every byte
of client code is exercised; only the "thinking" is replaced by deterministic rules.

It is NOT a language model and is labelled as such everywhere it shows up. It reads the same facts a real model would be given,
calls the same tools, and answers in the same shape, which makes behaviour reproducible in tests and demos. It cannot show how
well Grok itself reasons.
"""
from __future__ import annotations

import json

import httpx

from .ingestion import heuristic

LABEL = "local simulation (not Grok)"


def _reply(content: str | None = None, tool_calls: list | None = None) -> httpx.Response:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": message}]})  # no `usage`: the client estimates tokens, as it would for any reply without one


def _extraction(user: str) -> httpx.Response:
    text = user.split("Invoice text:\n", 1)[1].split("\n\nYour previous", 1)[0]
    inv = heuristic.extract(text)
    money = lambda v: str(v) if v is not None else None  # noqa: E731
    return _reply(json.dumps({
        "invoice_number": inv.invoice_number, "vendor": inv.vendor, "date": inv.date, "due_date": inv.due_date, "currency": inv.currency,
        "subtotal": money(inv.subtotal), "tax_amount": money(inv.tax_amount), "shipping": money(inv.shipping), "total": money(inv.total),
        "payment_terms": inv.payment_terms, "bank_account": inv.bank_account,
        "line_items": [{"item": li.item, "quantity": li.quantity, "unit_price": money(li.unit_price), "amount": money(li.amount)} for li in inv.line_items]}))


def _brief(user: str) -> dict:
    return json.loads(user.split("\n", 1)[0])


def _critic(user: str) -> httpx.Response:
    findings = _brief(user)["findings"]
    under_review = user.split("Decision under review:", 1)[1].strip().lower()
    blocking = any(f[0] in ("error", "hold") for f in findings)
    return _reply(json.dumps({"issues": ["approval conflicts with blocking findings"] if under_review.startswith("approve") and blocking else []}))


def _reviewer(messages: list[dict], with_tools: bool) -> httpx.Response:
    user = messages[1]["content"]
    brief = _brief(user)
    if with_tools and not any(m["role"] == "tool" for m in messages):          # first turn: gather evidence
        inv = brief["invoice"]
        calls = [("lookup_vendor", {"name": inv["vendor"] or ""}), ("vendor_history", {"vendor": inv["vendor"] or ""})]
        calls += [("lookup_item", {"item": name}) for name in dict.fromkeys(item for item, _ in inv["items"])][:4]
        return _reply(tool_calls=[{"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls, 1)])
    findings = brief["findings"]
    errors, holds = [f for f in findings if f[0] == "error"], [f for f in findings if f[0] == "hold"]
    checked = "ERP lookups agree" if with_tools else "findings reviewed"
    if errors:
        return _reply(json.dumps({"decision": "reject", "reasoning": f"{errors[0][2]} ({checked})."}))
    if holds:
        return _reply(json.dumps({"decision": "escalate", "reasoning": f"{holds[0][2]}: needs a person ({checked})."}))
    if "Worth a look:" in user:
        why = user.split("Worth a look:", 1)[1].split("\n")[0].strip(" .")
        return _reply(json.dumps({"decision": "escalate", "reasoning": f"Unusual for this vendor: {why}."}))
    return _reply(json.dumps({"decision": "approve", "reasoning": f"No errors or holds; vendor and items verified ({checked}); within the auto-approval limit."}))


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    messages = body["messages"]
    system, user = messages[0]["content"], messages[1]["content"]
    if system.startswith("You extract invoice data"):
        return _extraction(user)
    if "auditor" in system:
        return _critic(user)
    return _reviewer(messages, with_tools=bool(body.get("tools")))


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
