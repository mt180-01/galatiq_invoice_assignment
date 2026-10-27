"""Generate many randomly worded and corrupted invoices from known truth, run them through the offline extractor + validation +
policy, and count what matters: how often an invoice would be APPROVED for payment with a value that differs from the truth.

  silent wrong : approved, but vendor / number / total / items differ from the truth (the dangerous one; must be 0)
  safe stop    : not approved (a person or the vendor is asked); costs time, not money
  exact        : approved and every value matches the truth

The generator and the extractor were written by the same person, so this is a stress test, not an independent accuracy figure.
    python scripts/stress_extraction.py [N] [seed]
"""
from __future__ import annotations

import random
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acme_ap.agents import approval, validation  # noqa: E402
from acme_ap.erp import MockNetSuite  # noqa: E402
from acme_ap.erp.mock_netsuite import SEED_VENDORS, norm  # noqa: E402
from acme_ap.ingestion import heuristic  # noqa: E402
from acme_ap.models import Decision  # noqa: E402

CATALOG = {"WidgetA": (Decimal("250.00"), 15), "WidgetB": (Decimal("500.00"), 10), "GadgetX": (Decimal("750.00"), 5)}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Truth:
    number: str
    vendor: str
    items: list[tuple[str, int, Decimal]]
    day: tuple[int, int, int]
    terms: str

    @property
    def subtotal(self) -> Decimal:
        return sum((p * q for _, q, p in self.items), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.subtotal


def money(rng: random.Random, v: Decimal, sym: bool = True, ocr: bool = False) -> str:
    text = f"{v:,.2f}"
    if ocr and rng.random() < 0.3:
        text = text.replace("0", "O", 1)
    return ("$" if sym else "") + text


def date_text(rng: random.Random, y: int, m: int, d: int) -> str:
    return rng.choice([f"{y}-{m:02d}-{d:02d}", f"{MONTHS[m - 1]} {d}, {y}", f"{m:02d}/{d:02d}/{y}", f"{d:02d}-{MONTHS[m - 1]}-{y}"])


def render(rng: random.Random, t: Truth) -> str:
    y, m, d = t.day
    style = rng.choice(["table", "typo", "email", "bullets", "alt"])
    due = date_text(rng, y, m, min(d + 5, 28))
    on = date_text(rng, y, m, d)
    ocr = rng.random() < 0.3
    name = lambda n: rng.choice([n, n, n.replace("Widget", "Widget ")]) if False else n  # noqa: E731 - item names stay literal
    if style == "table":
        rows = [f"{name(n):<14}{q:>4}   {money(rng, p)}   {money(rng, p * q, ocr=ocr)}" for n, q, p in t.items]
        return "\n".join(["INVOICE", "", f"Invoice Number: {t.number}", f"Vendor: {t.vendor}", f"Date: {on}", f"Due Date: {due}", "",
                          "Item  Qty  Unit Price  Amount", "-" * 40, *rows, "-" * 40, f"Subtotal: {money(rng, t.subtotal)}",
                          "Tax (0%): $0.00", f"Total: {money(rng, t.total, ocr=ocr)}", "", f"Payment Terms: {t.terms}"])
    if style == "alt":
        rows = [rng.choice(ALT_ITEM).format(q=q, n=name(n), p=money(rng, p)) for n, q, p in t.items]
        return "\n".join([f"Invoice {t.number}", f"Vendor: {t.vendor}", f"Date: {on}", f"Due Date: {due}", *rows, f"Balance Due: {money(rng, t.total)}", f"Terms: {t.terms}"])
    if style == "typo":
        rows = [f"{name(n)}   qty: {q}   unit price: {money(rng, p)}" for n, q, p in t.items]
        return "\n".join([f"INV {t.number}", f"Vndr: {t.vendor}", f"Dt: {on}", f"Due Dt: {due}", *rows, f"Amt: {money(rng, t.total, ocr=ocr)}", f"Pymnt Terms: {t.terms}"])
    if style == "email":
        rows = [f"- {name(n)}   x{q}   {money(rng, p)} each" for n, q, p in t.items]
        return "\n".join([f"From: {t.vendor}", "", f"Hi, invoice {t.number} below.", f"Date: {on}", f"Due: {due}", "", *rows, "",
                          f"Total: {money(rng, t.total)}", f"Terms: {t.terms}"])
    rows = [f"{name(n)} ({rng.choice(['rush order', 'bulk', 'standard'])})   {q}   {money(rng, p)}   {money(rng, p * q)}" for n, q, p in t.items]
    return "\n".join(["INVOICE", f"Invoice #: {t.number}", f"Vendor: {t.vendor}", f"Date: {on}", f"Due Date: {due}", *rows, f"Total: {money(rng, t.total)}",
                      f"Payment Terms: {t.terms}"])


DISTRACTORS = ["PO Number: 44123", "Ship to: 12 Main St, Suite 400", "Phone: 555-0100", "Page 1 of 1", "Thank you for your business!",
               "Note: replaces INV-6001 which was cancelled", "Previous balance: $1,200.00", "Delivery in 3 days", "Contact: Dana 2nd floor",
               "Reference: your order 77", "Total weight 45 kg", "Account manager: J. Smith (ext 204)", "Tax ID 12-3456789",
               "Ref invoice 9999 (credit memo)", "Invoice INV-6001 was cancelled", "See also invoice 31337", "Total: $400.00"]
ALT_ITEM = ["{q} x {n} @ {p}", "{n} - {q} units @ {p}", "{q}  {n}  at {p} each"]


def corrupt(rng: random.Random, text: str, t: Truth) -> str:
    """Realistic mess: stray lines anywhere, shouting, trailing spaces. Never changes what the invoice means."""
    lines = text.split("\n")
    for _ in range(rng.choice([0, 1, 2, 3])):
        lines.insert(rng.randint(0, len(lines)), rng.choice(DISTRACTORS))
    if rng.random() < 0.15:
        lines = [ln.upper() if ln[:1].isalpha() and not ln.strip().startswith("-") else ln for ln in lines]
    if rng.random() < 0.2:
        lines = [ln + "   " for ln in lines]
    return "\n".join(lines)


def make_truth(rng: random.Random, i: int) -> Truth:
    names = rng.sample(list(CATALOG), rng.randint(1, 3))
    items = [(n, rng.randint(1, min(4, CATALOG[n][1])), CATALOG[n][0]) for n in names]
    return Truth(f"INV-{7000 + i}", rng.choice([v[0] for v in SEED_VENDORS]), items, (2026, rng.randint(1, 9), rng.randint(1, 20)),
                 rng.choice(["Net 30", "Net 15", "Net 60"]))


def run(n: int = 300, seed: int = 1) -> tuple[Counter, list[str]]:
    rng = random.Random(seed)
    tally: Counter = Counter()
    bad: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        erp = MockNetSuite(Path(tmp) / "s.db")
        for i in range(n):
            truth = make_truth(rng, i)
            text = corrupt(rng, render(rng, truth), truth)
            inv = heuristic.extract(text)
            decision = approval.policy_decision(inv, validation.validate(inv, erp)).decision
            if decision is not Decision.APPROVE:
                tally["safe stop"] += 1
                continue
            same = (norm(inv.vendor or "") == norm(truth.vendor) and inv.invoice_number == truth.number and inv.total == truth.total
                    and sorted((norm(li.item), li.quantity) for li in inv.line_items) == sorted((norm(n_), q) for n_, q, _ in truth.items))
            tally["exact" if same else "silent wrong"] += 1
            if not same:
                bad.append(f"--- truth {truth}\n{text}\n--- got vendor={inv.vendor!r} no={inv.invoice_number!r} total={inv.total} items={[(l.item, l.quantity) for l in inv.line_items]}")
    return tally, bad


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    tally, bad = run(n, int(sys.argv[2]) if len(sys.argv) > 2 else 1)
    print(f"{n} generated invoices: " + ", ".join(f"{k} {v} ({v / n:.0%})" for k, v in sorted(tally.items())))
    for b in bad[:5]:
        print(b)
