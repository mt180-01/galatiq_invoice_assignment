"""Adapter contract, shaped like the NetSuite records an AP automation touches
(Item, Vendor, Vendor Bill, Vendor Payment). A production `NetSuiteAdapter` would implement the
same methods over SuiteTalk REST (OAuth 2.0 M2M) and SuiteQL; nothing upstream would change."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class ItemRecord:
    name: str
    stock: int
    unit_price: Decimal
    fuzzy: bool = False  # matched by approximate name, not exactly


@dataclass(frozen=True)
class VendorRecord:
    name: str
    status: str  # approved | blocked


class ErpAdapter(Protocol):
    def lookup_item(self, name: str) -> ItemRecord | None: ...
    def lookup_vendor(self, name: str) -> VendorRecord | None: ...
    def claim_bill(self, key: str, source: str, vendor: str | None, total: Decimal | None) -> str | None:
        """Atomically register a vendor bill. Returns None if new, else the source that claimed it first."""
    def bill_status(self, key: str) -> str | None: ...
    def search_items(self, query: str, limit: int = 3) -> list[dict]:
        """Catalogue items with names resembling `query`: [{'name', 'stock', 'unit_price', 'similarity'}], best first."""
    def search_vendors(self, query: str, limit: int = 3) -> list[dict]:
        """Approved vendors with names resembling `query`: [{'name', 'status', 'similarity'}], best first."""
    def vendor_history(self, vendor: str) -> dict:
        """{'bills': n, 'avg_total': str, 'max_total': str} over this vendor's paid or scheduled bills."""
    def settle_bill(self, key: str, status: str, reasoning: str,
                    payment: tuple[str, Decimal, str] | None = None) -> None:
        """One transaction: final bill status + audit entry (+ vendor payment (vendor, amount, ref) if paid)."""
