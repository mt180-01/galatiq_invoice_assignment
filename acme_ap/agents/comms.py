"""Comms agent: words for people. The facts, and the decision whether anyone should be contacted at all, come from the rules; a model only
phrases them. Anything a model writes is checked against the record before it is used:

  * every number in the text must appear in the facts (no invented amounts, dates or quantities);
  * every number in a required question must survive into the email (nothing quantitative is dropped);
  * no links or addresses.
If a check fails, or the model is unavailable, the deterministic template is used instead, so a bad reply costs nothing but the call.
"""
from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from ..llm import LLM, LLMUnavailable, as_agent, structured
from ..models import Outcome
from ..outreach import followup_context, render_followup

log = logging.getLogger("acme.comms")
_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


_LIST_MARK = re.compile(r"(?m)^\s*\d+[.)]\s+")


def numbers(text: str) -> set[str]:
    """Figures in the text, ignoring list numbering ("1. ...", "2) ...") which is layout, not content."""
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(_LIST_MARK.sub("", text))}


def grounding_problem(text: str, facts: str, required: str = "") -> str | None:
    if invented := numbers(text) - numbers(facts):
        return f"numbers not in the record: {sorted(invented)[:3]}"
    if dropped := numbers(required) - numbers(text):
        return f"required figures missing: {sorted(dropped)[:3]}"
    if re.search(r"https?://|www\.|@", text):
        return "contains a link or address"
    return None


class EmailBody(BaseModel):
    body: str = Field(min_length=20, max_length=1200)


class Summary(BaseModel):
    summary: str = Field(min_length=20, max_length=500)


class Comms:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def _ask(self, system: str, facts: dict, schema, required: str = ""):
        text = json.dumps(facts, default=str, separators=(",", ":"))
        try:
            with as_agent("comms"):
                reply = await structured(self.llm, system, text, schema)
        except LLMUnavailable:
            return None
        except Exception as exc:  # noqa: BLE001 - wording is optional: whatever goes wrong, the template is used
            log.warning("comms model step failed, using the template: %s: %s", type(exc).__name__, exc)
            return None
        written = next(iter(reply.model_dump().values()))
        problem = grounding_problem(written, text, required)
        if not problem and self.llm.meter:
            self.llm.meter.event("comms", "wrote", " ".join(written.split())[:200])
        if problem:
            if self.llm.meter:
                self.llm.meter.event("comms", "rejected", f"model text discarded ({problem}); using the template")
            return None
        return written

    async def followup(self, outcome: Outcome) -> str | None:
        """One short question to the vendor, only when the rules say a vendor can fix it (suspicious senders are never contacted)."""
        ctx = followup_context(outcome)
        if ctx is None:
            return None
        body = None
        if self.llm.available:
            body = await self._ask(
                "You write short, polite emails from Acme Corp's Accounts Payable to a vendor. Use ONLY the facts given. Ask exactly the questions "
                "listed, each answerable in one line; add no other questions, numbers, links, deadlines or promises. At most five sentences. "
                'Sign off "Accounts Payable, Acme Corp". Reply as JSON: {"body": "..."}',
                {"invoice": ctx["ref"], "questions": [q for _, q in ctx["asks"]]}, EmailBody, required=" ".join(q for _, q in ctx["asks"]))
        return render_followup(ctx, body)

    async def explain(self, outcome: Outcome) -> str | None:
        """Plain English for the person who must act on a rejected or escalated invoice. None means 'use the recorded reasoning'."""
        inv, appr = outcome.invoice, outcome.approval
        if not self.llm.available or inv is None or appr is None or outcome.error or appr.decision.value == "approve" or outcome.status == "DUPLICATE":
            return None
        facts = {"invoice": inv.invoice_number, "vendor": inv.vendor, "total": str(inv.total), "currency": inv.currency, "decision": appr.decision.value,
                 "findings": [f.message for f in outcome.findings if f.severity.value != "warn"][:6], **({"next_owner": outcome.awaiting} if outcome.awaiting else {})}
        return await self._ask(
            "You write a plain-English note for a busy finance person about an invoice that was rejected or needs their decision. In at most three "
            "sentences say what the invoice is, what the checks found, and what to do next. Use ONLY the facts given; add no numbers, names or "
            'links that are not in them. Reply as JSON: {"summary": "..."}', facts, Summary)
