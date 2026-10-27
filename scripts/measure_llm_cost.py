"""How many model calls and tokens does the team need, compared with asking the model about everything?

Runs the sample invoices three ways through a stand-in Grok (an HTTP stub that answers correctly, so the real prompts and the real
request flow are exercised, and token counts use the same ~4 chars/token estimate the client falls back to):
  always, raw prompts   : the original behaviour: extract every unstructured invoice, draft + challenge every decision
  always, compact       : same calls, shorter prompts
  gated (default)       : calls only where the model can change the outcome
These are VOLUMES. Multiply tokens by your own price (XAI_PRICE_IN / XAI_PRICE_OUT per 1M tokens) for money. A stub cannot show whether a real
model would answer as well; it shows how much you would be asking it, and that the outcomes do not change when you ask less.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acme_ap.erp import MockNetSuite  # noqa: E402
from acme_ap.ingestion import heuristic, llm_extract  # noqa: E402
from acme_ap.llm import GrokClient, Meter  # noqa: E402
from acme_ap.payments import MockBank, PaymentService  # noqa: E402
from acme_ap.pipeline import Pipeline  # noqa: E402

CORPUS = [*sorted((ROOT / "data" / "invoices").iterdir()), *sorted((ROOT / "data" / "approvals_demo").iterdir())]


def stub(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    if system.startswith("You extract invoice data"):
        inv = heuristic.extract(user.split("Invoice text:\n", 1)[1].split("\n\nYour previous", 1)[0])
        content = json.dumps({"invoice_number": inv.invoice_number, "vendor": inv.vendor, "date": inv.date, "due_date": inv.due_date,
                              "currency": inv.currency, "subtotal": str(inv.subtotal) if inv.subtotal is not None else None,
                              "tax_amount": str(inv.tax_amount) if inv.tax_amount is not None else None,
                              "shipping": str(inv.shipping) if inv.shipping is not None else None,
                              "total": str(inv.total) if inv.total is not None else None, "bank_account": inv.bank_account,
                              "line_items": [{"item": li.item, "quantity": li.quantity, "unit_price": str(li.unit_price) if li.unit_price else None,
                                              "amount": str(li.amount) if li.amount else None} for li in inv.line_items]})
    elif "auditor" in system:
        content = json.dumps({"issues": []})
    else:
        findings = json.loads(user.split("\nA reviewer objected")[0])["findings"]
        kinds = {f[0] for f in findings}
        decision = "reject" if "error" in kinds else "escalate" if "hold" in kinds else "approve"
        content = json.dumps({"decision": decision, "reasoning": f"Policy outcome {decision} given the recorded findings."})
    usage = {"prompt_tokens": (len(system) + len(user)) // 4, "completion_tokens": len(content) // 4}
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": usage})


def run(policy: str, compact: bool) -> tuple[Meter, dict[str, str]]:
    llm_extract.COMPACT_PROMPTS = compact
    meter = Meter()
    llm = GrokClient("stub", transport=httpx.MockTransport(stub), meter=meter)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        erp = MockNetSuite(db)
        pay = PaymentService(db, erp, MockBank(str(db) + ".bank"))
        outs = asyncio.run(Pipeline(erp, llm, workers=1, payments=pay, llm_policy=policy).run([str(p) for p in CORPUS]))
    llm_extract.COMPACT_PROMPTS = True
    return meter, {Path(o.source_path).name: o.status for o in outs}


def main() -> None:
    rows = [("always, raw prompts", *run("always", False)), ("always, compact prompts", *run("always", True)), ("gated (default)", *run("gated", True))]
    base_calls, base_tokens = rows[0][1].total_calls, sum(rows[0][1].tokens_in.values()) + sum(rows[0][1].tokens_out.values())
    print(f"{len(CORPUS)} documents\n")
    print(f"{'policy':<26}{'calls':>7}{'intake':>8}{'reviewer':>9}{'critic':>7}{'tokens in':>11}{'tokens out':>11}{'total':>9}{'vs original':>13}")
    for name, m, _ in rows:
        t_in, t_out = sum(m.tokens_in.values()), sum(m.tokens_out.values())
        print(f"{name:<26}{m.total_calls:>7}{m.calls['intake']:>8}{m.calls['reviewer']:>9}{m.calls['critic']:>7}{t_in:>11,}{t_out:>11,}"
              f"{t_in + t_out:>9,}{(t_in + t_out) / base_tokens:>12.0%} ")
    # which of two formats of the same invoice is processed first is timing-dependent, so compare the multiset of outcomes
    same = sorted(rows[0][2].values()) == sorted(rows[1][2].values()) == sorted(rows[2][2].values())
    print(f"\nOutcomes identical across all three: {same}")
    if not same:
        for k in rows[0][2]:
            if len({r[2][k] for r in rows}) > 1:
                print("  differs:", k, [r[2][k] for r in rows])


if __name__ == "__main__":
    main()
