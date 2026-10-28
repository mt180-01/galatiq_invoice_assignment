"""Approval workflow: request -> decision card -> one-click response -> re-check -> pay, with reminders,
escalation and expiry. State lives in SQLite next to the ERP mock; time is injectable for testing.

Nothing here sends email: cards are written as drafts. A real deployment would hand `render_card` output to
the mail system and expose `respond` behind an authenticated HTTPS endpoint.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..agents import validation
from ..erp import ErpAdapter
from ..models import Invoice, Outcome, Severity
from ..payments import default_payments
from .routing import DIRECTORY, Route, authorized, route
from .tokens import TokenError, evidence_hash, make_token, read_token, utcnow

REMINDER_AFTER = timedelta(hours=24)
ESCALATE_AFTER = timedelta(hours=48)
EXPIRE_AFTER = timedelta(days=5)
# Facts a person cannot waive by clicking approve, even when validation downgraded them (e.g. OCR-sourced invoices).
HARD_FACTS = {"unknown_item", "out_of_stock", "insufficient_stock", "vendor_blocked", "invalid_quantity", "negative_total", "zero_total", "fraud_indicators", "bank_account_changed"}
LINK_BASE = os.environ.get("ACME_BASE_URL", "http://127.0.0.1:8765").rstrip("/") + "/decide?t="  # the local web interface (`python main.py serve`)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS approval_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bill_key TEXT, ev_hash TEXT, invoice_json TEXT, outcome_json TEXT,
    role TEXT, reasons TEXT, status TEXT DEFAULT 'pending', created_at TEXT, reminded_at TEXT,
    escalated_from TEXT, decided_by TEXT, decided_at TEXT, comment TEXT, payment_ref TEXT);
CREATE TABLE IF NOT EXISTS approval_events (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, at TEXT, event TEXT, detail TEXT);
"""


@dataclass
class Result:
    ok: bool
    message: str
    paid: bool = False
    payment_ref: str | None = None
    amount: Decimal | None = None


