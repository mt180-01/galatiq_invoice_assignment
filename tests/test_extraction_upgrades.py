"""Each test pins something the independent, Grok-rendered corpus (data/extraction_eval/) showed was wrong."""
import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import heuristic, llm_extract
from acme_ap.ingestion.heuristic import NON_ITEM, _clean_name, extract, join_label_lines, set_known_vendors, vendor_from_context
from acme_ap.ingestion.parsing import normalize_invoice_number
from acme_ap.llm import LLM, Meter
from acme_ap.models import Invoice, LineItem
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _vendor_master():
    set_known_vendors(["Widgets Inc.", "Gadgets Co.", "Summit Manufacturing Co."])
    yield
    set_known_vendors([])


# ---- invoice numbers keep their own scheme -------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [("INV 1012", "INV-1012"), ("inv-1012", "INV-1012"), ("#INV1012", "INV-1012"), ("INV-1012", "INV-1012"), ("1012", "INV-1012"),
                                          ("SI-1024", "SI-1024"), ("si 1024", "SI1024"), ("A/2041", "A/2041"), ("  #  ", None), ("", None), (None, None)])
def test_invoice_numbers_are_normalised_without_rewriting_other_schemes(raw, expected):
    assert normalize_invoice_number(raw) == expected


def test_a_vendors_own_numbering_scheme_survives_extraction():
    """Found: 'SI-1024' became 'INV-1024', changing the invoice's identity (duplicate detection, payment references)."""
    inv = extract("Vendor: Widgets Inc.\nInvoice Number: SI-1024\nWidgetA qty: 2 unit price: $250.00\nTotal: $500.00\n")
    assert inv.invoice_number == "SI-1024"


# ---- labels on their own line ---------------------------------------------------------------------------
def test_field_labels_are_joined_to_the_value_on_the_next_line_but_section_headers_are_not():
    text = "Vendor:\nWidgets Inc.\n\nInvoice Number:\nINV-1\nItems:\nWidgetA qty: 2 unit price: $250.00\nNotes:\nthanks\n"
    out = join_label_lines(text).splitlines()
    assert "Vendor: Widgets Inc." in out and "Invoice Number: INV-1" in out
    assert "Items:" in out and "WidgetA qty: 2 unit price: $250.00" in out and "Notes:" in out          # headers keep the lines under them
    assert join_label_lines("Vendor:\nTotal:\n5.00") == "Vendor:\nTotal: 5.00"                          # a label followed by another label is not a value


CELLS = """Vendor:
Gadgets Co.

Invoice Number:
13644

Invoice Date:
05/06/2026

Due Date:
05/21/2026

Item:
WidgetA
Quantity:
10
Unit Price:
240.00

Item:
GadgetX
Quantity:
6
Unit Price:
740.00

Subtotal:
6840.00
Tax (0%):
0.00
Shipping:
120.00
Total (USD):
6960.00
"""


def test_a_pdf_whose_every_cell_is_on_its_own_line_is_read_by_the_rules_alone():
    inv = extract(CELLS)
    assert (inv.vendor, inv.invoice_number, inv.total) == ("Gadgets Co.", "INV-13644", Decimal("6960.00"))
    assert [(li.item, li.quantity, li.unit_price) for li in inv.line_items] == [("WidgetA", 10, Decimal("240.00")), ("GadgetX", 6, Decimal("740.00"))]
    assert inv.shipping == Decimal("120.00") and not inv.parse_warnings


# ---- the vendor, when nothing labels it ------------------------------------------------------------------
def test_a_vendor_is_found_from_the_vendor_master_or_a_letterhead():
    assert vendor_from_context("Hi team,\nplease find our invoice.\nWidgets Inc. Accounting\n") == "Widgets Inc."
    assert vendor_from_context("SuperWidgets Inc.X is not it") is None                                                                             # whole names only; a sentence is no letterhead
    assert vendor_from_context("Thanks, this is from our Co. accounts team\nInvoice 4411\n") is None
    assert vendor_from_context("Brightline Fabrication Inc.\n12 Main St\nInvoice 4411\n") == "Brightline Fabrication Inc."                         # letterhead
    assert vendor_from_context("Invoice 4411\nsome words only\n") is None
    set_known_vendors(["Widgets", "Widgets Inc."])
    assert vendor_from_context("from Widgets Inc. today") == "Widgets Inc."                                                                       # the longest name wins


