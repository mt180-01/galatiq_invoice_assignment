"""Payment service: approved invoice -> scheduled instruction -> bank -> reconciled.

  * The instruction is written to the ledger *before* the bank is called, keyed by the bill (the
    idempotency key). A retry, a rerun or a crash can therefore never pay twice: before re-submitting,
    we ask the bank what it already holds for that key.
  * Money is released on the due date (minus the rail's lead time), not on approval, unless an
    early-payment discount is still open. That is cash Acme keeps for free.
  * Payees must have a verified bank account on file. Changing one needs a second person and holds that
    vendor's payments until then (the standard defence against "we've changed our bank details" fraud).
  * Bank calls run outside the ledger lock, so many submissions overlap; the daily outbound limit is
    enforced inside the lock so concurrent submissions cannot exceed it together.
Every number in POLICY is an assumption to replace with Acme's treasury policy and the bank's tariff.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

from ..erp import ErpAdapter
from ..erp.mock_netsuite import SEED_VENDORS, norm
from ..models import Invoice
from .bank import BankAdapter, BankError, BankRejected, SETTLE_DAYS
from .terms import parse_terms

WIRE_MIN = Decimal("25000")            # at or above this, a wire (same-day, irrevocable-ish) instead of ACH
DAILY_LIMIT = Decimal("250000")        # outbound per day; the rest waits for tomorrow
RAIL_COST = {"ach": Decimal("0.30"), "wire": Decimal("25.00")}  # assumed tariff per payment
TAKE_DISCOUNTS = True
MAX_ATTEMPTS = 3                       # per release attempt
GIVE_UP_AFTER = 3                      # release attempts (each retries MAX_ATTEMPTS times) before a person is asked to look

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    bill_key TEXT PRIMARY KEY, vendor TEXT, account TEXT, amount TEXT, discount TEXT DEFAULT '0', rail TEXT,
    release_on TEXT, state TEXT, bank_ref TEXT, attempts INTEGER DEFAULT 0, last_error TEXT, note TEXT,
    submitted_on TEXT, settled_on TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS vendor_accounts (
    vendor TEXT PRIMARY KEY, account TEXT, status TEXT, set_by TEXT, verified_by TEXT);
"""
ERP_STATUS = {"submitted": "paid", "settled": "paid", "scheduled": "scheduled", "held": "payment_held",
              "failed": "payment_failed", "returned": "payment_returned", "submitting": "scheduled"}


@dataclass(frozen=True)
class Plan:
    amount: Decimal
    discount: Decimal
    rail: str
    release_on: date
    note: str


@dataclass
class PayResult:
    state: str
    ref: str | None
    amount: Decimal
    discount: Decimal
    rail: str
    release_on: str
    message: str

    @property
    def submitted(self) -> bool:
        return self.state in ("submitted", "settled")

    @property
    def erp_status(self) -> str:
        return ERP_STATUS[self.state]


@dataclass
class Reconciliation:
    settled: int = 0
    returned: int = 0
    pending: int = 0
    issues: list[str] = field(default_factory=list)


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


