"""The web interface, driven over real HTTP against a real server on a free port."""
import asyncio
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from acme_ap.runner import run_batch
from acme_ap.web import make_server

ROOT = Path(__file__).resolve().parent.parent
XSS = "<script>alert('pwned')</script>"


def load(db, *paths):
    args = SimpleNamespace(db=str(db), keep_ledger=False, llm_concurrency=4, offline=False, simulate=True, grok=False)
    asyncio.run(run_batch([str(p) for p in paths], args, 1))


@pytest.fixture
def ui(tmp_path):
    hostile = tmp_path / "invoice_6666.json"
    hostile.write_text(json.dumps({"invoice_number": "INV-6666", "vendor": XSS, "date": "2026-02-01", "due_date": "2026-03-01", "currency": "USD", "payment_terms": "Net 30",
                                   "line_items": [{"item": "WidgetA", "quantity": 1, "unit_price": 250, "amount": 250}], "subtotal": 250, "tax_amount": 0, "total": 250,
                                   "notes": XSS}))
    db = tmp_path / "ui.db"
    load(db, ROOT / "data/approvals_demo/invoice_3001.json", ROOT / "data/invoices/invoice_1001.txt", ROOT / "data/invoices/invoice_1002.txt", hostile)
    server = make_server(db, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, db
    server.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(), dict(err.headers)


def post(url, **form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()


def invoice_id(base, number):
    """Row ids follow processing order, which is concurrent, so find the page by the invoice number instead."""
    return re.search(rf'href="/invoice/(\d+)">{number}<', get(base + "/")[1]).group(1)


def approve_link(base):
    _, html_, _ = get(base + "/")
    return base + re.search(r'href="(/decide\?t=[^"]+)"[^>]*>INV-3001', html_).group(1).replace("&amp;", "&")


def test_dashboard_lists_what_happened_and_who_is_waiting(ui):
    base, _ = ui
    code, body, headers = get(base + "/")
    assert code == 200 and "Waiting for a decision" in body and "INV-3001" in body and "INV-1001" in body and "Rejected" in body and "Paid" in body
    assert "VP Finance" in body and "Payments" in body


def test_hostile_text_in_invoice_fields_is_never_executed(ui):
    base, _ = ui
    pages = [get(base + "/")[1]] + [get(base + f"/invoice/{i}")[1] for i in range(1, 6)]
    token_page = None
    for p in pages:
        assert "<script>alert" not in p
        for m in re.findall(r'href="(/decide\?t=[^"]+)"', p):
            token_page = token_page or get(base + m.replace("&amp;", "&"))[1]
    assert any("&lt;script&gt;alert" in p for p in pages)                      # shown as harmless text
    assert token_page is None or "<script>alert" not in token_page


def test_security_headers_on_every_response(ui):
    base, _ = ui
    for url in (base + "/", base + "/invoice/1", base + "/nope", base + "/decide?t=bad"):
        _, _, h = get(url)
        assert "default-src 'none'" in h["Content-Security-Policy"] and h["X-Content-Type-Options"] == "nosniff" and h["Cache-Control"] == "no-store"


def test_invoice_page_shows_findings_and_the_agents_trace(ui):
    base, _ = ui
    code, body, _ = get(base + f"/invoice/{invoice_id(base, 'INV-1001')}")      # paid after a model second opinion with tool calls
    assert code == 200 and "What each agent did" in body and "lookup_vendor" in body and "Reviewer" in body and "In short" in body
    code, body, _ = get(base + f"/invoice/{invoice_id(base, 'INV-1002')}")      # rejected for stock
    assert "only 5 in stock" in body and "What the checks found" in body


def test_unknown_pages_and_ids_are_404(ui):
    base, _ = ui
    assert get(base + "/invoice/9999")[0] == 404 and get(base + "/invoice/abc")[0] == 404 and get(base + "/nothing")[0] == 404
    assert get(base + "/invoice/../../etc/passwd")[0] == 404


def test_a_forged_or_empty_decision_link_is_refused(ui):
    base, _ = ui
    link = approve_link(base)
    forged = link[:-6] + ("AAAAAA" if not link.endswith("AAAAAA") else "BBBBBB")
    for url in (forged, base + "/decide", base + "/decide?t=", base + "/decide?t=not.a.token"):
        code, body, _ = get(url)
        assert code == 400 and "cannot be used" in body


def test_approval_page_then_decision_pays_once(ui):
    base, db = ui
    link = approve_link(base)
    code, body, _ = get(link)
    assert code == 200 and "Pay USD 12,500.00 to Widgets Inc.?" in body and "Approve and pay" in body and "In short" in body and "Why this needs you" in body
    assert "Deciding as" in body and "<b>vp-finance@acmecorp.example</b>" in body and "<select" not in body       # identity is the link's, not a pick-list
    form = dict(zip(("t_approve", "t_reject"), re.findall(r'name="t_(?:approve|reject)" value="([^"]+)"', body)))
    ok = post(base + "/decide", decision="approve", who="attacker@evil.example", comment="looks right", **form)                   # a posted name has no weight
    assert ok[0] == 200 and "paid" in ok[1] and "Payment reference" in ok[1]
    import sqlite3
    assert sqlite3.connect(db).execute("SELECT decided_by FROM approval_requests WHERE bill_key LIKE '%3001%'").fetchone()[0] == "vp-finance@acmecorp.example"
    again = post(base + "/decide", decision="approve", **form)
    assert again[0] == 400 and "already approved" in again[1]                                                                        # a double click or replay pays nothing twice
    assert "already <b>approved</b>" in get(link)[1]
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM vendor_payments WHERE bill_key LIKE '%3001%'").fetchone()[0] == 1
    assert "Waiting for a decision" in get(base + "/")[1]


def test_a_tampered_form_token_is_refused(ui):
    base, db = ui
    _, body, _ = get(approve_link(base))
    form = dict(zip(("t_approve", "t_reject"), re.findall(r'name="t_(?:approve|reject)" value="([^"]+)"', body)))
    form["t_approve"] = form["t_approve"][:-3] + ("AAA" if not form["t_approve"].endswith("AAA") else "BBB")
    res = post(base + "/decide", decision="approve", **form)
    assert res[0] == 400 and "signature" in res[1]
    import sqlite3
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM vendor_payments WHERE bill_key LIKE '%3001%'").fetchone()[0] == 0


def test_reject_works_and_does_not_pay(ui):
    base, db = ui
    _, body, _ = get(approve_link(base))
    form = dict(zip(("t_approve", "t_reject"), re.findall(r'name="t_(?:approve|reject)" value="([^"]+)"', body)))
    res = post(base + "/decide", decision="reject", **form)
    assert res[0] == 200 and "rejected" in res[1]
    import sqlite3
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM vendor_payments WHERE bill_key LIKE '%3001%'").fetchone()[0] == 0


def test_empty_database_gets_a_helpful_page(tmp_path):
    server = make_server(tmp_path / "empty.db", port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code, body, _ = get(f"http://127.0.0.1:{server.server_address[1]}/")
        assert code == 200 and "Nothing here yet" in body and "serve --demo" in body
    finally:
        server.shutdown()


def test_approval_links_in_cards_point_at_the_local_interface(ui):
    from acme_ap.approvals.service import LINK_BASE
    assert LINK_BASE.startswith("http://127.0.0.1:8765/decide?t=") and "acmecorp.example" not in LINK_BASE
