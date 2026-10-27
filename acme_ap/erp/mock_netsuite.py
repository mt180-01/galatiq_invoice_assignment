"""SQLite stand-in for the ERP. Keeps the README's `inventory(item, stock)` table and extends it."""
from __future__ import annotations

import difflib
import re
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

from .base import ItemRecord, VendorRecord

SEED_ITEMS = [("WidgetA", 15, "250.00"), ("WidgetB", 10, "500.00"), ("GadgetX", 5, "750.00"), ("FakeItem", 0, "0.00")]
SEED_VENDORS = [
    ("Widgets Inc.", "approved", ""), ("Gadgets Co.", "approved", ""), ("Precision Parts Ltd.", "approved", ""),
    ("Global Supply Chain Partners", "approved", ""), ("Acme Industrial Supplies", "approved", ""),
    ("Consolidated Materials Group", "approved", ""), ("Summit Manufacturing Co.", "approved", ""),
    ("MegaWidgets Corp", "approved", ""), ("QuickShip Distributers", "approved", "FastShip Ltd."),
    ("Atlas Industrial Supply", "approved", ""), ("TechParts International", "approved", ""),
    ("Reliable Components Inc.", "approved", ""),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER, unit_price TEXT DEFAULT '0');
CREATE TABLE IF NOT EXISTS vendors (name TEXT PRIMARY KEY, status TEXT, aliases TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS vendor_bills (
    bill_key TEXT PRIMARY KEY, source TEXT, vendor TEXT, total TEXT, status TEXT DEFAULT 'received',
    reasoning TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS vendor_payments (
    bill_key TEXT PRIMARY KEY, vendor TEXT, amount TEXT, reference TEXT, paid_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bill_key TEXT, stage TEXT, detail TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class MockNetSuite:
    """Thread-safe: one connection per thread (WAL mode), writes serialised by a lock."""

    def __init__(self, path: str | Path = "inventory.db", keep_ledger: bool = False):
        self.path = str(path)
        self._local = threading.local()
        self._write = threading.Lock()
        with self._write:
            c = self._conn()
            c.executescript(_SCHEMA)
            if not keep_ledger:
                c.executescript("DELETE FROM vendor_bills; DELETE FROM vendor_payments; DELETE FROM audit_log;")
            c.execute("DELETE FROM inventory")
            c.execute("DELETE FROM vendors")
            c.executemany("INSERT INTO inventory VALUES (?,?,?)", SEED_ITEMS)
            c.executemany("INSERT INTO vendors VALUES (?,?,?)", SEED_VENDORS)
            c.commit()
        self._items = {norm(n): n for n, _, _ in SEED_ITEMS}

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # ---- reads -------------------------------------------------------------------------
    def lookup_item(self, name: str) -> ItemRecord | None:
        key = norm(name)
        fuzzy = False
        if key not in self._items:
            close = difflib.get_close_matches(key, list(self._items), n=1, cutoff=0.9)
            if not close:
                return None
            key, fuzzy = close[0], True
        row = self._conn().execute("SELECT item, stock, unit_price FROM inventory WHERE item=?", (self._items[key],)).fetchone()
        return ItemRecord(row[0], row[1], Decimal(row[2]), fuzzy) if row else None

    def lookup_vendor(self, name: str) -> VendorRecord | None:
        want = norm(name)
        for vname, status, aliases in self._conn().execute("SELECT name, status, aliases FROM vendors"):
            names = [vname] + [a for a in aliases.split(";") if a]
            if want in {norm(n) for n in names}:
                return VendorRecord(vname, status)
        return None

    # ---- writes ------------------------------------------------------------------------
    def claim_bill(self, key: str, source: str, vendor: str | None, total: Decimal | None) -> str | None:
        with self._write:
            c = self._conn()
            cur = c.execute("INSERT OR IGNORE INTO vendor_bills (bill_key, source, vendor, total) VALUES (?,?,?,?)",
                            (key, source, vendor, str(total) if total is not None else None))
            c.commit()
            if cur.rowcount:
                return None
            return c.execute("SELECT source FROM vendor_bills WHERE bill_key=?", (key,)).fetchone()[0]

    def settle_bill(self, key, status, reasoning, payment=None):
        with self._write:
            c = self._conn()
            c.execute("UPDATE vendor_bills SET status=?, reasoning=? WHERE bill_key=?", (status, reasoning, key))
            if payment:
                vendor, amount, ref = payment
                c.execute("INSERT INTO vendor_payments (bill_key, vendor, amount, reference) VALUES (?,?,?,?)",
                          (key, vendor, str(amount), ref))
            c.execute("INSERT INTO audit_log (bill_key, stage, detail) VALUES (?,?,?)", (key, "decision", f"{status}: {reasoning}"))
            c.commit()
