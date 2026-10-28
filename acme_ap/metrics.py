"""What changed for the business, measured on a batch: how many invoices needed nobody, what was stopped
before it reached the bank and why, how long things spent in the system, and what is waiting on a person."""
from __future__ import annotations

import os
import statistics
from typing import Any
from collections import Counter
from dataclasses import dataclass, field

from .models import Outcome, Severity


@dataclass
class Kpis:
    invoices: int = 0                 # distinct invoices (duplicates excluded)
    hands_off: int = 0                # paid or scheduled with no person involved
    stopped: int = 0                  # rejected before payment
    awaiting: int = 0                 # waiting for a named person
    asked_vendor: int = 0             # a follow-up question was drafted
    duplicates_blocked: int = 0
    reasons: Counter = field(default_factory=Counter)   # why stopped invoices were stopped (one count per invoice)
    median_ms: float = 0.0
    p95_ms: float = 0.0

    @property
    def hands_off_rate(self) -> float:
        return self.hands_off / self.invoices if self.invoices else 0.0


def llm_usage(outcomes: list[Outcome]) -> dict:
    """Model use across a batch, from the per-invoice records: what was called, cached, and deliberately not called."""
    use: dict[str, Any] = {"calls": 0, "cached": 0, "tools": 0, "tokens_in": 0, "tokens_out": 0, "ticks": 0, "skipped": Counter(), "cost": None}
    for o in outcomes:
        for k in ("calls", "cached", "tools", "tokens_in", "tokens_out"):
            use[k] += o.llm.get(k, 0)
        use["skipped"].update(o.llm.get("skipped", []))
        use["ticks"] += o.llm.get("cost_ticks", 0)
    if use["ticks"]:
        use["cost"] = use["ticks"] / 10_000_000_000  # exact: reported by the API call by call
        return use
    try:
        p_in, p_out = float(os.environ["XAI_PRICE_IN"]), float(os.environ["XAI_PRICE_OUT"])
        use["cost"] = (use["tokens_in"] * p_in + use["tokens_out"] * p_out) / 1_000_000
    except (KeyError, ValueError):
        pass
    return use


def kpis(outcomes: list[Outcome]) -> Kpis:
    k = Kpis(duplicates_blocked=sum(o.status == "DUPLICATE" for o in outcomes))
    times = []
    for o in outcomes:
        if o.status == "DUPLICATE" or not o.invoice:
            continue
        k.invoices += 1
        k.asked_vendor += bool(o.followup)
        if o.error:
            continue
        times.append(sum(o.timings_ms.values()))
        if o.status in ("PAID", "SCHEDULED"):
            k.hands_off += 1
        elif o.status == "AWAITING":
            k.awaiting += 1
        elif o.status == "REJECT":
            k.stopped += 1
            k.reasons.update({f.code for f in o.findings if f.severity is Severity.ERROR})
    if times:
        times.sort()
        k.median_ms = statistics.median(times)
        k.p95_ms = times[min(len(times) - 1, int(len(times) * 0.95))]
    return k
