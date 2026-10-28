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
    def claim_bill(self, key: str, source: str, vendor: str | None, total: Decimal | None, content: str | None = None) -> str | None:
        """Atomically register a vendor bill. Returns None if new, else the source that claimed it first. If the earlier attempt ended without
        payment (rejected, blocked, expired) and this copy's `content` differs, the bill is claimed afresh and None is returned: a corrected
        resubmission is not a duplicate. An identical copy of a rejected bill is one, since processing it again can only repeat the rejection."""
    def similar_bills(self, number: str, total: Decimal | None, vendor: str, exclude_key: str) -> list[dict]:
        """Live bills with the same invoice number and total from a similarly named vendor but a different key: possible duplicates."""
    def bill_status(self, key: str) -> str | None: ...
    def reserve_stock(self, key: str, lines: list[tuple[str, int]]) -> list[str]:
        """Under the `reserve` stock policy: atomically set aside what an approved bill will consume. Returns shortfall messages (and reserves
        nothing) if any line cannot be covered; an empty list means reserved. Under the default `cap` policy this does nothing and returns []."""
    def release_stock(self, key: str) -> None:
        """Give back a bill's reservation (its payment failed for good)."""
    def vendor_names(self) -> list[str]:
        """Every name the vendor master knows a vendor by (legal names and aliases): the readers use it to find a vendor that no label announces."""
    def search_items(self, query: str, limit: int = 3) -> list[dict]:
        """Catalogue items with names resembling `query`: [{'name', 'stock', 'unit_price', 'similarity'}], best first."""
    def search_vendors(self, query: str, limit: int = 3) -> list[dict]:
        """Approved vendors with names resembling `query`: [{'name', 'status', 'similarity'}], best first."""
    def vendor_history(self, vendor: str) -> dict:
        """{'bills': n, 'avg_total': str, 'max_total': str} over this vendor's paid or scheduled bills."""
    def settle_bill(self, key: str, status: str, reasoning: str,
                    payment: tuple[str, Decimal, str] | None = None) -> None:
        """One transaction: final bill status + audit entry (+ vendor payment (vendor, amount, ref) if paid)."""
