"""Every processed invoice's full outcome (findings, agent trace, explanation, payment) kept in the same SQLite file as the ledger, so the web
interface and any later tool can show what happened without re-running anything."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Outcome

_SCHEMA = """CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, bill_key TEXT, invoice_number TEXT, vendor TEXT, source TEXT, outcome_json TEXT,
    at TEXT DEFAULT CURRENT_TIMESTAMP)"""


class OutcomeStore:
    def __init__(self, db_path: str | Path, reset: bool = False):
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(_SCHEMA)
            if reset:
                self._db.execute("DELETE FROM outcomes")
                self._db.execute("DELETE FROM sqlite_sequence WHERE name='outcomes'")
            self._db.commit()

    def save(self, outcome: Outcome, bill_key: str | None = None) -> None:
        inv = outcome.invoice
        with self._lock:
            self._db.execute("INSERT INTO outcomes (bill_key, invoice_number, vendor, source, outcome_json) VALUES (?,?,?,?,?)",
                             (bill_key, inv.invoice_number if inv else None, inv.vendor if inv else None, outcome.source_path,
                              outcome.model_dump_json()))
            self._db.commit()

    def all(self) -> list[tuple[int, Outcome]]:
        with self._lock:
            rows = self._db.execute("SELECT id, outcome_json FROM outcomes ORDER BY id").fetchall()
        return [(i, Outcome.model_validate_json(j)) for i, j in rows]

    def get(self, row_id: int) -> Outcome | None:
        with self._lock:
            row = self._db.execute("SELECT outcome_json FROM outcomes WHERE id=?", (row_id,)).fetchone()
        return Outcome.model_validate_json(row[0]) if row else None