class PaymentService:
    def __init__(self, db_path: str | Path, erp: ErpAdapter, bank: BankAdapter, reset: bool = True,
                 today: Callable[[], date] = date.today, sleep: Callable[[float], None] = time.sleep,
                 daily_limit: Decimal = DAILY_LIMIT):
        self.erp, self.bank, self.today, self._sleep, self.daily_limit = erp, bank, today, sleep, daily_limit
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            if reset:
                self._db.execute("DELETE FROM payments")
                self._db.execute("DELETE FROM vendor_accounts")
            for name, _, _ in SEED_VENDORS:  # demo master data: every approved vendor starts verified
                self._db.execute("INSERT OR IGNORE INTO vendor_accounts VALUES (?,?,?,?,?)",
                                 (norm(name), "acct-" + hashlib.sha1(name.encode()).hexdigest()[:8], "verified", "seed", "seed"))
            self._db.commit()

    # ---- planning ---------------------------------------------------------------------------
    def plan(self, inv: Invoice, today: date) -> Plan:
        total = inv.total or Decimal(0)
        rail = "wire" if total >= WIRE_MIN else "ach"
        terms, dated = parse_terms(inv.payment_terms), _day(inv.date)
        if TAKE_DISCOUNTS and terms.discount_pct and dated and today <= dated + timedelta(days=terms.discount_days):
            discount = (total * terms.discount_pct / 100).quantize(Decimal("0.01"))
            return Plan(total - discount, discount, rail, today, f"early-payment discount {terms.discount_pct}% captured ({discount})")
        due = _day(inv.due_date) or (dated + timedelta(days=terms.net_days) if dated and terms.net_days is not None else None)
        if due is None:
            return Plan(total, Decimal(0), rail, today, "no due date or terms: paid now")
        release = max(today, due - timedelta(days=SETTLE_DAYS[rail]))
        note = "overdue: paid now" if due < today else f"released {SETTLE_DAYS[rail]}d before due {due} ({rail.upper()} lead time)"
        return Plan(total, Decimal(0), rail, release, note)

    # ---- pay (called by the pipeline and by the approval service) ---------------------------------
    def pay(self, inv: Invoice, key: str) -> PayResult:
        if inv.currency != "USD":
            raise ValueError(f"refusing to pay in {inv.currency}")
        today = self.today()
        vendor = inv.vendor or ""
        canonical = self._canonical(vendor)
        with self._lock:
            if self._row(key):
                return self._result(self._row(key))  # idempotent: same bill, same payment
            acct = self._db.execute("SELECT account, status FROM vendor_accounts WHERE vendor=?", (norm(canonical),)).fetchone()
            plan = self.plan(inv, today)
            held = None if acct and acct["status"] == "verified" else "no verified bank account on file for this vendor"
            self._db.execute(
                "INSERT INTO payments (bill_key, vendor, account, amount, discount, rail, release_on, state, note, last_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, canonical, acct["account"] if acct else None, str(plan.amount), str(plan.discount), plan.rail,
                 plan.release_on.isoformat(), "held" if held else "scheduled", plan.note, held))
            self._db.commit()
        if not held and plan.release_on <= today:
            self._submit(key)
        return self._result(self._row(key))

    # ---- submission ---------------------------------------------------------------------------
    def _submit(self, key: str) -> None:
        today = self.today()
        with self._lock:
            row = self._row(key)
            if not row or row["state"] != "scheduled":
                return  # someone else has it, or it is held/cancelled
            committed = sum(Decimal(r[0]) for r in self._db.execute(
                "SELECT amount FROM payments WHERE submitted_on=? AND state IN ('submitting','submitted','settled','returned')",
                (today.isoformat(),)))
            if committed + Decimal(row["amount"]) > self.daily_limit:
                self._db.execute("UPDATE payments SET release_on=?, note=? WHERE bill_key=?",
                                 ((today + timedelta(days=1)).isoformat(), "daily outbound limit reached: deferred a day", key))
                self._db.commit()
                return
            self._db.execute("UPDATE payments SET state='submitting', submitted_on=?, attempts=attempts+1 WHERE bill_key=?",
                             (today.isoformat(), key))
            self._db.commit()
        receipt, error, permanent = None, None, False
        try:
            receipt = self.bank.lookup(key)  # crashed before? the bank already knows
            for attempt in range(MAX_ATTEMPTS):
                if receipt:
                    break
                try:
                    receipt = self.bank.submit(key, row["vendor"], row["account"], Decimal(row["amount"]), "USD", row["rail"], today)
                except BankError as exc:
                    error = str(exc)
                    if attempt < MAX_ATTEMPTS - 1:
                        self._sleep(0.2 * 2 ** attempt)
        except BankRejected as exc:
            error, permanent = str(exc), True
        with self._lock:
            if receipt:
                self._db.execute("UPDATE payments SET state='submitted', bank_ref=?, last_error=NULL WHERE bill_key=?", (receipt.ref, key))
            else:
                state = "failed" if permanent or self._row(key)["attempts"] >= GIVE_UP_AFTER else "scheduled"
                self._db.execute("UPDATE payments SET state=?, last_error=? WHERE bill_key=?", (state, error, key))
            self._db.commit()

    def release_due(self, concurrency: int = 8) -> list[PayResult]:
        """Submit everything whose release date has come. Safe to run repeatedly (cron, or after an outage)."""
        today = self.today()
        with self._lock:
            keys = [r[0] for r in self._db.execute(
                "SELECT bill_key FROM payments WHERE state='scheduled' AND release_on<=? ORDER BY release_on, CAST(amount AS REAL) DESC",
                (today.isoformat(),))]
        with ThreadPoolExecutor(concurrency) as pool:
            list(pool.map(self._submit, keys))
        out = []
        for key in keys:
            res = self._result(self._row(key))
            if res.submitted:
                row = self._row(key)
                self.erp.settle_bill(key, "paid", f"payment released: {res.message}", (row["vendor"], Decimal(row["amount"]), res.ref))
            out.append(res)
        return out

    def recover(self) -> list[str]:
        """After a crash: payments left 'submitting' are resolved by asking the bank, never by guessing."""
        notes = []
        with self._lock:
            stuck = [r["bill_key"] for r in self._db.execute("SELECT bill_key FROM payments WHERE state='submitting'")]
        for key in stuck:
            receipt = self.bank.lookup(key)
            row = self._row(key)
            with self._lock:
                if receipt:
                    self._db.execute("UPDATE payments SET state='submitted', bank_ref=? WHERE bill_key=?", (receipt.ref, key))
                else:
                    self._db.execute("UPDATE payments SET state='scheduled' WHERE bill_key=?", (key,))
                self._db.commit()
            if receipt:
                self.erp.settle_bill(key, "paid", f"payment recovered after interruption: bank holds {receipt.ref}",
                                     (row["vendor"], Decimal(row["amount"]), receipt.ref))
            notes.append(f"{key}: {'bank has it (' + receipt.ref + ')' if receipt else 'bank has nothing; will resubmit'}")
        return notes

    # ---- reconciliation -----------------------------------------------------------------------
    def reconcile(self) -> Reconciliation:
        today, rec = self.today(), Reconciliation()
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM payments WHERE state IN ('submitted','settled','returned')")]
        ours = {r["bill_key"]: r for r in rows}
        for row in rows:
            if row["state"] != "submitted":
                continue
            state = self.bank.status(row["bank_ref"], today)
            if state == "pending":
                rec.pending += 1
                continue
            with self._lock:
                self._db.execute("UPDATE payments SET state=?, settled_on=? WHERE bill_key=?",
                                 ("settled" if state == "settled" else "returned", today.isoformat(), row["bill_key"]))
                self._db.commit()
            if state == "settled":
                rec.settled += 1
            else:
                rec.returned += 1
                self.erp.settle_bill(row["bill_key"], "payment_returned", f"bank returned {row['bank_ref']}: needs a person")
                rec.issues.append(f"RETURNED {row['bank_ref']} {row['vendor']} {row['amount']}")
        for bp in self.bank.payments():
            mine = ours.get(bp["idem"])
            if mine is None:
                rec.issues.append(f"bank holds {bp['ref']} ({bp['vendor']} {bp['amount']}) that our ledger has no record of")
            elif Decimal(bp["amount"]) != Decimal(mine["amount"]):
                rec.issues.append(f"amount mismatch on {bp['ref']}: bank {bp['amount']} vs ledger {mine['amount']}")
        return rec

    # ---- vendor bank accounts (dual control) -------------------------------------------------
    def set_account(self, vendor: str, account: str, by: str) -> str:
        name = self._canonical(vendor)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO vendor_accounts VALUES (?,?,?,?,NULL)", (norm(name), account, "pending_verification", by))
            held = self._db.execute("UPDATE payments SET state='held', last_error=? WHERE vendor=? AND state='scheduled'",
                                    ("bank account changed; awaiting verification callback", name)).rowcount
            self._db.commit()
        return f"{name}: new account recorded, NOT usable until a second person verifies it; {held} scheduled payment(s) held"

    def verify_account(self, vendor: str, by: str) -> str:
        name = self._canonical(vendor)
        with self._lock:
            row = self._db.execute("SELECT * FROM vendor_accounts WHERE vendor=?", (norm(name),)).fetchone()
            if not row or row["status"] != "pending_verification":
                return f"{name}: nothing to verify"
            if row["set_by"] == by:
                return f"refused: {by} entered this account and cannot also verify it"
            self._db.execute("UPDATE vendor_accounts SET status='verified', verified_by=? WHERE vendor=?", (by, norm(name)))
            released = self._db.execute("UPDATE payments SET state='scheduled', account=?, last_error=NULL WHERE vendor=? AND state='held'",
                                        (row["account"], name)).rowcount
            self._db.commit()
        return f"{name}: account verified by {by}; {released} held payment(s) released to the schedule"

    def _canonical(self, vendor: str) -> str:
        record = self.erp.lookup_vendor(vendor)
        return record.name if record else vendor

    # ---- reads --------------------------------------------------------------------------------
    def _row(self, key: str):
        with self._lock:
            return self._db.execute("SELECT * FROM payments WHERE bill_key=?", (key,)).fetchone()

    def _result(self, r) -> PayResult:
        state = r["state"]
        msg = {"submitted": f"{r['rail'].upper()} submitted ({r['bank_ref']}); {r['note']}",
               "settled": f"{r['rail'].upper()} settled ({r['bank_ref']})",
               "returned": f"returned by bank ({r['bank_ref']})",
               "scheduled": f"scheduled for {r['release_on']} via {r['rail'].upper()}; {r['last_error'] or r['note']}",
               "submitting": "submission in progress",
               "held": f"held: {r['last_error']}",
               "failed": f"bank did not accept it: {r['last_error']}"}[state]
        return PayResult(state, r["bank_ref"], Decimal(r["amount"]), Decimal(r["discount"]), r["rail"], r["release_on"], msg)

    def rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM payments ORDER BY release_on, bill_key"))

    def accounts(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM vendor_accounts ORDER BY vendor"))
