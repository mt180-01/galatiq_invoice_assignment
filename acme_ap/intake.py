"""Email intake: vendors keep doing what they already do (email an invoice); we do the rest.

Accepts .eml files. Every supported attachment becomes a document; an email with no usable
attachment is treated as an invoice in the body (like data/invoices/invoice_1008.txt).
Sender and subject are kept so a follow-up can be addressed to the right person.
"""
from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass
from pathlib import Path

from .ingestion import SUPPORTED


@dataclass(frozen=True)
class Origin:
    sender: str | None
    subject: str | None
    email_file: str


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", name)[:80] or "attachment"


def expand(paths: list[str], workdir: Path) -> tuple[list[str], dict[str, Origin]]:
    """Replace each .eml with the documents inside it. Non-email paths pass through unchanged."""
    out: list[str] = []
    origins: dict[str, Origin] = {}
    for p in paths:
        if not p.lower().endswith(".eml"):
            out.append(p)
            continue
        msg = email.message_from_bytes(Path(p).read_bytes(), policy=email.policy.default)
        origin = Origin(msg.get("From"), msg.get("Subject"), p)
        dest = workdir / _safe(Path(p).stem)
        dest.mkdir(parents=True, exist_ok=True)
        found = 0
        for part in msg.iter_attachments():
            name = part.get_filename() or ""
            if Path(name).suffix.lower() in SUPPORTED:
                target = dest / _safe(name)
                target.write_bytes(part.get_content() if isinstance(part.get_content(), bytes) else part.get_content().encode())
                out.append(str(target))
                origins[str(target)] = origin
                found += 1
        if not found:
            body = msg.get_body(preferencelist=("plain", "html"))
            if body is not None:
                target = dest / "body.txt"
                # keep the header block: the sender is often the only vendor hint in a body-only invoice
                target.write_text(f"From: {origin.sender}\nSubject: {origin.subject}\n\n{body.get_content()}")
                out.append(str(target))
                origins[str(target)] = origin
    return out, origins
