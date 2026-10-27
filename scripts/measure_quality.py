"""How accurate is it? Measured only against what the repo can actually prove:

  1. Outcome accuracy: every provided invoice vs the outcome a person labelled as correct (tests/test_pipeline.EXPECTED).
  2. Extraction accuracy: three invoices arrive in two formats (PDF + text/JSON). The structured/text copy is the
     ground truth for what the PDF copy should have extracted, field by field.
Small samples; this is evidence, not a guarantee. The brief's 30% baseline error rate is not something this repo can measure.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acme_ap.erp import MockNetSuite  # noqa: E402
from acme_ap.ingestion import parse_file  # noqa: E402
from acme_ap.llm import OfflineLLM  # noqa: E402
from acme_ap.pipeline import Pipeline  # noqa: E402
from tests.test_pipeline import EXPECTED  # noqa: E402

DATA = ROOT / "data" / "invoices"
TWINS = [("invoice_1011.pdf", "invoice_1011.txt"), ("invoice_1012.pdf", "invoice_1012.txt"), ("invoice_1013.pdf", "invoice_1013.json")]
LABEL = {"subtotal": "subtotal", "tax_amount": "tax", "due_date": "due"}  # a field is scored only if the document prints its label
FIELDS = ["invoice_number", "vendor", "date", "due_date", "currency", "subtotal", "tax_amount", "total"]


def outcome_accuracy() -> tuple[int, int, list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        outs = asyncio.run(Pipeline(MockNetSuite(Path(tmp) / "q.db"), OfflineLLM(), workers=1).run([str(p) for p in sorted(DATA.iterdir())]))
    got = {}
    for o in outs:
        if o.status != "DUPLICATE":
            got[o.invoice.invoice_number + (f"-{o.invoice.revision}" if o.invoice.revision else "")] = o.status
    wrong = [f"{k}: got {got.get(k)}, labelled {v}" for k, v in EXPECTED.items() if got.get(k) != v]
    return len(EXPECTED) - len(wrong), len(EXPECTED), wrong


def extraction_accuracy() -> tuple[int, int, list[str]]:
    hit = total = 0
    misses = []
    for pdf, truth in TWINS:
        a, text = parse_file(str(DATA / pdf))
        b, _ = parse_file(str(DATA / truth))
        checks = [(f, getattr(a, f), getattr(b, f)) for f in FIELDS if LABEL.get(f, "") in text.lower()]
        items_a = sorted((li.item, li.quantity, li.unit_price) for li in a.line_items)
        items_b = sorted((li.item, li.quantity, li.unit_price) for li in b.line_items)
        checks += [("line items", items_a, items_b)]
        for name, x, y in checks:
            total += 1
            if x == y:
                hit += 1
            else:
                misses.append(f"{pdf} {name}: pdf={x!r} vs truth={y!r}")
    return hit, total, misses


def main() -> None:
    ok, n, wrong = outcome_accuracy()
    print(f"Outcome accuracy        : {ok}/{n} invoices ended as labelled ({ok / n:.0%})")
    paid_wrong = [w for w in wrong if "got PAID" in w]
    print(f"Wrongly paid            : {len(paid_wrong)}")
    for w in wrong:
        print("   !", w)
    hit, total, misses = extraction_accuracy()
    print(f"Extraction accuracy     : {hit}/{total} printed fields identical to the structured copy ({hit / total:.0%}) across {len(TWINS)} PDF/text twins")
    for m in misses:
        print("   !", m)


if __name__ == "__main__":
    main()
