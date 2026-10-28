import asyncio
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from acme_ap.agents import approval, validation
from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import heuristic, parse_file
from acme_ap.ingestion.parsing import parse_date, parse_money
from acme_ap.llm import GrokClient, OfflineLLM
from acme_ap.models import Decision, Finding, Invoice, LineItem, Severity
from acme_ap.pipeline import Pipeline

DATA = Path(__file__).resolve().parent.parent / "data" / "invoices"


@pytest.fixture
def erp(tmp_path):
    return MockNetSuite(tmp_path / "inv.db")


def run(erp, paths, workers=1, llm=None):
    return asyncio.run(Pipeline(erp, llm or OfflineLLM(), workers=workers).run([str(p) for p in paths]))


def by_number(outcomes):
    result = {}
    for o in outcomes:
        if o.status != "DUPLICATE":
            result[o.invoice.invoice_number + (f"-{o.invoice.revision}" if o.invoice.revision else "")] = o
    return result


EXPECTED = {
    "INV-1001": "PAID", "INV-1002": "REJECT", "INV-1003": "REJECT", "INV-1004": "PAID", "INV-1004-R1": "ESCALATE",
    "INV-1005": "REJECT", "INV-1006": "PAID", "INV-1007": "REJECT", "INV-1008": "REJECT", "INV-1009": "REJECT",
    "INV-1010": "PAID", "INV-1011": "PAID", "INV-1012": "PAID", "INV-1013": "REJECT", "INV-1014": "ESCALATE",
    "INV-1015": "PAID", "INV-1016": "REJECT",
}


def test_all_sample_invoices_end_to_end(erp):
    outcomes = run(erp, sorted(DATA.iterdir()))
    got = {k: v.status for k, v in by_number(outcomes).items()}
    assert got == EXPECTED
    # the pdf/txt (and pdf/json) twins must not be processed or paid twice
    assert sum(o.status == "DUPLICATE" for o in outcomes) == 3
    assert not any(o.error for o in outcomes)


def test_parallel_pool_matches_serial(erp, tmp_path):
    pdfs = sorted(DATA.glob("*.pdf")) * 3
    serial = run(erp, pdfs, workers=1)
    erp2 = MockNetSuite(tmp_path / "b.db")
    parallel = run(erp2, pdfs, workers=2)
    # which copy wins the duplicate claim depends on timing, so compare the multiset of outcomes
    assert sorted(o.status for o in serial) == sorted(o.status for o in parallel)


def test_no_double_payment_across_formats(erp):
    run(erp, [DATA / "invoice_1011.pdf", DATA / "invoice_1011.txt"])
    rows = erp._conn().execute("SELECT COUNT(*) FROM vendor_payments").fetchone()[0]
    assert rows == 1


def test_messy_pdf_text_extraction():
    inv, _ = parse_file(str(DATA / "invoice_1012.pdf"))
    assert inv.invoice_number == "INV-1012" and inv.date == "2026-01-26"  # 2O26 repaired
    assert [(li.item, li.quantity) for li in inv.line_items] == [("Widget A", 12), ("WidgetB", 7), ("Gadget X", 4)]
    assert inv.total == Decimal("9975.00")


def test_typo_header_invoice():
    inv = heuristic.extract((DATA / "invoice_1002.txt").read_text())
    assert (inv.invoice_number, inv.vendor, inv.total) == ("INV-1002", "Gadgets Co.", Decimal("15000.00"))
    assert inv.line_items[0].quantity == 20


def test_unparseable_date_is_not_guessed():
    assert parse_date("yesterday") is None
    assert parse_date("26-Jan-2O26") == "2026-01-26"
    assert parse_money("$3,500.O0") == Decimal("3500.00")


def test_validation_flags(erp):
    def codes(inv):
        return {f.code for f in validation.validate(inv, erp)}

    base = dict(invoice_number="INV-1", vendor="Widgets Inc.", date="2026-01-01", due_date="2026-02-01", total=Decimal(0))
    assert "unknown_item" in codes(Invoice(**base, line_items=[LineItem(item="Nope", quantity=1)]))
    assert "out_of_stock" in codes(Invoice(**base, line_items=[LineItem(item="FakeItem", quantity=1)]))
    assert "invalid_quantity" in codes(Invoice(**base, line_items=[LineItem(item="WidgetA", quantity=-1)]))
    assert "insufficient_stock" in codes(Invoice(**base, line_items=[LineItem(item="WidgetA", quantity=10), LineItem(item="WidgetA", quantity=6)]))
    assert "missing_vendor" in codes(Invoice(**{**base, "vendor": None}, line_items=[LineItem(item="WidgetA", quantity=1)]))


def test_policy_over_threshold_always_needs_a_person():
    inv = Invoice(invoice_number="X", vendor="V", total=Decimal("20000"))
    clean = approval.policy_decision(inv, [])
    warned = approval.policy_decision(inv, [Finding(code="price_variance", severity=Severity.WARN, message="w")])
    # above the limit a person always signs off, however clean the invoice is
    assert clean.decision is Decision.ESCALATE and warned.decision is Decision.ESCALATE
    assert "human sign-off" in clean.reasoning


class _GreedyLLM(OfflineLLM):
    """A model that tries to approve everything."""
    available = True

    async def chat_json(self, system, user):
        return {"decision": "approve", "reasoning": "looks fine to me, approving this invoice"} if "VP" in system else {"issues": []}


def test_llm_cannot_loosen_policy():
    inv = Invoice(invoice_number="X", vendor="V", total=Decimal("50"))
    findings = [Finding(code="unknown_item", severity=Severity.ERROR, message="bad item")]
    result = asyncio.run(approval.decide(inv, findings, _GreedyLLM()))
    assert result.decision is Decision.REJECT and result.critiques


def test_grok_client_parses_json_and_degrades():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": 'sure: {"decision": "reject"}'}}]})

    client = GrokClient("k", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.chat_json("s", "u")) == {"decision": "reject"}
    failing = GrokClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    from acme_ap.llm import LLMUnavailable
    with pytest.raises(LLMUnavailable):
        asyncio.run(failing.chat_json("s", "u"))


def test_llm_extract_disagreement_forces_review(erp):
    from acme_ap.ingestion import llm_extract

    class Liar(OfflineLLM):
        available = True

        async def chat_json(self, system, user):
            return {"invoice_number": "INV-1001", "vendor": "Widgets Inc.", "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250}],
                    "subtotal": 250, "tax_amount": 0, "total": 250}

    inv, text = parse_file(str(DATA / "invoice_1001.txt"))
    merged = asyncio.run(llm_extract.extract(text, inv, Liar()))
    assert any(w.startswith("extractor disagreement") for w in merged.parse_warnings)
    assert "extractor_disagreement" in {f.code for f in validation.validate(merged, erp)}