class ApprovalService:
    def __init__(self, db_path: str | Path, erp: ErpAdapter, reset: bool = True, payments=None):
        self.erp = erp
        self.payments = payments or default_payments(erp)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._db.executescript(_SCHEMA)
            if reset:
                self._db.executescript("DELETE FROM approval_requests; DELETE FROM approval_events; "
                                       "DELETE FROM sqlite_sequence WHERE name IN ('approval_requests', 'approval_events');")
            row = self._db.execute("SELECT v FROM meta WHERE k='secret'").fetchone()
            if not row:
                self._db.execute("INSERT INTO meta VALUES ('secret', ?)", (secrets.token_hex(32),))
            self._db.commit()
            self._secret = bytes.fromhex(self._db.execute("SELECT v FROM meta WHERE k='secret'").fetchone()[0])

    # ---- helpers ---------------------------------------------------------------------------
    def _event(self, rid: int, at: datetime, event: str, detail: str = "") -> None:
        self._db.execute("INSERT INTO approval_events (request_id, at, event, detail) VALUES (?,?,?,?)", (rid, at.isoformat(), event, detail))

    def _fetch(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """One connection is shared by every thread, so reads take the lock too, and the row type is set on a private
        cursor (flipping it on the shared connection let one thread's reads come back in another's shape)."""
        with self._lock:
            cur = self._db.cursor()
            cur.row_factory = sqlite3.Row
            return cur.execute(sql, params).fetchall()

    def _row(self, rid: int) -> sqlite3.Row | None:
        rows = self._fetch("SELECT * FROM approval_requests WHERE id=?", (rid,))
        return rows[0] if rows else None

    def _token(self, rid: int, action: str, ev_hash: str, now: datetime) -> str:
        return make_token(self._secret, rid, action, ev_hash, now + EXPIRE_AFTER)

    # ---- create ----------------------------------------------------------------------------
    def open_request(self, outcome: Outcome, bill_key: str, now: datetime | None = None) -> tuple[int, Route] | None:
        """Create a pending request for an escalated invoice. Returns None if no human decision applies."""
        r = route(outcome)
        if r is None or not r.approvable:
            return None
        now = now or utcnow()
        inv = outcome.invoice
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO approval_requests (bill_key, ev_hash, invoice_json, outcome_json, role, reasons, created_at) VALUES (?,?,?,?,?,?,?)",
                (bill_key, evidence_hash(inv), inv.model_dump_json(), outcome.model_dump_json(), r.role, json.dumps(r.reasons), now.isoformat()))
            rid = cur.lastrowid
            self._event(rid, now, "opened", f"routed to {r.role}")
            self._db.commit()
        return rid, r

    # ---- the decision card -----------------------------------------------------------------
    def render_card(self, rid: int, now: datetime | None = None, note: str = "") -> str:
        now = now or utcnow()
        row = self._row(rid)
        inv = Invoice.model_validate_json(row["invoice_json"])
        out = Outcome.model_validate_json(row["outcome_json"])
        reasons = json.loads(row["reasons"])
        d = DIRECTORY[row["role"]]
        verified = [f for f in out.findings if f.severity is Severity.WARN]
        lines = [f"To: {d['email']}", f"Subject: Decision needed: {inv.currency} {inv.total} to {inv.vendor or 'unknown vendor'} ({inv.invoice_number})", ""]
        if note:
            lines += [note, ""]
        lines += [f"Pay {inv.currency} {inv.total} to {inv.vendor or '(vendor missing)'}?", "",
                  "WHY YOU", *[f"  - {x}" for x in reasons], "",
                  "THE INVOICE",
                  f"  Invoice {inv.invoice_number}, dated {inv.date or '?'}, due {inv.due_date or '?'}, terms {inv.payment_terms or '?'}",
                  f"  {len(inv.line_items)} line(s): " + ", ".join(f"{li.item} x{li.quantity}" for li in inv.line_items[:5]) + (" ..." if len(inv.line_items) > 5 else ""),
                  f"  Read by: {inv.extractor} ({inv.lane})", "",
                  *(["IN SHORT", f"  {out.explanation}", ""] if out.explanation else []),
                  "SYSTEM RECOMMENDATION",
                  ("  APPROVE: every automated check passed; the only reason this needs you is the amount."
                   if not any(f.severity is Severity.HOLD for f in out.findings) else
                   "  REVIEW BEFORE APPROVING: " + "; ".join(f.message for f in out.findings if f.severity is Severity.HOLD))]
        if verified:
            lines += ["", "NOTED (did not block)", *[f"  - {f.message}" for f in verified]]
        lines += ["", f"  APPROVE: {LINK_BASE}{self._token(rid, 'approve', row['ev_hash'], now)}",
                  f"  REJECT:  {LINK_BASE}{self._token(rid, 'reject', row['ev_hash'], now)}", "",
                  "These links work once, only for this invoice as shown, and expire in 5 days.",
                  "Nothing is paid until you approve; if you do not respond it will be escalated to your delegate."]
        return "\n".join(lines)

    # ---- respond ---------------------------------------------------------------------------
    def respond(self, token: str, who: str, comment: str = "", now: datetime | None = None) -> Result:
        now = now or utcnow()
        try:
            data = read_token(self._secret, token, now)
        except TokenError as exc:
            return Result(False, f"refused: {exc}")
        rid, action = data["r"], data["a"]
        with self._lock:
            row = self._row(rid)
            if row is None:
                return Result(False, "refused: unknown request")
            if row["status"] != "pending":
                return Result(False, f"refused: request already {row['status']}")
            if who.strip().lower() not in authorized(row["role"], row["escalated_from"]):
                self._event(rid, now, "refused", f"{who} is not authorized to decide for {row['role']}")
                self._db.commit()
                return Result(False, f"refused: {who} is not authorized to decide for {row['role']}")
            inv = Invoice.model_validate_json(row["invoice_json"])
            if evidence_hash(inv) != data["h"] or data["h"] != row["ev_hash"]:
                self._event(rid, now, "refused", "invoice content changed since the request was issued")
                self._db.commit()
                return Result(False, "refused: the invoice changed after this request was sent; a new request is needed")
            if action == "reject":
                self._close(rid, "rejected", who, comment, now)
                self.erp.settle_bill(row["bill_key"], "rejected", f"rejected by {who}: {comment or 'no comment'}")
                return Result(True, f"rejected by {who}")
            # Approve: the world may have moved since the request went out. Re-run the checks before any money moves.
            errors = [f for f in validation.validate(inv, self.erp, self.payments) if f.severity is Severity.ERROR or f.code in HARD_FACTS]
            if errors:
                self._close(rid, "blocked", who, comment, now)
                self._event(rid, now, "blocked", "; ".join(f.message for f in errors))
                self._db.commit()
                self.erp.settle_bill(row["bill_key"], "blocked", "approved but re-check failed: " + "; ".join(f.message for f in errors))
                return Result(False, "approval recorded but payment blocked: re-check failed: " + "; ".join(f.message for f in errors))
            res = self.payments.pay(inv, row["bill_key"])
            self._close(rid, "approved", who, comment, now, res.ref)
            self.erp.settle_bill(row["bill_key"], res.erp_status, f"approved by {who}: {comment or 'no comment'}",
                                 (inv.vendor or "", res.amount, res.ref) if res.submitted else None)
            return Result(True, f"approved by {who}; " + ("paid" if res.submitted else res.message), res.submitted, res.ref, res.amount)

    def _close(self, rid: int, status: str, who: str, comment: str, now: datetime, ref: str | None = None) -> None:
        self._db.execute("UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, comment=?, payment_ref=? WHERE id=?",
                         (status, who, now.isoformat(), comment, ref, rid))
        self._event(rid, now, status, f"{who}: {comment}")
        self._db.commit()

    # ---- time-driven: reminders, escalation, expiry ----------------------------------------
    def tick(self, now: datetime | None = None) -> list[str]:
        """Advance the clock. Returns human-readable actions taken (reminder / escalation / expiry drafts)."""
        now = now or utcnow()
        actions: list[str] = []
        with self._lock:
            rows = self._fetch("SELECT * FROM approval_requests WHERE status='pending'")
            for row in rows:
                age = now - datetime.fromisoformat(row["created_at"])
                rid, role = row["id"], row["role"]
                if age >= EXPIRE_AFTER:
                    self._close(rid, "expired", "system", "no response in 5 days", now)
                    self.erp.settle_bill(row["bill_key"], "expired", "no approver responded; not paid")
                    actions.append(f"#{rid} expired unpaid (no response from {role}); AP notified")
                elif age >= ESCALATE_AFTER and not row["escalated_from"] and DIRECTORY[role]["delegate"]:
                    new_role = DIRECTORY[role]["delegate"]
                    self._db.execute("UPDATE approval_requests SET role=?, escalated_from=? WHERE id=?", (new_role, role, rid))
                    self._event(rid, now, "escalated", f"{role} -> {new_role}")
                    self._db.commit()
                    actions.append(f"#{rid} escalated {role} -> {new_role}")
                elif age >= REMINDER_AFTER and not row["reminded_at"]:
                    self._db.execute("UPDATE approval_requests SET reminded_at=? WHERE id=?", (now.isoformat(), rid))
                    self._event(rid, now, "reminded", role)
                    self._db.commit()
                    actions.append(f"#{rid} reminder to {role}")
        return actions

    # ---- views -----------------------------------------------------------------------------
    def pending(self) -> list[dict]:
        rows = self._fetch("SELECT id, bill_key, role, reasons, status, created_at FROM approval_requests ORDER BY id")
        return [dict(r) for r in rows]

    def waiting(self, now: datetime | None = None) -> list[dict]:
        """Requests still waiting for a person: how long, who has it, what happens next if nobody acts."""
        now, out = now or utcnow(), []
        rows = self._fetch("SELECT * FROM approval_requests WHERE status='pending' ORDER BY id")
        for row in rows:
            age = now - datetime.fromisoformat(row["created_at"])
            inv = Invoice.model_validate_json(row["invoice_json"])
            if age >= ESCALATE_AFTER and not row["escalated_from"]:
                nxt = ("expires unpaid", EXPIRE_AFTER - age) if not DIRECTORY[row["role"]]["delegate"] else ("escalates", ESCALATE_AFTER - age)
            elif age < REMINDER_AFTER and not row["reminded_at"]:
                nxt = ("reminder", REMINDER_AFTER - age)
            elif age < ESCALATE_AFTER and not row["escalated_from"]:
                nxt = ("escalates to " + (DIRECTORY[row["role"]]["delegate"] or "nobody (expires)"), ESCALATE_AFTER - age)
            else:
                nxt = ("expires unpaid", EXPIRE_AFTER - age)
            out.append({"id": row["id"], "role": row["role"], "invoice": inv.invoice_number, "vendor": inv.vendor,
                        "amount": inv.total, "age_hours": age.total_seconds() / 3600, "reasons": json.loads(row["reasons"]),
                        "next": nxt[0], "next_in_hours": max(0.0, nxt[1].total_seconds() / 3600),
                        "escalated_from": row["escalated_from"]})
        return out

    def inspect(self, token: str, now: datetime | None = None) -> dict:
        """Everything the approval page shows for a valid, unexpired link. Raises TokenError otherwise."""
        now = now or utcnow()
        data = read_token(self._secret, token, now)
        row = self._row(data["r"])
        if row is None:
            raise TokenError("unknown request")
        return {"id": row["id"], "action": data["a"], "status": row["status"], "role": row["role"], "reasons": json.loads(row["reasons"]),
                "invoice": Invoice.model_validate_json(row["invoice_json"]), "outcome": Outcome.model_validate_json(row["outcome_json"]),
                "approvers": sorted(authorized(row["role"], row["escalated_from"])), "primary": DIRECTORY[row["role"]]["email"], "tokens": self.tokens_for(row["id"], now),
                "decided_by": row["decided_by"], "comment": row["comment"]}

    def tokens_for(self, rid: int, now: datetime | None = None) -> dict[str, str]:
        row = self._row(rid)
        return {a: self._token(rid, a, row["ev_hash"], now or utcnow()) for a in ("approve", "reject")}

    def events(self, rid: int) -> list[tuple[str, str, str]]:
        return [tuple(r) for r in self._fetch("SELECT at, event, detail FROM approval_events WHERE request_id=? ORDER BY id", (rid,))]
