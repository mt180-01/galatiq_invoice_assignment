import asyncio
from decimal import Decimal
from pathlib import Path

import pymupdf
import pytest

from acme_ap import intake, outreach
from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import parse_file, structured
from acme_ap.llm import OfflineLLM
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
EMAILS = ROOT / "data" / "emails"


def run(tmp_path, paths, origins=None):
    erp = MockNetSuite(tmp_path / "t.db")
    return asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, origins=origins).run(paths))


def process_emails(tmp_path, names):
    paths, origins = intake.expand([str(EMAILS / n) for n in names], tmp_path / "work")
    return run(tmp_path, paths, origins), origins


def test_ubl_invoice_parsed_without_ai():
    ubl = (ROOT / "scripts" / "make_sample_emails.py").read_text().split('UBL = """')[1].split('"""')[0]
    inv = structured.from_xml(ubl)
    assert (inv.invoice_number, inv.vendor, inv.total, inv.due_date) == ("INV-2002", "Atlas Industrial Supply", Decimal("1250.00"), "2026-03-05")
    assert [(li.item, li.quantity, li.unit_price) for li in inv.line_items] == [("WidgetA", 5, Decimal("250.00"))]
    assert inv.channel == "structured"


def test_cii_invoice_parsed():
    xml = """<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
      xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
      xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
      <rsm:ExchangedDocument><ram:ID>INV-3001</ram:ID><ram:IssueDateTime><udt:DateTimeString>20260210</udt:DateTimeString></ram:IssueDateTime></rsm:ExchangedDocument>
      <rsm:SupplyChainTradeTransaction>
        <ram:IncludedSupplyChainTradeLineItem><ram:SpecifiedTradeProduct><ram:Name>WidgetB</ram:Name></ram:SpecifiedTradeProduct>
          <ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice><ram:ChargeAmount>500.00</ram:ChargeAmount></ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>
          <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity>2</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery></ram:IncludedSupplyChainTradeLineItem>
        <ram:ApplicableHeaderTradeAgreement><ram:SellerTradeParty><ram:Name>Widgets Inc.</ram:Name></ram:SellerTradeParty></ram:ApplicableHeaderTradeAgreement>
        <ram:ApplicableHeaderTradeSettlement><ram:InvoiceCurrencyCode>USD</ram:InvoiceCurrencyCode>
          <ram:SpecifiedTradeSettlementHeaderMonetarySummation><ram:GrandTotalAmount>1000.00</ram:GrandTotalAmount></ram:SpecifiedTradeSettlementHeaderMonetarySummation>
        </ram:ApplicableHeaderTradeSettlement></rsm:SupplyChainTradeTransaction></rsm:CrossIndustryInvoice>"""
    inv = structured.from_xml(xml)
    assert (inv.invoice_number, inv.vendor, inv.date, inv.total) == ("INV-3001", "Widgets Inc.", "2026-02-10", Decimal("1000.00"))
    assert inv.line_items[0].quantity == 2


def test_xml_entity_bomb_is_rejected():
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><invoice>&b;</invoice>'
    with pytest.raises(Exception):
        structured.from_xml(bomb)


def test_pdf_with_embedded_einvoice_skips_text_extraction(tmp_path):
    inv, text = parse_file(str(next(iter(_attachments(tmp_path, "email_2002_pdf_with_embedded_einvoice.eml")))))
    assert inv.extractor == "embedded-ubl" and inv.lane == "embedded" and inv.total == Decimal("1250.00")


def _attachments(tmp_path, name):
    paths, _ = intake.expand([str(EMAILS / name)], tmp_path / "w")
    return paths


def test_email_with_two_attachments_becomes_two_invoices(tmp_path):
    outcomes, origins = process_emails(tmp_path, ["email_2004_two_invoices.eml"])
    assert sorted(o.invoice.invoice_number for o in outcomes) == ["INV-2004", "INV-2005"]
    assert all(o.sender == "billing@precisionparts.example" for o in outcomes)


def test_body_only_email_is_ingested(tmp_path):
    (o,), _ = process_emails(tmp_path, ["email_2003_body_only.eml"])
    assert o.status == "PAID" and o.invoice.vendor == "Widgets Inc." and o.invoice.total == Decimal("1500.00")


def test_fixable_error_gets_one_short_question_to_the_sender(tmp_path):
    (o,), _ = process_emails(tmp_path, ["email_2006_total_mismatch.eml"])
    assert o.status == "REJECT" and o.followup
    assert o.followup.startswith("To: billing@summitmfg.example")
    assert o.followup.count("\n  1.") == 1 and "\n  2." not in o.followup


def test_suspicious_email_gets_no_reply_to_the_sender(tmp_path):
    (o,), _ = process_emails(tmp_path, ["email_2007_suspicious.eml"])
    assert o.status == "REJECT" and o.followup is None


def test_no_followup_for_internal_issues_or_paid(tmp_path):
    outcomes = run(tmp_path, [str(ROOT / "data/invoices" / n) for n in ("invoice_1001.txt", "invoice_1002.txt", "invoice_1003.txt", "invoice_1008.txt")])
    # 1001 paid; 1002 stock problem is ours; 1003 fraud; 1008 unverified vendor: nobody is asked anything
    assert [o.followup for o in outcomes] == [None, None, None, None]


def test_followup_questions_are_capped(tmp_path):
    (o,) = run(tmp_path, [str(ROOT / "data/invoices/invoice_1009.json")])
    assert o.followup and o.followup.count("\n  ") <= outreach.MAX_ASKS


def test_planted_total_error_in_sample_1013_is_caught(tmp_path):
    (o,) = run(tmp_path, [str(ROOT / "data/invoices/invoice_1013.json")])
    assert "total_math" in {f.code for f in o.findings}  # 21040 + 1472.80 = 22512.80, invoice says 22562.80
