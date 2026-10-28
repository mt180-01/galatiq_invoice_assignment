"""Build an extraction test set that was NOT written by the extractor's author.

My code decides the ground truth (vendor, number, dates, items, amounts). Grok writes the *rendering*: the layout, wording, typos and noise of an invoice
as it would look after text extraction, in one of twelve styles. So the layouts come from a model that has never seen the extractor's regexes. Each
rendering is checked for faithfulness (every fact present) and rejected if not. The corpus and the truth are committed (data/extraction_eval/); this script
needs a key only to rebuild it (about ten cents):

    python scripts/make_extraction_corpus.py [N=60]

It is LLM-authored, not a set of real vendor invoices: a different author, not a different world.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, Field  # noqa: E402

from acme_ap.erp.mock_netsuite import SEED_VENDORS  # noqa: E402
from acme_ap.llm import as_agent, make_llm, structured  # noqa: E402

OUT = ROOT / "data" / "extraction_eval"
STYLES = {
    "columns": "a plain business invoice with a header block and an item table whose columns are aligned with spaces",
    "email": "the body of a casual email from the vendor's accounts person; the items are mentioned in sentences, not a table",
    "cells": "text extracted from a PDF table where every cell ended up on its own line (each label and each value on separate lines)",
    "ocr": "OCR-damaged text: some digits swapped for look-alike letters (O for 0, l for 1, S for 5) in a few numbers, a few broken words, but still decodable by a careful reader",
    "terse": "a terse purchase-order style with abbreviations (qty, u/p, amt) and no label for the vendor, whose name appears only as a letterhead",
    "multiline": "items with multi-line descriptions, SKUs and notes, plus a discount remark in the notes (do not change the totals), a shipping line and a tax percentage line",
    "refs": "an invoice that also prints a PO number, an order number, a customer number and a delivery note number near the invoice number (all different from it)",
    "bullets": "a bullet list of items like '- 4 x WidgetA @ $250.00 each', then totals",
    "twocol": "a two-column layout (bill from | bill to) at the top, an invoice details block, then an item table with line totals",
    "thread": "a forwarded email thread where the invoice details sit inside quoted replies with '>' prefixes and some chatter around them",
    "caps": "ALL CAPS, minimal punctuation, labels like 'INV NO' and 'AMT DUE', dates written as '12 MAR 2026'",
    "prose": "a short formal letter that states the invoice in sentences: number, date, what was supplied, the amount due and the due date",
}
UNKNOWN_VENDORS = ["Brightline Fabrication", "Hartwell & Sons Ltd", "Nova Supply Partners"]
ITEMS = [("WidgetA", Decimal("250.00")), ("WidgetB", Decimal("500.00")), ("GadgetX", Decimal("750.00"))]


class Rendering(BaseModel):
    text: str = Field(min_length=60, max_length=3500)


def scenario(rng: random.Random, i: int) -> dict:
    vendor = rng.choice([v[0] for v in SEED_VENDORS] + UNKNOWN_VENDORS)
    number = rng.choice([f"INV-{rng.randint(2000, 9999)}", f"INV-{rng.randint(2000, 9999)}", f"{rng.randint(10000, 99999)}", f"SI-{rng.randint(1000, 9999)}"])
    d = date(2026, rng.randint(1, 9), rng.randint(1, 26))
    items = []
    for name, price in rng.sample(ITEMS, rng.randint(1, 3)):
        qty = rng.randint(1, 12)
        items.append({"item": name, "quantity": qty, "unit_price": str(price + rng.choice([0, 0, 0, 10, -10]))})
    subtotal = sum(Decimal(x["unit_price"]) * x["quantity"] for x in items)
    rate = rng.choice([0, 0, 5, 8])
    tax = (subtotal * rate / 100).quantize(Decimal("0.01"))
    shipping = Decimal(rng.choice([0, 0, 25, 60, 120]))
    return {"vendor": vendor, "invoice_number": number, "date": d.isoformat(), "due_date": (d + timedelta(days=rng.choice([15, 30, 45]))).isoformat() if rng.random() > 0.2 else None,
            "currency": "USD", "items": items, "subtotal": str(subtotal), "tax_rate": rate, "tax_amount": str(tax), "shipping": str(shipping), "total": str(subtotal + tax + shipping)}


def prompt(truth: dict, style: str) -> str:
    facts = [f"vendor: {truth['vendor']}", f"invoice number: {truth['invoice_number']}", f"invoice date: {truth['date']}",
             f"due date: {truth['due_date']}" if truth["due_date"] else "due date: (none: leave it out entirely)",
             "items: " + "; ".join(f"{x['quantity']} x {x['item']} at ${x['unit_price']} each" for x in truth["items"]),
             f"subtotal: {truth['subtotal']}", f"tax: {truth['tax_amount']} ({truth['tax_rate']}%)", f"shipping: {truth['shipping']}", f"total: {truth['total']}", "currency: USD"]
    return (f"Write the text of a vendor invoice exactly as it would look after text extraction from a PDF, or as an email. Style: {STYLES[style]}.\n"
            "It must state these facts (format dates and amounts however a real document of this style would, keeping the same values), and must not invent any other "
            "amounts, quantities or item names:\n" + "\n".join(facts) + '\nReply as JSON: {"text": "..."}')


def faithful(truth: dict, text: str, ocr: bool = False) -> str | None:
    if ocr:  # look-alike swaps are the point of that style: undo the usual ones before checking
        text = text.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"}))
        truth = {**truth, "invoice_number": truth["invoice_number"].translate(str.maketrans({"O": "0", "I": "1"})), "items": []}
    low, alnum = text.lower(), re.sub(r"[^a-z0-9]", "", text.lower())
    if re.sub(r"[^a-z0-9]", "", truth["invoice_number"].lower()) not in alnum:
        return "invoice number missing"
    if re.sub(r"[^a-z0-9]", "", truth["vendor"].lower().split()[0]) not in alnum:
        return "vendor missing"
    t = Decimal(truth["total"])
    forms = {f"{t:,.2f}", f"{t:.2f}", f"{t:,.0f}", f"{t:.0f}", f"{t:,.2f}".replace(",", ".")}
    if not any(f in text for f in forms) and not re.search(r"[0-9OolS]{2,}", text):
        return "total missing"
    for x in truth["items"]:
        if re.sub(r"[^a-z0-9]", "", x["item"].lower()) not in alnum and x["item"].lower().replace("widget", "widg") not in low:
            return f"item {x['item']} missing"
    return None


async def main(n: int) -> None:
    llm = make_llm(live=True)
    if llm.label != "Grok":
        sys.exit("needs a real key (see README): this script asks Grok to write the renderings")
    styles = list(STYLES)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "docs").mkdir(exist_ok=True)
    sem, manifest = asyncio.Semaphore(4), []
    truths = [scenario(random.Random(2026 + i), i) for i in range(n)]   # each scenario has its own seed: the truth never depends on task timing

    async def run(i: int) -> None:
        style, truth = styles[i % len(styles)], truths[i]
        existing = OUT / "docs" / f"{i:03d}_{style}.txt"
        if existing.exists():  # resumable: only what is missing is paid for
            manifest.append({"id": i, "style": style, "file": f"docs/{existing.name}", "truth": truth})
            return
        async with sem:
            for _ in range(3):
                with as_agent("intake"):  # the 900-token reply cap: documents are long
                    try:
                        text = (await structured(llm, "You write realistic business documents for a software test set. Follow the instructions exactly.", prompt(truth, style), Rendering)).text
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {i:03d} {style}: {type(exc).__name__}: {exc}")
                        continue
                if (why := faithful(truth, text, ocr=style == "ocr")) is None:
                    name = f"{i:03d}_{style}.txt"
                    (OUT / "docs" / name).write_text(text)
                    manifest.append({"id": i, "style": style, "file": f"docs/{name}", "truth": truth})
                    return
                print(f"  {i:03d} {style}: rejected ({why}), retrying")

    await asyncio.gather(*(run(i) for i in range(n)))
    manifest.sort(key=lambda m: m["id"])
    (OUT / "truth.json").write_text(json.dumps(manifest, indent=1))
    print(f"wrote {len(manifest)} of {n} documents to {OUT.relative_to(ROOT)}; spent ${llm.meter.spent_usd:.4f}")
    await llm.aclose()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 60))
