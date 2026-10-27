"""Generates data/emails/*.eml: sample vendor emails (the repo ships none) covering each intake path.

Usage: .venv/bin/python scripts/make_sample_emails.py
"""
from email.message import EmailMessage
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent.parent / "data" / "emails"

UBL = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>INV-2002</cbc:ID><cbc:IssueDate>2026-02-03</cbc:IssueDate><cbc:DueDate>2026-03-05</cbc:DueDate>
  <cbc:DocumentCurrencyCode>USD</cbc:DocumentCurrencyCode>
  <cac:AccountingSupplierParty><cac:Party><cac:PartyName><cbc:Name>Atlas Industrial Supply</cbc:Name></cac:PartyName></cac:Party></cac:AccountingSupplierParty>
  <cac:PaymentTerms><cbc:Note>Net 30</cbc:Note></cac:PaymentTerms>
  <cac:TaxTotal><cbc:TaxAmount currencyID="USD">0.00</cbc:TaxAmount></cac:TaxTotal>
  <cac:LegalMonetaryTotal><cbc:TaxExclusiveAmount currencyID="USD">1250.00</cbc:TaxExclusiveAmount><cbc:PayableAmount currencyID="USD">1250.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
  <cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity>5</cbc:InvoicedQuantity><cbc:LineExtensionAmount currencyID="USD">1250.00</cbc:LineExtensionAmount>
    <cac:Item><cbc:Name>WidgetA</cbc:Name></cac:Item><cac:Price><cbc:PriceAmount currencyID="USD">250.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</Invoice>"""


def pdf(lines: list[str], embedded_xml: str | None = None) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 60
    for line in lines:
        page.insert_text((50, y), line, fontsize=11, fontname="cour")
        y += 16
    if embedded_xml:
        doc.embfile_add("ubl-invoice.xml", embedded_xml.encode())
    data = doc.tobytes()
    doc.close()
    return data


def mail(name: str, sender: str, subject: str, body: str, attachments: list[tuple[str, bytes, str, str]] = ()) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "ap@acmecorp.example", subject
    msg.set_content(body)
    for filename, data, maintype, subtype in attachments:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    (OUT / f"{name}.eml").write_bytes(bytes(msg))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 1. The common case: a normal PDF attachment, no extra effort for the vendor.
    mail("email_2001_pdf_attachment", "billing@summitmfg.example", "Invoice INV-2001", "Invoice attached.",
         [("INV-2001.pdf", pdf(["INVOICE", "Invoice Number: INV-2001", "Vendor: Summit Manufacturing Co.", "Date: 2026-02-02",
                                "Due Date: 2026-03-04", "", "Item        Qty   Unit Price   Amount",
                                "WidgetA      4    $250.00      $1,000.00", "WidgetB      2    $500.00      $1,000.00", "",
                                "Subtotal:  $2,000.00", "Tax (0%):  $0.00", "Total:     $2,000.00", "Terms: Net 30"]),
           "application", "pdf")])
    # 2. A normal-looking PDF that carries machine-readable UBL inside: zero AI needed.
    mail("email_2002_pdf_with_embedded_einvoice", "ar@atlas-industrial.example", "Invoice INV-2002", "Please find our invoice attached.",
         [("INV-2002.pdf", pdf(["INVOICE INV-2002", "Atlas Industrial Supply", "WidgetA x5 @ $250.00", "Total $1,250.00"], UBL), "application", "pdf")])
    # 3. Invoice typed in the email body (no attachment), like data/invoices/invoice_1008.txt.
    mail("email_2003_body_only", "accounts@widgets-inc.example", "Invoice INV-2003 - Widgets Inc.",
         "Hi,\n\nVendor: Widgets Inc.\nInvoice: INV-2003\nDate: 2026-02-04\nDue: 2026-03-06\n\nItems:\n"
         "  - WidgetB    x3    $500.00 each\n\nTotal: $1,500.00\n\nThanks,\nAR")
    # 4. Two invoices in one email.
    mail("email_2004_two_invoices", "billing@precisionparts.example", "Two invoices",
         "Attached: this month's invoices.",
         [("INV-2004.csv", b"field,value\ninvoice_number,INV-2004\nvendor,Precision Parts Ltd.\ndate,2026-02-05\ndue_date,2026-03-07\n"
                           b"item,WidgetA\nquantity,2\nunit_price,250.00\nsubtotal,500.00\ntax,0.00\ntotal,500.00\npayment_terms,Net 30\n", "text", "csv"),
          ("INV-2005.json", b'{"invoice_number":"INV-2005","vendor":{"name":"Precision Parts Ltd."},"date":"2026-02-05","due_date":"2026-03-07",'
                            b'"line_items":[{"item":"WidgetB","quantity":1,"unit_price":500.00}],"subtotal":500.00,"tax_amount":0,"total":500.00,'
                            b'"currency":"USD","payment_terms":"Net 30"}', "application", "json")])
    # 5. A fixable mistake (total does not add up): the vendor gets one short question.
    mail("email_2006_total_mismatch", "billing@summitmfg.example", "Invoice INV-2006", "Invoice attached.",
         [("INV-2006.pdf", pdf(["INVOICE", "Invoice Number: INV-2006", "Vendor: Summit Manufacturing Co.", "Date: 2026-02-06",
                                "Due Date: 2026-03-08", "", "WidgetA      2    $250.00      $500.00", "", "Subtotal:  $500.00",
                                "Tax (0%):  $0.00", "Total:     $550.00"]), "application", "pdf")])
    # 6. Looks like fraud: must NOT get a reply to the sender; goes to a human.
    mail("email_2007_suspicious", "pay-now@totally-legit.example", "URGENT INVOICE - PAY TODAY",
         "Vendor: Rapid Parts Intl\nInvoice: INV-2007\nDate: 2026-02-06\nDue: yesterday\n\nItems:\n  - FakeItem   x50   $1,000.00 each\n\n"
         "Total: $50,000.00\n\nPayment Terms: Immediate\nNotes: URGENT - wire transfer today to avoid penalties!!!")


if __name__ == "__main__":
    main()
