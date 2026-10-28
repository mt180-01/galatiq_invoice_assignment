"""How many model calls and tokens does the team need, compared with asking the model about everything?

Runs the sample invoices three ways through the local stand-in model (it answers sensibly, so the real prompts, tool calls and request flow
are exercised, and token counts use the same ~4 chars/token estimate the client falls back to):
  always, raw prompts   : the original behaviour: extract every unstructured invoice, draft + challenge every decision
  always, compact       : same calls, shorter prompts
  gated (default)       : calls only where the model can change the outcome
These are VOLUMES. Multiply tokens by your own price (XAI_PRICE_IN / XAI_PRICE_OUT per 1M tokens) for money. A stub cannot show whether a real
model would answer as well; it shows how much you would be asking it, and that the outcomes do not change when you ask less.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acme_ap.erp import MockNetSuite  # noqa: E402
from acme_ap import sim_llm  # noqa: E402
from acme_ap.ingestion import llm_extract  # noqa: E402
from acme_ap.llm import GrokClient, Meter  # noqa: E402
from acme_ap.payments import MockBank, PaymentService  # noqa: E402
from acme_ap.pipeline import Pipeline  # noqa: E402

CORPUS = [*sorted((ROOT / "data" / "invoices").iterdir()), *sorted((ROOT / "data" / "approvals_demo").iterdir())]


def run(policy: str, compact: bool) -> tuple[Meter, dict[str, str]]:
    llm_extract.COMPACT_PROMPTS = compact
    meter = Meter()
    llm = GrokClient("stub", transport=sim_llm.transport(), meter=meter)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "m.db"
        erp = MockNetSuite(db)
        pay = PaymentService(db, erp, MockBank(str(db) + ".bank"))
        outs = asyncio.run(Pipeline(erp, llm, workers=1, payments=pay, llm_policy=policy).run([str(p) for p in CORPUS]))
    llm_extract.COMPACT_PROMPTS = True
    return meter, {Path(o.source_path).name: o.status for o in outs}


def main() -> None:
    rows = [("always, raw prompts", *run("always", False)), ("always, compact prompts", *run("always", True)), ("gated (default)", *run("gated", True))]
    base_tokens = sum(rows[0][1].tokens_in.values()) + sum(rows[0][1].tokens_out.values())
    print(f"{len(CORPUS)} documents\n")
    print(f"{'policy':<26}{'calls':>7}{'intake':>8}{'reviewer':>9}{'critic':>7}{'resolver':>9}{'comms':>7}{'tokens in':>11}{'tokens out':>11}{'total':>9}{'vs original':>13}")
    for name, m, _ in rows:
        t_in, t_out = sum(m.tokens_in.values()), sum(m.tokens_out.values())
        print(f"{name:<26}{m.total_calls:>7}{m.calls['intake']:>8}{m.calls['reviewer']:>9}{m.calls['critic']:>7}{m.calls['resolver']:>9}{m.calls['comms']:>7}{t_in:>11,}{t_out:>11,}"
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
