"""Approval agent (the simulated VP). Draft -> critique -> revise loop.

Safety principle: the LLM proposes, the policy engine disposes. A model can make a decision
*stricter* than policy, never looser, so a hallucinated "approve" cannot release money.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..llm import LLM, LLMUnavailable, Tool, as_agent, structured
from ..models import Approval, Decision, Finding, Invoice, Severity
from .validation import APPROVAL_THRESHOLD

log = logging.getLogger("acme.approval")
MAX_ROUNDS = 3
_RANK = {Decision.APPROVE: 0, Decision.ESCALATE: 1, Decision.REJECT: 2}


def policy_decision(inv: Invoice, findings: list[Finding]) -> Approval:
    """Deterministic floor. Same inputs, same answer."""
    errors = [f for f in findings if f.severity is Severity.ERROR]
    holds = [f for f in findings if f.severity is Severity.HOLD]
    warns = [f for f in findings if f.severity is Severity.WARN]
    total = inv.total or Decimal(0)
    if errors:
        return Approval(decision=Decision.REJECT, reasoning="Rejected: " + "; ".join(f.message for f in errors))
    if holds:
        return Approval(decision=Decision.ESCALATE, reasoning="Needs human review: " + "; ".join(f.message for f in holds))
    if total > APPROVAL_THRESHOLD:
        # Above the limit a person signs off however clean the invoice is; the system only prepares the decision.
        why = "open warnings: " + "; ".join(f.message for f in warns) if warns else "all checks clean"
        return Approval(decision=Decision.ESCALATE, reasoning=f"Over {APPROVAL_THRESHOLD}: human sign-off required ({why})")
    note = f" (noted: {'; '.join(f.message for f in warns)})" if warns else ""
    return Approval(decision=Decision.APPROVE, reasoning="Within auto-approval limit; all checks passed" + note)


def first_pass(inv: Invoice, findings: list[Finding]) -> Approval:
    """The VP's first read: any outright error, and how big is it? It deliberately does not weigh holds, warnings or
    risk signals; that is the critic's job, and what makes the review a two-step reflection rather than one rule."""
    errors = [f for f in findings if f.severity is Severity.ERROR]
    if errors:
        return Approval(decision=Decision.REJECT, reasoning="Rejected: " + "; ".join(f.message for f in errors))
    if (inv.total or Decimal(0)) > APPROVAL_THRESHOLD:
        return Approval(decision=Decision.ESCALATE, reasoning=f"Over {APPROVAL_THRESHOLD}: needs sign-off")
    return Approval(decision=Decision.APPROVE, reasoning="No errors and within the auto-approval limit")


def critique(inv: Invoice, findings: list[Finding], draft: Approval) -> list[str]:
    """Independent rule-based critic: re-derives the strictest defensible decision from the evidence."""
    floor = policy_decision(inv, findings)
    issues = []
    if _RANK[draft.decision] < _RANK[floor.decision]:
        issues.append(f"policy requires at least {floor.decision.value}, draft says {draft.decision.value}")
    if draft.decision is Decision.APPROVE and any(f.severity is not Severity.WARN for f in findings):
        issues.append("approval conflicts with blocking findings")
    if len(draft.reasoning.strip()) < 15:
        issues.append("reasoning is too thin to audit")
    return issues


class ReviewDecision(BaseModel):
    """The only shape a reviewer's answer may take (structured output, validated; a malformed reply is sent back for correction)."""
    decision: Literal["approve", "reject", "escalate"]
    reasoning: str = Field(min_length=10, max_length=600)

    @field_validator("decision", mode="before")
    @classmethod
    def _normalise(cls, v):
        return str(v).strip().lower()


class AuditIssues(BaseModel):
    issues: list[str] = Field(default_factory=list, max_length=8)


def second_opinion_worth_paying_for(inv: Invoice, findings: list[Finding], floor: Approval, risk_hint: str | None = None) -> bool:
    """The model can only make a decision stricter, so it can matter in exactly one case: policy would approve.
    Even then, a small, clean invoice read from a structured file has nothing a second opinion could catch."""
    if floor.decision is not Decision.APPROVE:
        return False  # already rejected or with a person: nothing to tighten, and a person will look anyway
    return (any(f.severity is Severity.WARN for f in findings) or (inv.total or Decimal(0)) >= APPROVAL_THRESHOLD / 2
            or inv.lane == "ai-extracted" or bool(inv.parse_warnings) or bool(risk_hint))


