"""SQLite stand-in for the ERP. Keeps the README's `inventory(item, stock)` table and extends it."""
from __future__ import annotations

import difflib
import os
import re
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

from .base import ItemRecord, VendorRecord

ABANDONED_AFTER_MINUTES = 15  # a claim that is still 'received' after this long belongs to a crashed run
STOCK_POLICIES = ("cap", "reserve", "decrement")
FAILED = ('reject', 'rejected', 'blocked', 'expired')  # bill statuses that mean nothing was paid and the attempt is over
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
    reasoning TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, number TEXT, content TEXT, fingerprint TEXT);
CREATE TABLE IF NOT EXISTS vendor_payments (
    bill_key TEXT PRIMARY KEY, vendor TEXT, amount TEXT, reference TEXT, paid_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stock_reservations (bill_key TEXT, item TEXT, qty INTEGER, PRIMARY KEY (bill_key, item));
CREATE TABLE IF NOT EXISTS stock_movements (bill_key TEXT, item TEXT, qty INTEGER, PRIMARY KEY (bill_key, item));
CREATE TABLE IF NOT EXISTS erp_settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bill_key TEXT, stage TEXT, detail TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


class MockNetSuite:
    """Thread-safe: one connection per thread (WAL mode), writes serialised by a lock."""

    def __init__(self, path: str | Path = "inventory.db", keep_ledger: bool = True, stock_policy: str | None = None):
        """stock_policy: 'cap' (default) checks each invoice against stock on hand; 'reserve' also counts what earlier approved invoices have
        already claimed. The choice is stored with the database, so every later tool (approve.py, pay.py, the web interface) follows it."""
        self.path = str(path)
        # keep_ledger=True is the default for every caller: opening the database must never forget what was paid. Only an explicit "do not keep" (`--reset`,
        # a scratch database made for one run) starts from an empty ledger.
        self.ledger_reset = not keep_ledger
        self._local = threading.local()
        self._write = threading.Lock()
        with self._write:
            c = self._conn()
            c.executescript(_SCHEMA)
            if "number" not in {r[1] for r in c.execute("PRAGMA table_info(vendor_bills)")}:  # a ledger from before the invoice-number column
                c.execute("ALTER TABLE vendor_bills ADD COLUMN number TEXT")
                c.execute("UPDATE vendor_bills SET number = substr(bill_key, instr(bill_key, '|') + 1, instr(substr(bill_key, instr(bill_key, '|') + 1), '|') - 1) WHERE bill_key NOT LIKE 'anon|%'")
            if "content" not in {r[1] for r in c.execute("PRAGMA table_info(vendor_bills)")}:
                c.execute("ALTER TABLE vendor_bills ADD COLUMN content TEXT")
            if "fingerprint" not in {r[1] for r in c.execute("PRAGMA table_info(vendor_bills)")}:
                c.execute("ALTER TABLE vendor_bills ADD COLUMN fingerprint TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS vendor_bills_number ON vendor_bills(number)")  # near-duplicate lookups must not scan the ledger
            if not keep_ledger:
                c.executescript("DELETE FROM vendor_bills; DELETE FROM vendor_payments; DELETE FROM audit_log; DELETE FROM stock_reservations; DELETE FROM stock_movements;")
            if stock_policy:
                if stock_policy not in STOCK_POLICIES:
                    raise ValueError("stock_policy must be one of " + ", ".join(STOCK_POLICIES))
                c.execute("INSERT OR REPLACE INTO erp_settings VALUES ('stock_policy', ?)", (stock_policy,))
            row = c.execute("SELECT v FROM erp_settings WHERE k='stock_policy'").fetchone()
            self.stock_policy = row[0] if row else "cap"
            if not keep_ledger:
                c.execute("DELETE FROM inventory")  # a fresh ledger starts from the opening stock...
            c.executemany("INSERT OR IGNORE INTO inventory VALUES (?,?,?)", SEED_ITEMS)  # ...otherwise stock is what the last run left (a new catalogue item is added, none is overwritten)
            c.execute("DELETE FROM vendors")
            c.executemany("INSERT INTO vendors VALUES (?,?,?)", SEED_VENDORS)
            c.commit()
        self._items = {norm(n): n for n, _, _ in SEED_ITEMS}
        # The vendor master is read on every invoice and nothing in this mock changes it, so it is loaded once.
        self._vendors = {norm(n): VendorRecord(vname, status)
                         for vname, status, aliases in SEED_VENDORS for n in [vname, *[a for a in aliases.split(";") if a]]}

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
        if not row:
            return None
        return ItemRecord(row[0], row[1] - self._reserved(row[0]), Decimal(row[2]), fuzzy)  # what is still unclaimed, if stock is being reserved

    @property
    def tracks_stock(self) -> bool:
        """True when approved invoices claim stock (`reserve` and `decrement`); under the default `cap` each invoice is checked on its own."""
        return self.stock_policy != "cap"

    def _reserved(self, item: str, exclude_key: str | None = None) -> int:
        if not self.tracks_stock:
            return 0
        return self._conn().execute("SELECT COALESCE(SUM(qty), 0) FROM stock_reservations WHERE item=? AND bill_key IS NOT ?", (item, exclude_key)).fetchone()[0]

    def reserve_stock(self, key: str, lines: list[tuple[str, int]]) -> list[str]:
        if not self.tracks_stock:
            return []
        wanted: dict[str, int] = {}
        for name, qty in lines:
            rec = self.lookup_item(name)
            if rec is not None and qty > 0:
                wanted[rec.name] = wanted.get(rec.name, 0) + qty
        with self._write:  # one writer at a time: two invoices racing for the last units cannot both get them
            c = self._conn()
            short = []
            for item, qty in wanted.items():
                on_hand = c.execute("SELECT stock FROM inventory WHERE item=?", (item,)).fetchone()[0]
                claimed = self._reserved(item, exclude_key=key)
                if qty > on_hand - claimed:
                    short.append(f"{item}: {qty} billed but only {max(on_hand - claimed, 0)} left unclaimed ({claimed} already reserved by earlier approved invoices)")
            if short:
                return short
            c.execute("DELETE FROM stock_reservations WHERE bill_key=?", (key,))
            c.executemany("INSERT INTO stock_reservations VALUES (?,?,?)", [(key, item, qty) for item, qty in wanted.items()])
            c.commit()
            return []

    def release_stock(self, key: str) -> None:
        with self._write:
            self._conn().execute("DELETE FROM stock_reservations WHERE bill_key=?", (key,))
            self._conn().commit()

    def _consume(self, c: sqlite3.Connection, key: str) -> None:
        """`decrement` policy, when the bill is paid: the reserved units leave the shelf for good. Safe to call twice (the reservation is gone after the first)."""
        rows = c.execute("SELECT item, qty FROM stock_reservations WHERE bill_key=?", (key,)).fetchall()
        for item, qty in rows:
            c.execute("UPDATE inventory SET stock = stock - ? WHERE item=?", (qty, item))
            c.execute("INSERT OR REPLACE INTO stock_movements VALUES (?,?,?)", (key, item, qty))
            c.execute("INSERT INTO audit_log (bill_key, stage, detail) VALUES (?,?,?)", (key, "stock", f"{item}: -{qty} on payment"))
        c.execute("DELETE FROM stock_reservations WHERE bill_key=?", (key,))

    def _restore(self, c: sqlite3.Connection, key: str) -> None:
        """The bank returned the payment, so the goods are not accounted for: put back what `_consume` took."""
        for item, qty in c.execute("SELECT item, qty FROM stock_movements WHERE bill_key=?", (key,)).fetchall():
            c.execute("UPDATE inventory SET stock = stock + ? WHERE item=?", (qty, item))
            c.execute("INSERT INTO audit_log (bill_key, stage, detail) VALUES (?,?,?)", (key, "stock", f"{item}: +{qty} payment returned"))
        c.execute("DELETE FROM stock_movements WHERE bill_key=?", (key,))

    def stock_levels(self) -> list[dict]:
        """On hand, still reserved by approved-but-unpaid bills, and available, for every catalogue item."""
        rows = self._conn().execute("SELECT item, stock FROM inventory ORDER BY item").fetchall()
        return [{"item": i, "on_hand": s, "reserved": self._reserved(i), "available": s - self._reserved(i)} for i, s in rows]

    def lookup_vendor(self, name: str) -> VendorRecord | None:
        return self._vendors.get(norm(name))

    # ---- writes ------------------------------------------------------------------------
    def claim_bill(self, key: str, source: str, vendor: str | None, total: Decimal | None, content: str | None = None, fingerprint: str | None = None) -> str | None:
        with self._write:
            c = self._conn()
            parts = key.split("|")
            number = parts[1] if len(parts) >= 3 and parts[0] != "anon" else None  # keys are vendor|number|revision; anything else has no number
            cur = c.execute("INSERT OR IGNORE INTO vendor_bills (bill_key, source, vendor, total, number, content, fingerprint) VALUES (?,?,?,?,?,?,?)",
                            (key, source, vendor, str(total) if total is not None else None, number, content, fingerprint))
            if cur.rowcount:
                c.commit()
                return None
            source_before, status, abandoned, content_before = c.execute(
                "SELECT source, status, created_at < datetime('now', ?), content FROM vendor_bills WHERE bill_key=?", (f"-{ABANDONED_AFTER_MINUTES} minutes", key)).fetchone()
            if status == "received" and abandoned:
                status = "abandoned"  # claimed long ago and never finished: the process that held it died, so the claim no longer protects anything
            identical_to_a_rejected_one = status in FAILED and content is not None and content == content_before
            if (status in FAILED or status == "abandoned") and not identical_to_a_rejected_one:
                # The earlier attempt never paid anything. Holding the number forever would make a corrected resubmission impossible, so the bill
                # is claimed again and judged on its own merits; the audit log keeps the history.
                c.execute("UPDATE vendor_bills SET source=?, vendor=?, total=?, status='received', reasoning=NULL, created_at=CURRENT_TIMESTAMP, content=?, fingerprint=? WHERE bill_key=?",
                          (source, vendor, str(total) if total is not None else None, content, fingerprint, key))
                c.execute("INSERT INTO audit_log (bill_key, stage, detail) VALUES (?,?,?)",
                          (key, "resubmitted", f"earlier attempt ({os.path.basename(source_before)}) ended {status}; reprocessing {os.path.basename(source)}"))
                c.commit()
                return None
            c.commit()
            return source_before

    def similar_bills(self, number: str, total: Decimal | None, vendor: str, exclude_key: str) -> list[dict]:
        if not number or total is None:
            return []
        out = []
        rows = self._conn().execute("SELECT bill_key, source, vendor, total, status FROM vendor_bills WHERE number = ? AND bill_key != ?", (number, exclude_key)).fetchall()
        for key, source, other, other_total, status in rows:
            if status in FAILED or other_total is None or Decimal(other_total) != total:
                continue
            score = difflib.SequenceMatcher(None, norm(vendor), norm(other or "")).ratio()
            if score >= 0.75:
                out.append({"key": key, "source": source, "vendor": other, "status": status, "similarity": round(score, 2)})
        return out

    def recent_bills(self, key: str, days: int = 7) -> list[dict]:
        if "|" not in key or key.startswith("anon|"):
            return []
        rows = self._conn().execute(
            "SELECT bill_key, total, fingerprint FROM vendor_bills WHERE bill_key LIKE ? AND bill_key != ? AND status NOT IN (?,?,?,?) AND created_at >= datetime('now', ?)",
            (key.split("|")[0] + "|%", key, *FAILED, f"-{days} days")).fetchall()
        return [{"key": k, "total": Decimal(t) if t is not None else None, "fingerprint": f} for k, t, f in rows]

    def vendor_names(self) -> list[str]:
        return [n for vname, _, aliases in SEED_VENDORS for n in [vname, *[a for a in aliases.split(";") if a]]]

    def search_items(self, query: str, limit: int = 3) -> list[dict]:
        want, out = norm(query), []
        for key, name in self._items.items():
            score = difflib.SequenceMatcher(None, want, key).ratio()
            if score >= 0.5:
                row = self._conn().execute("SELECT stock, unit_price FROM inventory WHERE item=?", (name,)).fetchone()
                out.append({"name": name, "stock": row[0], "unit_price": row[1], "similarity": round(score, 2)})
        return sorted(out, key=lambda r: -r["similarity"])[:limit]

    def search_vendors(self, query: str, limit: int = 3) -> list[dict]:
        want = norm(query)
        seen: dict[str, float] = {}
        out: list[dict] = []
        for key, rec in self._vendors.items():
            score = difflib.SequenceMatcher(None, want, key).ratio()
            if score >= 0.5 and seen.get(rec.name, 0) < score:
                seen[rec.name] = score
        for name, score in seen.items():
            out.append({"name": name, "status": self._vendors[norm(name)].status, "similarity": round(score, 2)})
        return sorted(out, key=lambda r: -r["similarity"])[:limit]

    def vendor_history(self, vendor: str) -> dict:
        rows = self._conn().execute("SELECT total FROM vendor_bills WHERE bill_key LIKE ? AND status IN ('paid','scheduled') AND total IS NOT NULL",
                                    (norm(vendor) + "|%",)).fetchall()
        totals = [Decimal(r[0]) for r in rows]
        if not totals:
            return {"bills": 0}
        return {"bills": len(totals), "avg_total": str((sum(totals, Decimal(0)) / len(totals)).quantize(Decimal("0.01"))), "max_total": str(max(totals))}

    def bill_status(self, key: str) -> str | None:
        row = self._conn().execute("SELECT status FROM vendor_bills WHERE bill_key=?", (key,)).fetchone()
        return row[0] if row else None

    def settle_bill(self, key, status, reasoning, payment=None):
        with self._write:
            c = self._conn()
            c.execute("UPDATE vendor_bills SET status=?, reasoning=? WHERE bill_key=?", (status, reasoning, key))
            if payment:
                vendor, amount, ref = payment
                c.execute("INSERT INTO vendor_payments (bill_key, vendor, amount, reference) VALUES (?,?,?,?)",
                          (key, vendor, str(amount), ref))
            c.execute("INSERT INTO audit_log (bill_key, stage, detail) VALUES (?,?,?)", (key, "decision", f"{status}: {reasoning}"))
            if self.stock_policy == "decrement":
                if status == "paid":
                    self._consume(c, key)
                elif status == "payment_returned":
                    self._restore(c, key)
            c.commit()
