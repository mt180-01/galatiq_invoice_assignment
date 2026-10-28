"""Bank boundary. The payment service only talks to `BankAdapter`; `MockBank` stands in for a real
bank/payments API (ACH + wire, idempotency keys, asynchronous settlement, returns).

What a real adapter must provide: submit with an idempotency key (a repeated key returns the original
payment instead of paying twice), lookup by that key (so a crashed caller can find out what happened),
and status by reference. Credentials, signing and the bank's own limits live behind this class.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .calendar import add_business_days
from .mock import mock_payment

SETTLE_DAYS = {"ach": 2, "wire": 0}  # business days


class BankError(Exception):
    """Transient (timeout, 5xx, rate limit): safe to retry with the same idempotency key."""


class BankRejected(Exception):
    """Permanent (closed account, bad routing, sanctions hit): retrying cannot help."""


@dataclass(frozen=True)
class Receipt:
    ref: str
    rail: str
    submitted_on: str
    settles_on: str


class BankAdapter(Protocol):
    def submit(self, idem: str, vendor: str, account: str, amount: Decimal, currency: str, rail: str, on: date) -> Receipt: ...
    def lookup(self, idem: str) -> Receipt | None: ...
    def status(self, ref: str, on: date) -> str:
        """pending | settled | returned"""
    def payments(self) -> list[dict]:
        """Everything the bank holds, for reconciliation against our ledger."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_payments (
    idem TEXT PRIMARY KEY, ref TEXT, vendor TEXT, account TEXT, amount TEXT, currency TEXT, rail TEXT,
    submitted_on TEXT, settles_on TEXT);
"""


class MockBank:
    """Its own SQLite file, so "the bank" keeps its state when our process dies."""

    def __init__(self, path: str | Path, latency: float = 0.0, fail_first: int = 0,
                 reject_accounts: set[str] | None = None, return_vendors: set[str] | None = None, reset: bool = False):
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._lock = threading.Lock()
        self.latency, self._fail = latency, fail_first
        self.reject_accounts, self.return_vendors = reject_accounts or set(), return_vendors or set()
        self.calls = 0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()
        if reset:
            self.reset()

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM bank_payments")
            self._db.commit()

    def submit(self, idem, vendor, account, amount, currency, rail, on):
        if self.latency:
            time.sleep(self.latency)
        with self._lock:
            self.calls += 1
            if self._fail > 0:
                self._fail -= 1
                raise BankError("503 service unavailable")
            if existing := self._get(idem):
                return existing  # idempotent replay: same payment, no second transfer
            if account in self.reject_accounts:
                raise BankRejected("account closed")
            if mock_payment(vendor, amount).get("status") != "success":
                raise BankRejected("bank declined the payment")
            ref = f"{rail.upper()}-{hashlib.sha1(idem.encode()).hexdigest()[:10].upper()}"
            settles = add_business_days(on, SETTLE_DAYS[rail])
            self._db.execute("INSERT INTO bank_payments VALUES (?,?,?,?,?,?,?,?,?)",
                             (idem, ref, vendor, account, str(amount), currency, rail, on.isoformat(), settles.isoformat()))
            self._db.commit()
            return Receipt(ref, rail, on.isoformat(), settles.isoformat())

    def lookup(self, idem):
        with self._lock:
            return self._get(idem)

    def _get(self, idem) -> Receipt | None:
        r = self._db.execute("SELECT ref, rail, submitted_on, settles_on FROM bank_payments WHERE idem=?", (idem,)).fetchone()
        return Receipt(*r) if r else None

    def status(self, ref, on):
        with self._lock:
            r = self._db.execute("SELECT vendor, settles_on FROM bank_payments WHERE ref=?", (ref,)).fetchone()
        if not r:
            raise BankRejected(f"unknown payment {ref}")
        if date.fromisoformat(r[1]) > on:
            return "pending"
        return "returned" if r[0] in self.return_vendors else "settled"

    def payments(self):
        with self._lock:
            cur = self._db.execute("SELECT idem, ref, vendor, amount, rail FROM bank_payments")
            return [dict(zip(("idem", "ref", "vendor", "amount", "rail"), r)) for r in cur]