async def decide(inv: Invoice, findings: list[Finding], llm: LLM, gate: bool = False, tools: list[Tool] | None = None,
                 risk_hint: str | None = None) -> Approval:
    """gate=True spends model calls only where they can change the outcome (see second_opinion_worth_paying_for), and
    runs the critic only when the draft departs from policy: one call for the common case, three at most."""
    floor = policy_decision(inv, findings)
    draft = first_pass(inv, findings)  # a quick read of the headlines; the critique below checks it against everything
    critiques: list[str] = []
    consulted = False
    if llm.available and gate and not second_opinion_worth_paying_for(inv, findings, floor, risk_hint):
        if llm.meter:
            llm.meter.skip("reviewer")
            llm.meter.skip("critic")
    elif llm.available:
        try:
            with as_agent("reviewer"):
                draft = await _llm_draft(inv, findings, llm, None, tools, risk_hint) or floor
                consulted = True
            for _ in range(MAX_ROUNDS - 1):
                issues = critique(inv, findings, draft)
                if not (gate and not issues and draft.decision == floor.decision):  # agreeing with policy needs no challenger
                    with as_agent("critic"):
                        issues += await _llm_critic(inv, findings, draft, llm)
                elif llm.meter:
                    llm.meter.skip("critic")
                if not issues:
                    break
                critiques.extend(issues)
                with as_agent("reviewer"):
                    draft = await _llm_draft(inv, findings, llm, issues, tools, risk_hint) or floor
        except LLMUnavailable as exc:
            log.warning("approval LLM unavailable, using policy engine: %s", exc)
            draft = floor
    # Reflection, always on and free: the draft is challenged against the complete evidence, and revised until the
    # challenger has nothing left to object to (never looser than policy).
    revisions = 0
    for _ in range(MAX_ROUNDS):
        issues = critique(inv, findings, draft)
        if not issues:
            break
        critiques.extend(issues)
        revisions += 1
        draft = Approval(decision=floor.decision, reasoning=floor.reasoning)
        log.info("draft revised after critique: %s", "; ".join(issues))
    if draft.decision == floor.decision and not consulted:
        draft.reasoning = floor.reasoning  # the full-evidence wording (warnings noted, limits cited), not the quick read's
    draft.critiques = critiques
    draft.rounds = 1 + revisions
    return draft


def _brief(inv: Invoice, findings: list[Finding]) -> str:
    """The least the model needs: no free text from the invoice beyond names, findings as code + message."""
    return json.dumps({
        "invoice": {"number": inv.invoice_number, "vendor": inv.vendor, "total": str(inv.total), "currency": inv.currency,
                    "items": [(li.item, li.quantity) for li in inv.line_items]},
        "findings": [[f.severity.value, f.code, f.message] for f in findings],
        "policy": f"auto-approve <= {APPROVAL_THRESHOLD} USD when clean; any error -> reject; any hold -> escalate to human",
    }, separators=(",", ":"))


async def _llm_draft(inv, findings, llm: LLM, issues: list[str] | None, tools: list[Tool] | None = None, risk_hint: str | None = None) -> Approval | None:
    user = _brief(inv, findings) + (f"\nWorth a look: {risk_hint}." if risk_hint else "") + (f"\nA reviewer objected: {issues}. Revise." if issues else "")
    reply = await structured(
        llm, "You are the VP approving accounts-payable invoices. Decide approve|reject|escalate and justify briefly. "
        + ("You may call tools to verify facts, but only the ones you need, each at most once (vendor, one call per distinct item, "
           "the vendor's payment history), and then decide: the findings you were given are already checked. " if tools else "")
        + 'Reply as JSON: {"decision": "approve|reject|escalate", "reasoning": "..."}', user, ReviewDecision, tools)
    return Approval(decision=Decision(reply.decision), reasoning=reply.reasoning)


async def _llm_critic(inv, findings, draft: Approval, llm: LLM) -> list[str]:
    reply = await structured(
        llm, "You are an internal auditor challenging an AP approval. Find policy violations or unsupported reasoning. "
        'Reply as JSON: {"issues": ["..."]} (empty list if the decision is sound).',
        _brief(inv, findings) + f"\nDecision under review: {draft.decision.value}: {draft.reasoning}", AuditIssues)
    return reply.issues
