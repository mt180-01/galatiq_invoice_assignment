"""Self-correction for unknown names: when validation cannot find an item, an agent searches the catalogue and proposes the likely
intended name; we re-validate. The model suggests, deterministic checks decide:

  * Items are corrected only if the model's pick exists in the catalogue AND is textually very close to what was written (>= MIN_SIMILARITY).
    A model that wants to turn "SuperGizmo" into "WidgetA" is refused however confident it sounds.
  * Vendors are NEVER corrected. A look-alike vendor name is a classic payment-fraud trick, so a suggested match is only shown to the
    person who decides ("did you mean ...? confirm by phone"); the unknown-vendor hold stays.
Both searches are read-only tools the model calls itself.
"""
from __future__ import annotations

import difflib
import re

from pydantic import BaseModel, Field

from ..erp import ErpAdapter
from ..llm import LLM, LLMUnavailable, Tool, as_agent, structured
from ..models import Finding, Invoice, Severity

MIN_SIMILARITY = 0.8
MAX_NAMES = 6


class Correction(BaseModel):
    written: str
    correct: str
    reason: str = ""


class Corrections(BaseModel):
    corrections: list[Correction] = Field(default_factory=list, max_length=MAX_NAMES)


class VendorSuggestion(BaseModel):
    match: str | None = None
    reason: str = ""


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _key(a), _key(b)).ratio()


def search_tools(erp: ErpAdapter) -> list[Tool]:
    obj = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False}
    return [Tool("search_items", "Find catalogue items whose names resemble the query. Returns candidates with a 0-1 similarity.", obj,
                 lambda query: erp.search_items(query)),
            Tool("search_vendors", "Find approved vendors whose names resemble the query. Returns candidates with a 0-1 similarity.", obj,
                 lambda query: erp.search_vendors(query))]


async def correct_items(inv: Invoice, erp: ErpAdapter, llm: LLM) -> list[Finding]:
    """Fix mistyped item names in place (original kept in the line's note). Returns WARN findings describing each fix."""
    unknown = list(dict.fromkeys(li.item for li in inv.line_items if erp.lookup_item(li.item) is None))[:MAX_NAMES]
    if not unknown or not llm.available:
        return []
    try:
        with as_agent("resolver"):
            reply = await structured(
                llm, "You resolve item names on an invoice. These names are not in the catalogue. Use search_items to look for the product each was "
                "probably meant to be. Correct a name ONLY if it is clearly the same product written differently (typo, spacing, case, punctuation); "
                "if it could be a different product, leave it out. Never invent catalogue names. "
                'Reply as JSON: {"corrections": [{"written": "...", "correct": "...", "reason": "..."}]}', f"Not found: {unknown}",
                Corrections, search_tools(erp))
    except LLMUnavailable:
        return []
    findings = []
    for c in reply.corrections:
        record = erp.lookup_item(c.correct)
        score = similarity(c.written, c.correct)
        if c.written not in unknown or record is None or record.fuzzy or score < MIN_SIMILARITY:
            if llm.meter:
                llm.meter.event("resolver", "refused", f"{c.written!r} -> {c.correct!r} refused (similarity {score:.2f}; must be an existing catalogue item and >= {MIN_SIMILARITY})")
            continue
        for li in inv.line_items:
            if li.item == c.written:
                li.item, li.note = record.name, f"as written: {c.written}"
        if llm.meter:
            llm.meter.event("resolver", "corrected", f"{c.written!r} -> {record.name!r} (similarity {score:.2f})")
        findings.append(Finding(code="item_corrected", severity=Severity.WARN,
                                message=f"item {c.written!r} read as {record.name!r} (found by catalogue search, similarity {score:.2f}); confirm if unexpected"))
    return findings


async def suggest_vendor(inv: Invoice, erp: ErpAdapter, llm: LLM) -> str | None:
    """A possible match for an unknown vendor, as advice to the person who decides. Never changes the invoice."""
    if not inv.vendor or not llm.available:
        return None
    try:
        with as_agent("resolver"):
            reply = await structured(
                llm, "The vendor on an invoice is not in the approved vendor master. Use search_vendors to see whether it could be an approved vendor "
                'written differently. Reply as JSON: {"match": "<approved vendor name or null>", "reason": "..."}. Say null if unsure.',
                f"Vendor on the invoice: {inv.vendor}", VendorSuggestion, search_tools(erp))
    except LLMUnavailable:
        return None
    record = erp.lookup_vendor(reply.match) if reply.match else None
    if record is None:
        return None
    if llm.meter:
        llm.meter.event("resolver", "suggested", f"vendor {inv.vendor!r} might be {record.name!r}; NOT applied")
    return (f"possible match: approved vendor {record.name!r}. Look-alike names are a common fraud pattern, so confirm by calling a known number "
            "before treating them as the same company")