# ---- totals and dates ------------------------------------------------------------------------------------
def test_a_line_items_amount_column_is_not_a_competing_total():
    text = "Vendor: Widgets Inc.\nInvoice Number: INV-5000\nWidgetA qty: 2 unit price: $250.00\nAmount: 500.00\nWidgetB qty: 1 unit price: $9000.00\nAmount: 9000.00\nTotal: 9500.00\n"
    inv = extract(text)
    assert inv.total == Decimal("9500.00") and not [w for w in inv.parse_warnings if "several different totals" in w]


def test_labelled_totals_beat_a_bare_amount_and_the_weak_label_is_still_a_fallback():
    assert extract("Vendor: Widgets Inc.\nInvoice: INV-5001\nAmount: 10.00\nTotal Due: 77.00\n").total == Decimal("77.00")
    assert extract("Vendor: Widgets Inc.\nInvoice: INV-5002\nAmt: 1500\n").total == Decimal("1500")
    assert extract("Total (USD): 7450.00\nInvoice: INV-5003\n").total == Decimal("7450.00") and extract("AMT DUE 3560.00 USD\nInvoice: INV-5004\n").total == Decimal("3560.00")


def test_the_invoice_date_wins_over_an_email_headers_date_and_prose_is_not_a_due_date():
    thread = "From: ap@widgetsinc.com\nDate: Tue, 17 Feb 2026 09:12\n\nInvoice INV-8488\nInvoice Date: 2026-02-15\nCan you confirm the due date on the attached invoice?\nTotal: $500.00\n"
    inv = extract(thread)
    assert inv.date == "2026-02-15" and inv.due_date is None and not [w for w in inv.parse_warnings if "due date" in w]


def test_labels_without_colons_in_all_caps_documents_are_read():
    inv = extract("NOVA SUPPLY PARTNERS\nINV NO INV-9559\nINVOICE DATE 18 MAR 2026\nDUE DATE 02 APR 2026\nWIDGETB 7 500.00 3500.00\nTOTAL 3560.00\n")
    assert (inv.date, inv.due_date, inv.total) == ("2026-03-18", "2026-04-02", Decimal("3560.00"))


# ---- item names and non-item lines -----------------------------------------------------------------------
@pytest.mark.parametrize("raw,name", [("WidgetA - Premium quality widget with a 2-year warranty", "WidgetA"), ("WidgetA (rush order)", "WidgetA"),
                                      ("GadgetX: next-generation smart gadget", "GadgetX"), ("Steel-Bolt", "Steel-Bolt"), ("Widget A", "Widget A")])
def test_a_description_is_not_part_of_the_item_name(raw, name):
    assert _clean_name(raw)[0] == name


@pytest.mark.parametrize("line", ["Shipping", "Freight charge", "Sales Tax", "Discount 10%", "Subtotal", "Total", "Handling fee"])
def test_charges_are_not_items(line):
    assert NON_ITEM.match(line)


def test_the_models_shipping_line_becomes_shipping_not_stock():
    inv = llm_extract._build({"invoice_number": "INV-1", "vendor": "V", "total": 125, "line_items": [
        {"item": "WidgetA - Premium widget", "quantity": 4, "unit_price": 25, "amount": 100}, {"item": "Shipping", "quantity": 1, "unit_price": 25, "amount": 25}]})
    assert [(li.item, li.quantity) for li in inv.line_items] == [("WidgetA", 4)] and inv.shipping == Decimal("25")


# ---- reconciliation: accept what is in the document, hold what conflicts ---------------------------------------
class Replies(LLM):
    available = True

    def __init__(self, answer):
        self.answer, self.meter = answer, Meter()

    async def chat_json(self, system, user):
        return self.answer


def reconcile(text, answer, baseline=None):
    base = baseline or Invoice(invoice_number=None, vendor=None, total=None, line_items=[], channel="unstructured", extractor="heuristic:text",
                               parse_warnings=["invoice number not found"])
    return asyncio.run(llm_extract.extract(text, base, Replies(answer)))


DOC = "Acme Parts Ltd\nInvoice SI-4417 dated 2026-02-01\n3 x WidgetA at 250.00 each\nAmount due: $750.00\n"
GOOD = {"invoice_number": "SI-4417", "vendor": "Acme Parts Ltd", "date": "2026-02-01", "total": 750, "line_items": [{"item": "WidgetA", "quantity": 3, "unit_price": 250, "amount": 750}]}


def disagreements(inv):
    return [w for w in inv.parse_warnings if w.startswith("extractor disagreement")]


def test_what_the_regex_could_not_find_is_accepted_when_it_is_in_the_document_and_stale_warnings_go():
    inv = reconcile(DOC, GOOD)
    assert not disagreements(inv) and inv.invoice_number == "SI-4417" and inv.total == Decimal("750") and "invoice number not found" not in inv.parse_warnings


