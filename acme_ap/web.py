"""The local web interface: a dashboard, an invoice page with the agents' trace, and the approval page the decision links open.

Standard library only (nothing to install), bound to this machine, every dynamic value HTML-escaped, strict content-security-policy.
Who is approving is chosen from the mailboxes the routing table authorises for that request. That is a stand-in for single sign-on:
a deployment must put real authentication in front of this page.
"""
from __future__ import annotations

import html
import json
import urllib.parse
from datetime import datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .approvals import ApprovalService
from .approvals.tokens import TokenError
from .erp import MockNetSuite
from .models import Outcome
from .payments import MockBank, PaymentService
from .store import OutcomeStore

e = html.escape

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16202b;--mute:#5d6b7a;--line:#e3e7ec;--brand:#1f4fd8;--ok:#0f7b45;--okbg:#e4f5ec;--bad:#b3261e;--badbg:#fbe9e7;
--wait:#8a5a00;--waitbg:#fff3d6;--info:#145a8a;--infobg:#e2f0fa;--mutebg:#eceff3}
@media (prefers-color-scheme:dark){:root{--bg:#10151b;--card:#18202a;--ink:#e8edf2;--mute:#9aa8b6;--line:#2a3542;--brand:#7ea2ff;--ok:#6fd49b;--okbg:#14301f;
--bad:#ff8a80;--badbg:#3a1a17;--wait:#ffcc66;--waitbg:#3a2f12;--info:#7cc4f2;--infobg:#132a3a;--mutebg:#222c37}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 20px;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
header b{font-size:17px}header a{color:var(--brand);text-decoration:none}main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:4px 0 4px}h2{font-size:16px;margin:28px 0 10px}.sub{color:var(--mute);margin:0 0 18px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}.tile .n{font-size:28px;font-weight:650;line-height:1.1}
.tile .l{color:var(--mute);font-size:13px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}td:first-child{white-space:nowrap}td.num{white-space:nowrap}th{font-size:12px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12.5px;font-weight:600;white-space:nowrap}
.ok{background:var(--okbg);color:var(--ok)}.bad{background:var(--badbg);color:var(--bad)}.wait{background:var(--waitbg);color:var(--wait)}
.info{background:var(--infobg);color:var(--info)}.muted{background:var(--mutebg);color:var(--mute)}
a.btn,button{font:inherit;font-weight:600;border-radius:8px;padding:9px 18px;border:1px solid var(--line);cursor:pointer;text-decoration:none;display:inline-block;color:var(--ink);background:var(--card)}
button.approve{background:var(--ok);border-color:var(--ok);color:#fff}button.reject{background:var(--card);color:var(--bad);border-color:var(--bad)}
a.btn.primary{background:var(--brand);border-color:var(--brand);color:#fff}a{color:var(--brand)}
.trace{border-left:3px solid var(--line);margin:0 0 6px 6px;padding:2px 0 2px 14px}.trace .who{font-weight:650}
.ev{color:var(--mute);font-size:13.5px;margin:2px 0 2px 4px;word-break:break-word}.ev code{background:var(--mutebg);padding:1px 5px;border-radius:4px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:4px 14px}.kv div:nth-child(odd){color:var(--mute)}ul.f{margin:6px 0 0;padding-left:18px}
select,textarea{font:inherit;width:100%;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}
label{display:block;margin:12px 0 4px;color:var(--mute);font-size:13px}.actions{display:flex;gap:10px;margin-top:16px;flex-wrap:wrap}
.note{background:var(--infobg);color:var(--info);border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13.5px}
@media (max-width:600px){.kv{grid-template-columns:1fr}th,td{padding:8px}}
"""

STATUS = {"paid": ("Paid", "ok"), "scheduled": ("Scheduled", "info"), "pending_approval": ("Waiting for a person", "wait"), "reject": ("Rejected", "bad"),
          "rejected": ("Rejected", "bad"), "escalate": ("Needs review", "wait"), "blocked": ("Blocked", "bad"), "expired": ("Expired unpaid", "bad"),
          "payment_held": ("Payment held", "wait"), "payment_failed": ("Payment failed", "bad"), "payment_returned": ("Payment returned", "bad"),
          "approve": ("Approved", "ok"), "received": ("Received", "muted")}


def money(v, cur="USD") -> str:
    try:
        return f"{cur} {Decimal(str(v)):,.2f}"
    except Exception:  # noqa: BLE001
        return "-"


def short(text: str, n: int = 140) -> str:
    """Cut at a word boundary, with an ellipsis, instead of mid-word."""
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def pill(label: str, kind: str) -> str:
    return f'<span class="pill {kind}">{e(label)}</span>'


def page(title: str, body: str, refresh: bool = False) -> bytes:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{e(title)}</title><style>{CSS}</style></head><body><header><b>Acme AP</b><a href="/">Dashboard</a>'
            f'<span style="color:var(--mute)">invoice processing with a team of agents</span></header><main>{body}</main></body></html>').encode()


class App:
    """The data the pages need, read from the database the command-line runs leave behind."""

    def __init__(self, db: str | Path):
        self.db = str(db)
        self.erp = MockNetSuite(self.db, keep_ledger=True)
        self.payments = PaymentService(self.db, self.erp, MockBank(self.db + ".bank"), reset=False)
        self.approvals = ApprovalService(self.db, self.erp, reset=False, payments=self.payments)
        self.store = OutcomeStore(self.db)

    def status(self, row_id: int, o: Outcome) -> tuple[str, str]:
        """The invoice's state *now* (from the ledger), which may have moved on since the run: approved, paid, expired..."""
        if o.status == "DUPLICATE":
            return "Duplicate blocked", "muted"
        if o.error:
            return "Error", "bad"
        key = self._key(row_id)
        return STATUS.get(self.erp.bill_status(key) or "", (o.status.title(), "muted")) if key else (o.status.title(), "muted")

    def _key(self, row_id: int) -> str | None:
        row = self.store._db.execute("SELECT bill_key FROM outcomes WHERE id=?", (row_id,)).fetchone()
        return row[0] if row else None

    # ---- pages ---------------------------------------------------------------------------------
    def dashboard(self) -> bytes:
        rows = sorted(self.store.all(), key=lambda r: (str(r[1].invoice.invoice_number if r[1].invoice else "~"), r[0]))  # by invoice number, not by which finished first
        real = [(i, o) for i, o in rows if o.invoice and o.status != "DUPLICATE"]
        shown = [(i, o, *self.status(i, o)) for i, o in rows]
        paid = sum(1 for _, _, label, _ in shown if label in ("Paid", "Scheduled"))
        stopped = sum(1 for _, _, label, _ in shown if label in ("Rejected", "Blocked", "Expired unpaid"))
        dup = sum(1 for i, o in rows if o.status == "DUPLICATE")
        waiting = self.approvals.waiting()
        pay_rows = self.payments.rows()
        sched = [r for r in pay_rows if r["state"] == "scheduled"]
        sent = [r for r in pay_rows if r["state"] in ("submitted", "settled")]
        tiles = [(len(real), "invoices processed"), (paid, "paid or scheduled"), (len(waiting), "waiting for a person"), (stopped, "stopped before the bank"), (dup, "duplicates blocked")]
        if not rows:
            return page("Acme AP", '<h1>Nothing here yet</h1><p class="sub">Process some invoices first: <code>python main.py --invoice_path=data/invoices</code>, '
                                   'then reload this page. Or run <code>python main.py serve --demo</code> to load a worked example.</p>')
        body = ['<h1>Invoice processing</h1><p class="sub">What the agents did with the invoices, who has to decide, and where the money is.</p>',
                '<div class="tiles">' + "".join(f'<div class="tile"><div class="n">{n}</div><div class="l">{e(l)}</div></div>' for n, l in tiles) + "</div>"]
        body.append("<h2>Waiting for a decision</h2>")
        if waiting:
            trs = "".join(
                f'<tr><td><a href="/decide?t={e(self.approvals.tokens_for(w["id"])["approve"])}">{e(str(w["invoice"]))}</a></td><td>{e(str(w["vendor"]))}</td>'
                f'<td class="num">{e(money(w["amount"]))}</td><td>{e(w["role"])}{" (escalated)" if w["escalated_from"] else ""}</td><td>{w["age_hours"]:.0f} h</td>'
                f'<td>{e(w["next"])} in {w["next_in_hours"]:.0f} h</td><td>{e(short("; ".join(w["reasons"]), 120))}</td></tr>' for w in waiting)
            body.append(f'<div class="scroll"><table><tr><th>Invoice</th><th>Vendor</th><th class="num">Amount</th><th>With</th><th>Age</th><th>Next step</th><th>Why</th></tr>{trs}</table></div>')
        else:
            body.append('<div class="card">Nobody is waiting on a decision.</div>')
        body.append("<h2>All invoices</h2>")
        trs = "".join(
            f'<tr><td><a href="/invoice/{i}">{e(str(o.invoice.invoice_number if o.invoice else Path(o.source_path).name))}</a></td>'
            f'<td>{e(str(o.invoice.vendor or "-") if o.invoice else "-")}</td><td class="num">{e(money(o.invoice.total, o.invoice.currency) if o.invoice and o.invoice.total is not None else "-")}</td>'
            f'<td>{pill(label, kind)}</td><td>{e(short(o.explanation or (o.approval.reasoning if o.approval else o.error) or "", 150))}</td></tr>'
            for i, o, label, kind in shown)
        body.append(f'<div class="scroll"><table><tr><th>Invoice</th><th>Vendor</th><th class="num">Total</th><th>Status</th><th>What happened</th></tr>{trs}</table></div>')
        body.append("<h2>Payments</h2>")
        body.append(f'<div class="card">{len(sent)} sent to the bank ({e(money(sum(Decimal(r["amount"]) for r in sent)))}), '
                    f'{len(sched)} scheduled for later ({e(money(sum(Decimal(r["amount"]) for r in sched)))}).'
                    + "".join(f'<div class="ev">{e(r["bill_key"])}: release {e(r["release_on"])} by {e(r["rail"].upper())}, {e(money(r["amount"]))}</div>' for r in sched) + "</div>")
        return page("Acme AP: dashboard", "".join(body))

    def invoice(self, row_id: int) -> bytes | None:
        o = self.store.get(row_id)
        if o is None:
            return None
        label, kind = self.status(row_id, o)
        inv = o.invoice
        body = [f'<p><a href="/">&larr; Dashboard</a></p><h1>{e(str(inv.invoice_number if inv else Path(o.source_path).name))} {pill(label, kind)}</h1>']
        if not inv:
            return page("Invoice", "".join(body) + f'<div class="card">{e(o.error or "Could not be read.")}</div>')
        body.append(f'<p class="sub">{e(str(inv.vendor or "unknown vendor"))} &middot; {e(money(inv.total, inv.currency) if inv.total is not None else "no total")} '
                    f'&middot; read from {e(Path(o.source_path).name)} by {e(inv.extractor)}</p>')
        if o.explanation or (o.approval and o.approval.reasoning):
            body.append(f'<div class="card"><b>In short</b><br>{e(o.explanation or o.approval.reasoning)}</div>')
        if o.approval:
            body.append(f'<div class="card"><b>Decision</b>: {e(o.approval.decision.value)} after {o.approval.rounds} round(s)'
                        + ("".join(f'<div class="ev">critique: {e(c)}</div>' for c in o.approval.critiques)) + f'<div class="ev">{e(o.approval.reasoning)}</div></div>')
        if o.findings:
            kinds = {"error": "bad", "hold": "wait", "warn": "muted"}
            body.append("<h2>What the checks found</h2><div class='card'><ul class='f'>" + "".join(
                f'<li>{pill(f.severity.value, kinds[f.severity.value])} {e(f.message)}</li>' for f in o.findings) + "</ul></div>")
        if inv.line_items:
            trs = "".join(f'<tr><td>{e(li.item)}{" <span class=ev>(" + e(li.note) + ")</span>" if li.note else ""}</td><td class="num">{li.quantity}</td>'
                          f'<td class="num">{e(money(li.unit_price)) if li.unit_price is not None else "-"}</td></tr>' for li in inv.line_items)
            body.append(f'<h2>Line items</h2><table><tr><th>Item</th><th class="num">Qty</th><th class="num">Unit price</th></tr>{trs}</table>')
        body.append("<h2>What each agent did</h2><div class='card'>")
        for t in o.trace:
            body.append(f'<div class="trace"><span class="who">{e(t["agent"])}</span> {e(t["action"])} <span class="ev">({t["ms"]:.0f} ms)</span>')
            for ev in t.get("events", []):
                k = ev["kind"]
                text = (f'tool <code>{e(ev["name"])}</code>({e(str(ev["args"] or ""))}) &rarr; {e(str(ev["result"]))}' if k == "tool" else
                        f'asked the model as {e(ev["as"])}: {ev["tokens"][0]} tokens in, {ev["tokens"][1]} out' if k == "model" else
                        f'model not asked ({e(ev["as"])}): nothing it could change' if k == "skipped" else f'{e(k)}: {e(ev.get("text", ""))}')
                body.append(f'<div class="ev">&rarr; {text}</div>')
            body.append("</div>")
        body.append("</div>")
        if o.payment_ref:
            body.append(f'<div class="card"><b>Payment</b>: {e(money(o.payment_amount or inv.total))} sent, reference <code>{e(o.payment_ref)}</code></div>')
        elif o.release_on:
            body.append(f'<div class="card"><b>Payment</b>: scheduled for {e(o.release_on)}</div>')
        return page("Invoice", "".join(body))

    def decide(self, token: str) -> tuple[int, bytes]:
        try:
            d = self.approvals.inspect(token)
        except TokenError as exc:
            return 400, page("Link not valid", f'<h1>This link cannot be used</h1><div class="card">{e(str(exc))}. Links expire after five days, work once, '
                                               'and only for the invoice as it was when the request was sent.</div><p><a href="/">Dashboard</a></p>')
        inv, o = d["invoice"], d["outcome"]
        body = [f'<p><a href="/">&larr; Dashboard</a></p><h1>Pay {e(money(inv.total, inv.currency))} to {e(str(inv.vendor or "(vendor missing)"))}?</h1>',
                f'<p class="sub">Invoice {e(str(inv.invoice_number))} &middot; with {e(d["role"])}</p>']
        if d["status"] != "pending":
            body.append(f'<div class="card">This request is already <b>{e(d["status"])}</b>'
                        + (f' by {e(d["decided_by"])}' if d["decided_by"] else "") + ".</div>")
            return 200, page("Decision", "".join(body))
        if o.explanation:
            body.append(f'<div class="card"><b>In short</b><br>{e(o.explanation)}</div>')
        body.append('<div class="card"><b>Why this needs you</b><ul class="f">' + "".join(f"<li>{e(r)}</li>" for r in d["reasons"]) + "</ul></div>")
        body.append('<div class="card kv"><div>Invoice</div><div>' + e(str(inv.invoice_number)) + f'</div><div>Dated / due</div><div>{e(str(inv.date or "?"))} / {e(str(inv.due_date or "?"))}</div>'
                    f'<div>Terms</div><div>{e(str(inv.payment_terms or "?"))}</div><div>Items</div><div>{e(", ".join(f"{li.item} x{li.quantity}" for li in inv.line_items[:6]))}</div>'
                    f'<div>Read by</div><div>{e(inv.extractor)} ({e(inv.lane)})</div></div>')
        notes = [f for f in o.findings if f.severity.value == "warn"]
        if notes:
            body.append('<div class="card"><b>Noted (did not block)</b><ul class="f">' + "".join(f"<li>{e(f.message)}</li>" for f in notes) + "</ul></div>")
        opts = "".join(f'<option value="{e(a)}">{e(a)}</option>' for a in sorted(d["approvers"], key=lambda a: (a != d["primary"], a)))  # the role's own mailbox first
        body.append(f'<form method="post" action="/decide" class="card"><input type="hidden" name="t_approve" value="{e(d["tokens"]["approve"])}">'
                    f'<input type="hidden" name="t_reject" value="{e(d["tokens"]["reject"])}"><label for="who">Deciding as</label><select id="who" name="who">{opts}</select>'
                    '<label for="comment">Comment (optional)</label><textarea id="comment" name="comment" rows="2" maxlength="300"></textarea>'
                    '<div class="actions"><button class="approve" name="decision" value="approve">Approve and pay</button>'
                    '<button class="reject" name="decision" value="reject">Reject</button></div>'
                    '<div class="note">Nothing is paid until you approve. The invoice is re-checked first, so a changed bank account or stock problem still blocks it. '
                    'In this local demo you pick who you are from the people authorised for this request; a real deployment puts single sign-on in front of this page.</div></form>')
        return 200, page("Decision", "".join(body))

    def respond(self, form: dict[str, str]) -> tuple[int, bytes]:
        choice = "approve" if form.get("decision") == "approve" else "reject"
        token = form.get("t_approve" if choice == "approve" else "t_reject", "")
        res = self.approvals.respond(token, form.get("who", ""), form.get("comment", "")[:300])
        kind = "ok" if res.ok else "bad"
        return (200 if res.ok else 400), page("Result", f'<h1>{pill("Done" if res.ok else "Not done", kind)}</h1><div class="card">{e(res.message)}</div>'
                                                        + (f'<div class="card">Payment reference <code>{e(res.payment_ref)}</code></div>' if res.payment_ref else "")
                                                        + '<p><a class="btn primary" href="/">Back to the dashboard</a></p>')


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AcmeAP"

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            try:
                if url.path == "/":
                    return self._send(200, app.dashboard())
                if url.path.startswith("/invoice/") and url.path[9:].isdigit():
                    body = app.invoice(int(url.path[9:]))
                    return self._send(200, body) if body else self._send(404, page("Not found", "<h1>No such invoice</h1>"))
                if url.path == "/decide":
                    code, body = app.decide(urllib.parse.parse_qs(url.query).get("t", [""])[0])
                    return self._send(code, body)
                if url.path == "/api/state":
                    rows = app.store.all()
                    return self._send(200, json.dumps({"invoices": len(rows), "waiting": len(app.approvals.waiting())}).encode(), "application/json")
                self._send(404, page("Not found", "<h1>Not found</h1>"))
            except Exception:  # noqa: BLE001 - a page error must not take the server down, and must not leak internals
                self._send(500, page("Error", "<h1>Something went wrong</h1><p>See the server log.</p>"))
                raise

        def do_POST(self) -> None:  # noqa: N802
            if urllib.parse.urlparse(self.path).path != "/decide":
                return self._send(404, page("Not found", "<h1>Not found</h1>"))
            length = min(int(self.headers.get("Content-Length") or 0), 10_000)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace")).items()}
            code, body = app.respond(form)
            self._send(code, body)

        def log_message(self, fmt: str, *args) -> None:  # quiet by default; the dashboard is the log
            pass
    return Handler


def make_server(db: str | Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(App(db)))


def serve(db: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(db, host, port)
    print(f"Acme AP interface: http://{host}:{server.server_address[1]}/   (Ctrl-C to stop; local only, approver identity is a stand-in for SSO)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
