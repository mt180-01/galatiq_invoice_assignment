"""Signed, expiring, single-purpose decision tokens for one-click approve/reject links.

A token binds (request, action, approver, invoice content hash, expiry) under an HMAC key held by AP, outside the database. Consequences:
  * it was issued to one mailbox: whoever holds the link acts as that person (the "magic link" model: control of the mailbox is the credential),
    so nobody types who they are, and nobody can claim to be someone else
  * it cannot be forged or edited (change the action or amount and the signature fails)
  * it dies if the invoice changes after the request was sent (the content hash no longer matches)
  * it expires; single use is enforced by the request's status in the store
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import Decimal

from ..models import Invoice


def _num(v) -> str:
    """250.0, 250.00 and 250 are the same amount: a PDF and a JSON of one invoice must hash alike."""
    return "None" if v is None else format(Decimal(str(v)).normalize(), "f")


def evidence_hash(inv: Invoice) -> str:
    """Hash of everything the approver is actually approving (and what makes two copies of an invoice the same invoice)."""
    canon = json.dumps({
        "vendor": inv.vendor, "number": inv.invoice_number, "revision": inv.revision, "currency": inv.currency,
        "total": _num(inv.total), "items": sorted((li.item, li.quantity, _num(li.unit_price)) for li in inv.line_items)},
        sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _sig(secret: bytes, body: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret, body, hashlib.sha256).digest()[:16]).decode().rstrip("=")


def make_token(secret: bytes, request_id: int, action: str, ev_hash: str, expires: datetime, who: str = "") -> str:
    body = json.dumps({"r": request_id, "a": action, "h": ev_hash, "w": who.strip().lower(), "x": int(expires.timestamp())}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + _sig(secret, body)


class TokenError(ValueError):
    pass


def read_token(secret: bytes, token: str, now: datetime) -> dict:
    try:
        raw, sig = token.split(".")
        body = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("malformed token") from exc
    if not hmac.compare_digest(sig, _sig(secret, body)):
        raise TokenError("signature does not match (token was altered or is not ours)")
    if now.timestamp() > data["x"]:
        raise TokenError("token expired")
    return data


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