@pytest.mark.parametrize("change,fragment", [({"total": 7500}, "total 7500 is not in the document"), ({"vendor": "Totally Different Inc"}, "'Totally Different Inc' is not in the document"),
                                             ({"invoice_number": "INV-9999"}, "'INV-9999' is not in the document"),
                                             ({"line_items": [{"item": "GadgetX", "quantity": 3, "unit_price": 250, "amount": 750}]}, "'GadgetX' x3 is not in the document"),
                                             ({"line_items": [{"item": "WidgetA", "quantity": 30, "unit_price": 25, "amount": 750}]}, "'WidgetA' x30 is not in the document")])
def test_what_the_model_made_up_is_a_conflict_not_a_reading(change, fragment):
    assert any(fragment in w for w in disagreements(reconcile(DOC, {**GOOD, **change})))


def test_a_bank_account_seen_only_by_the_model_is_never_accepted():
    assert any("bank account differs" in w for w in disagreements(reconcile(DOC + "pay to 12345678\n", {**GOOD, "bank_account": "12345678"})))


def test_when_both_readers_have_a_value_and_differ_it_is_a_conflict():
    base = Invoice(invoice_number="INV-1", vendor="Widgets Inc.", total=Decimal("750"), line_items=[LineItem(item="WidgetA", quantity=3)], channel="unstructured", extractor="heuristic:text")
    assert any("vendor" in w for w in disagreements(reconcile(DOC, GOOD, base)))
    assert any("invoice_number" in w for w in disagreements(reconcile(DOC, GOOD, base)))


def test_a_sku_chosen_over_the_product_name_is_caught_downstream_not_paid(tmp_path):
    """Found in the 'multiline' style: the model read the SKU column as the item. The catalogue does not know SKUs, so the invoice is rejected, never paid."""
    text = "Widgets Inc.\nInvoice INV-7061\nWidgetA - Premium widget    WGT-A001    7    $250.00    $1,750.00\nTotal $1,750.00\n"
    erp = MockNetSuite(tmp_path / "s.db")
    bad = {"invoice_number": "INV-7061", "vendor": "Widgets Inc.", "total": 1750, "line_items": [{"item": "WGT-A001", "quantity": 7, "unit_price": 250, "amount": 1750}]}
    f = tmp_path / "inv.txt"
    f.write_text(text)

    class SkuModel(Replies):
        async def chat_json(self, system, user):
            if system.startswith("You extract"):
                return bad
            raise ValueError("not needed")
    (o,) = asyncio.run(Pipeline(erp, SkuModel(bad), workers=1).run([str(f)]))
    assert not o.paid and o.status in ("REJECT", "ESCALATE")


# ---- the independent evaluation itself -------------------------------------------------------------------------
def test_the_extraction_evaluation_is_current_and_says_what_the_readers_found():
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "XAI_API_KEY_FILE": "/nonexistent"}
    p = subprocess.run([sys.executable, "scripts/evaluate_extraction.py", "--check"], cwd=ROOT, capture_output=True, text=True, env=env, timeout=240)
    assert p.returncode == 0, p.stdout + p.stderr
    text = (ROOT / "docs" / "EXTRACTION_EVAL.md").read_text()
    rows = {r.split("|")[1].strip(): [c.strip() for c in r.split("|")[2:-1]] for r in text.splitlines() if r.startswith("| rules") or r.startswith("| gated") or r.startswith("| always")}
    assert set(rows) == {"rules", "gated", "always"}
    assert all(r[2] == "**0**" for r in rows.values()), "no reader may approve a wrongly read invoice for payment"
    assert rows["gated"][0].startswith("58 / 58") and rows["always"][0].startswith("58 / 58")                 # with the model: every critical field right
    assert int(rows["rules"][0].split()[0]) > 20 and int(rows["gated"][4]) < int(rows["always"][4])           # the rules alone are useful; asking only when unsure costs fewer calls


def test_the_corpus_and_its_recording_are_committed_and_clean():
    manifest = json.loads((ROOT / "data/extraction_eval/truth.json").read_text())
    assert len(manifest) >= 55 and len({m["style"] for m in manifest}) == 12
    assert all((ROOT / "data/extraction_eval" / m["file"]).exists() for m in manifest)
    rec = [json.loads(l) for l in (ROOT / "data/extraction_eval/extraction.cassette.jsonl").read_text().splitlines()[1:]]
    assert all(r["status"] == 200 for r in rec) and len(rec) >= 50
