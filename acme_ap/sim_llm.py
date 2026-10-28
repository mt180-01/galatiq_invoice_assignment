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


def _key(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def _tool_results(messages: list[dict]) -> list[tuple[str, dict, object]]:
    """[(tool name, arguments, result)] for every tool call already answered in this conversation."""
    calls = {c["id"]: c["function"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or []}
    out = []
    for m in messages:
        if m["role"] == "tool":
            fn = calls.get(m["tool_call_id"], {})
            out.append((fn.get("name", ""), json.loads(fn.get("arguments") or "{}"), json.loads(m["content"]).get("result")))
    return out


def _call(name: str, args: dict, i: int) -> dict:
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _resolver_items(messages: list[dict]) -> httpx.Response:
    """Looks each unknown item up, and corrects it only when the best catalogue match is the same name written differently."""
    names = [n.strip(" '") for n in messages[1]["content"].split("Not found:", 1)[1].strip(" []").split(",") if n.strip()]
    if not _tool_results(messages):
        return _reply(tool_calls=[_call("search_items", {"query": n}, i) for i, n in enumerate(names, 1)])
    fixes = []
    for _, args, result in _tool_results(messages):
        top = (result or [{}])[0]
        if top and top.get("similarity", 0) >= 0.8 and _key(args["query"])[:1] == _key(top["name"])[:1] and _key(args["query"])[-1:] == _key(top["name"])[-1:]:
            fixes.append({"written": args["query"], "correct": top["name"], "reason": f"closest catalogue item, similarity {top['similarity']}"})
    return _reply(json.dumps({"corrections": fixes}))


def _resolver_vendor(messages: list[dict]) -> httpx.Response:
    name = messages[1]["content"].split("Vendor on the invoice:", 1)[1].strip()
    if not _tool_results(messages):
        return _reply(tool_calls=[_call("search_vendors", {"query": name}, 1)])
    top = ((_tool_results(messages)[0][2] or [{}])[0])
    if top and top.get("similarity", 0) >= 0.75 and _key(name)[:1] == _key(top["name"])[:1]:
        return _reply(json.dumps({"match": top["name"], "reason": f"closest approved vendor, similarity {top['similarity']}"}))
    return _reply(json.dumps({"match": None, "reason": "no close approved vendor"}))


def _comms_email(user: str) -> httpx.Response:
    facts = json.loads(user)
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(facts["questions"], 1))
    return _reply(json.dumps({"body": f"Hello,\n\nThank you for invoice {facts['invoice']}. So we can pay it, could you help with the following "
                                      f"(a one-line reply is fine)?\n\n{qs}\n\nThank you,\nAccounts Payable, Acme Corp"}))


def _comms_summary(user: str) -> httpx.Response:
    f = json.loads(user)
    problems = "; ".join(f["findings"][:2]) or "no specific finding recorded"
    verb = {"reject": "was rejected by the checks", "escalate": "needs a person's decision"}.get(f["decision"], "was reviewed")
    nxt = f"Next: {f['next_owner']} decides." if f.get("next_owner") else "Nothing further is needed unless the vendor sends a corrected invoice."
    return _reply(json.dumps({"summary": f"Invoice {f['invoice']} from {f['vendor']} for {f['currency']} {f['total']} {verb}: {problems}. {nxt}"}))


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    messages = body["messages"]
    system, user = messages[0]["content"], messages[1]["content"]
    if system.startswith("You extract invoice data"):
        return _extraction(user)
    if system.startswith("You resolve item names"):
        return _resolver_items(messages)
    if system.startswith("The vendor on an invoice is not in the approved vendor master"):
        return _resolver_vendor(messages)
    if system.startswith("You write short, polite emails"):
        return _comms_email(user)
    if system.startswith("You write a plain-English note"):
        return _comms_summary(user)
    if "auditor" in system:
        return _critic(user)
    return _reviewer(messages, with_tools=bool(body.get("tools")))


def transport() -> httpx.MockTransport:
    return httpx.MockTransport(handler)
