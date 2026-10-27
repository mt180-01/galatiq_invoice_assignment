"""Tools the Reviewer's model may call to check facts before it decides. All read-only; none can move money or change a record.
Results are kept small, because every byte comes back to us as tokens."""
from __future__ import annotations

from decimal import Decimal

from ..erp import ErpAdapter
from ..llm import Tool
from .validation import APPROVAL_THRESHOLD
from ..approvals.routing import CFO_THRESHOLD, VP_THRESHOLD

_STR = {"type": "string"}


def reviewer_tools(erp: ErpAdapter) -> list[Tool]:
    def lookup_item(item: str) -> dict:
        rec = erp.lookup_item(item)
        return {"found": False} if rec is None else {"found": True, "name": rec.name, "stock": rec.stock,
                                                      "unit_price": str(rec.unit_price), "approximate_match": rec.fuzzy}

    def lookup_vendor(name: str) -> dict:
        rec = erp.lookup_vendor(name)
        return {"found": False} if rec is None else {"found": True, "name": rec.name, "status": rec.status}

    def vendor_history(vendor: str) -> dict:
        return erp.vendor_history(vendor)

    def policy_limits() -> dict:
        return {"auto_approve_up_to_usd": str(APPROVAL_THRESHOLD), "vp_sign_off_above_usd": str(VP_THRESHOLD), "cfo_above_usd": str(CFO_THRESHOLD),
                "rules": "any error finding -> reject; any hold finding -> escalate to a person; you may only be stricter than this, never looser"}

    obj = lambda **props: {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}  # noqa: E731
    return [
        Tool("lookup_item", "Look up an item in the inventory: whether it exists, stock on hand and list price.", obj(item=_STR), lookup_item),
        Tool("lookup_vendor", "Look up a vendor in the approved vendor master: whether it exists and its status.", obj(name=_STR), lookup_vendor),
        Tool("vendor_history", "How many invoices this vendor has had paid or scheduled, and their average and largest total.", obj(vendor=_STR), vendor_history),
        Tool("policy_limits", "The company's approval limits and rules.", {"type": "object", "properties": {}}, policy_limits),
    ]
