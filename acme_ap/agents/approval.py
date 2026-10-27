"""Approval agent (the simulated VP). Draft -> critique -> revise loop.

Safety principle: the LLM proposes, the policy engine disposes. A model can make a decision
*stricter* than policy, never looser, so a hallucinated "approve" cannot release money.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from ..llm import LLM, LLMUnavailable
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
        if warns:
            return Approval(decision=Decision.ESCALATE,
                            reasoning=f"Over {APPROVAL_THRESHOLD} with open warnings: " + "; ".join(f.message for f in warns))
        return Approval(decision=Decision.APPROVE, reasoning=f"Over {APPROVAL_THRESHOLD} but fully clean: known vendor, stock and arithmetic verified")
    note = f" (noted: {'; '.join(f.message for f in warns)})" if warns else ""
    return Approval(decision=Decision.APPROVE, reasoning="Within auto-approval limit; all checks passed" + note)


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


async def decide(inv: Invoice, findings: list[Finding], llm: LLM) -> Approval:
    floor = policy_decision(inv, findings)
    draft = floor
    critiques: list[str] = []
    if llm.available:
        try:
            draft = await _llm_draft(inv, findings, llm, None) or floor
            for _ in range(MAX_ROUNDS - 1):
                issues = critique(inv, findings, draft)
                issues += await _llm_critic(inv, findings, draft, llm)
                if not issues:
                    break
                critiques.extend(issues)
                draft = await _llm_draft(inv, findings, llm, issues) or floor
        except LLMUnavailable as exc:
            log.warning("approval LLM unavailable, using policy engine: %s", exc)
            draft = floor
    # Final guardrail: never looser than policy.
    final_issues = critique(inv, findings, draft)
    if final_issues:
        critiques.extend(final_issues)
        draft = floor
    draft.critiques = critiques
    draft.rounds = 1 + len(critiques) if critiques else 1
    return draft


def _brief(inv: Invoice, findings: list[Finding]) -> str:
    return json.dumps({
        "invoice": {"number": inv.invoice_number, "vendor": inv.vendor, "total": str(inv.total), "currency": inv.currency,
                    "items": [(li.item, li.quantity) for li in inv.line_items]},
        "findings": [f.model_dump(mode="json") for f in findings],
        "policy": f"auto-approve <= {APPROVAL_THRESHOLD} USD when clean; any error -> reject; any hold -> escalate to human",
    })


async def _llm_draft(inv, findings, llm: LLM, issues: list[str] | None) -> Approval | None:
    user = _brief(inv, findings) + (f"\nA reviewer objected: {issues}. Revise." if issues else "")
    data = await llm.chat_json(
        "You are the VP approving accounts-payable invoices. Decide approve|reject|escalate and justify briefly. "
        'JSON: {"decision": "...", "reasoning": "..."}', user)
    try:
        return Approval(decision=Decision(str(data["decision"]).lower()), reasoning=str(data["reasoning"]))
    except (KeyError, ValueError):
        return None


async def _llm_critic(inv, findings, draft: Approval, llm: LLM) -> list[str]:
    data = await llm.chat_json(
        "You are an internal auditor challenging an AP approval. Find policy violations or unsupported reasoning. "
        'JSON: {"issues": ["..."]} (empty list if the decision is sound).',
        _brief(inv, findings) + f"\nDecision under review: {draft.model_dump(mode='json')}")
    issues = data.get("issues", [])
    return [str(i) for i in issues] if isinstance(issues, list) else []
