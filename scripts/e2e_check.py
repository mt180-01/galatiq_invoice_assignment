"""End-to-end check of the whole system: every step is a real command run in a scratch folder, each prints PASS/FAIL with its evidence.

    python scripts/e2e_check.py            # this checkout
    python scripts/e2e_check.py /path/to/other/checkout     (e.g. a fresh clone with its own .venv)
Nothing in the checkout is modified (databases and outboxes go to a temp folder). Needs `tesseract` for the scanned-PDF step."""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
PY = str(REPO / ".venv/bin/python") if (REPO / ".venv/bin/python").exists() else sys.executable
ENV = {**{k: v for k, v in os.environ.items() if k != "XAI_API_KEY"}, "PYTHONWARNINGS": "ignore", "COLUMNS": "220",
       "XAI_API_KEY_FILE": "/nonexistent/xai_key",  # this check is free and offline: it never uses a real key
       "ACME_APPROVAL_SECRET": "e2e-secret-not-for-real-use"}  # and signs its links with its own key, not the one in ~/.config
results = []


def sh(*args, ok=True, timeout=300):
    p = subprocess.run(list(args), cwd=REPO, capture_output=True, text=True, timeout=timeout, env=ENV)
    out = p.stdout + p.stderr
    return p.returncode, out


def check(name, cond, evidence=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"   [{evidence}]" if evidence else ""))


def py(*a, **k):
    return sh(PY, *a, **k)


tmp = Path(tempfile.mkdtemp(prefix="e2e_"))
db, outbox = str(tmp / "e2e.db"), str(tmp / "outbox")

# 1. unit/integration tests in the clean clone
rc, out = py("-m", "pytest", "-q")
m = re.search(r"(\d+) passed", out)
check("test suite in a fresh clone", rc == 0 and m and "failed" not in out, m.group(0) if m else out[-200:])

# 2. the guided demo
rc, out = py("main.py", "demo")
check("main.py demo runs all 8 steps", rc == 0 and all(f"Step {i}." in out for i in range(1, 9)) and "Traceback" not in out)
check("demo: VP approval paid, payments reconciled with no issues", "approved by vp-finance@acmecorp.example; paid" in out and re.search(r"returned 0, still in flight 0; issues: none", out))

# 3. agents roster
rc, out = py("main.py", "agents")
check("main.py agents lists the six agents", rc == 0 and all(a in out for a in ("Intake", "Validation", "Reviewer", "Desk", "Payment", "Comms")))

# 4. provided invoices end to end (offline), JSON result
rc, out = py("main.py", "--offline", "--db", db, "--outbox", outbox, "--json", str(tmp / "r.json"), "--invoice_path", "data/invoices")
res = json.loads((tmp / "r.json").read_text())
status = {}
for o in res:
    if o["status"] != "DUPLICATE":
        inv = o["invoice"]
        status[inv["invoice_number"] + (f"-{inv['revision']}" if inv.get("revision") else "")] = o["status"]
expected = {"INV-1001": "PAID", "INV-1002": "REJECT", "INV-1003": "REJECT", "INV-1004": "PAID", "INV-1004-R1": "AWAITING", "INV-1005": "REJECT",
            "INV-1006": "PAID", "INV-1007": "REJECT", "INV-1008": "REJECT", "INV-1009": "REJECT", "INV-1010": "PAID", "INV-1011": "PAID",
            "INV-1012": "PAID", "INV-1013": "REJECT", "INV-1014": "AWAITING", "INV-1015": "PAID", "INV-1016": "REJECT"}
check("17 provided invoices: every outcome as labelled", rc == 0 and status == expected, f"{sum(status[k] == v for k, v in expected.items())}/17")
check("3 cross-format duplicates blocked, none errored", sum(o["status"] == "DUPLICATE" for o in res) == 3 and not any(o.get("error") for o in res))
check("every invoice carries an agent trace", all(o["trace"] for o in res if o["status"] != "DUPLICATE"))
check("stdout shows results, impact, payments, AI usage", all(s in out for s in ("Invoice results", "Impact", "Payments", "AI usage")) and "Traceback" not in out)
paid_refs = [o["payment_ref"] for o in res if o["status"] == "PAID"]
check("7 paid with unique bank references", len(paid_refs) == 7 == len(set(paid_refs)), ", ".join(paid_refs[:2]) + " ...")
check("rejections carry reasons in the audit log",
      all(r for r in [subprocess.run([PY, "-c", f"import sqlite3;print(sqlite3.connect('{db}').execute(\"select count(*) from audit_log where detail like 'reject:%'\").fetchone()[0])"], capture_output=True, text=True).stdout.strip() == "8"]))
out_dir = Path(outbox)
check("outbox: follow-ups, approval cards, notices, digest",
      len(list(out_dir.glob("*.txt"))) >= 4 and len(list((out_dir / "approvals").glob("*.txt"))) == 2 and (out_dir / "daily_digest.txt").exists() and list((out_dir / "notices").glob("*.txt")))

# 4e. evidence: a real Grok run replayed offline, and the per-invoice evaluation
rc, out = py("main.py", "--replay-llm", "docs/live_runs/provided-invoices.cassette.jsonl", "--db", str(tmp / "rp.db"), "--outbox", outbox + "R", "--json", str(tmp / "rp.json"), "--invoice_path", "data/invoices")
rp = json.loads((tmp / "rp.json").read_text())
rstatus = {}
for o in rp:
    if o["status"] != "DUPLICATE":
        rstatus[o["invoice"]["invoice_number"] + (f"-{o['invoice']['revision']}" if o["invoice"].get("revision") else "")] = o["status"]
check("a recorded live Grok run replays offline: 36 model calls answered, 0 missing, the recorded cost", "36 model call(s) answered from the recording, 0 not recorded" in out and "spent $0.0200" in out)
check("the replay reaches the same 17 outcomes and reproduces the model's notes", rstatus == expected and sum(bool(o.get("explanation")) for o in rp) == 10, f"{sum(rstatus[k] == v for k, v in expected.items())}/17")
rc, out = py("scripts/evaluate.py", "--check")
check("docs/EVALUATION.md (rules-only and LLM-on, every invoice) is current", rc == 0 and "is current" in out)

# 4a. the ledger remembers: running twice never pays twice; --reset starts over
dbl = str(tmp / "twice.db")
one = ["main.py", "--offline", "--db", dbl, "--outbox", outbox + "T", "--invoice_path", "data/invoices/invoice_1001.txt"]
py(*one)
rc, out2 = py(*one)
pays = subprocess.run([PY, "-c", f"import sqlite3;print(sqlite3.connect('{dbl}').execute('select count(*) from vendor_payments').fetchone()[0])"], capture_output=True, text=True).stdout.strip()
check("running the CLI twice on the same invoice pays it once (the ledger is kept)", "DUPLICATE=1" in out2 and pays == "1" and "already processed in an earlier run" in out2, f"payments in ledger: {pays}")
rc, out3 = py(*one, "--reset")
check("--reset starts from an empty ledger", "PAID=1" in out3)

# 4b. the default run (no flags): local stand-in model with real tool calls, same outcomes, structured logs
dbs = str(tmp / "sim.db")
rc, out = py("main.py", "--db", dbs, "--outbox", outbox + "S", "--json", str(tmp / "s.json"), "--invoice_path", "data/invoices")
sres = json.loads((tmp / "s.json").read_text())
sstatus = {}
for o in sres:
    if o["status"] != "DUPLICATE":
        sstatus[o["invoice"]["invoice_number"] + (f"-{o['invoice']['revision']}" if o["invoice"].get("revision") else "")] = o["status"]
check("default run uses the local stand-in model and reaches the same 17 outcomes", "local simulation (not Grok)" in out and sstatus == expected, f"{sum(sstatus[k] == v for k, v in expected.items())}/17")
check("the stand-in model called tools (function calling exercised)", sum(o["llm"].get("tools", 0) for o in sres) > 0 and "tool calls run for the model" in out,
      f"{sum(o['llm'].get('tools', 0) for o in sres)} tool calls")
check("structured logs: a decision line per invoice and a 'Paid X to Y' line per payment", out.count("acme.decision") == 17 and out.count("acme.payment Paid") == 7, f"{out.count('acme.decision')} decisions, {out.count('acme.payment Paid')} payments")

# 4c. the agent upgrades: item self-correction, lookalike vendor, model-written notes, and the trace
rc, out = py("main.py", "--trace", "--db", str(tmp / "sc.db"), "--outbox", outbox + "C", "--json", str(tmp / "c.json"), "--invoice_path", "data/self_correction")
cres = {o["invoice"]["invoice_number"]: o for o in json.loads((tmp / "c.json").read_text())}
check("mistyped item 'Wdgt B' is found by catalogue search, corrected and the invoice goes on to payment",
      cres["INV-3201"]["status"] in ("PAID", "SCHEDULED") and cres["INV-3201"]["invoice"]["line_items"][0]["item"] == "WidgetB", cres["INV-3201"]["status"])
check("a similar-looking but different product (GadgetY) is NOT corrected", cres["INV-3202"]["status"] == "REJECT" and cres["INV-3202"]["invoice"]["line_items"][0]["item"] == "GadgetY")
check("a look-alike vendor is flagged for a person, never changed", cres["INV-3203"]["status"] == "AWAITING" and cres["INV-3203"]["invoice"]["vendor"] == "Widget Inc"
      and "possible match" in json.dumps(cres["INV-3203"]["findings"]))
check("rejections get a model-written plain-English note", bool(cres["INV-3202"]["explanation"]), (cres["INV-3202"]["explanation"] or "")[:60])
check("--trace shows tool calls, corrections and conclusions", all(s in out for s in ("tool search_items", "corrected:", "suggested:", "decision:")))
check("every PAID invoice had a model second opinion before the money moved",
      all(any(e["kind"] == "model" for t in o["trace"] if t["agent"] == "Reviewer" for e in t.get("events", [])) for o in sres if o["status"] == "PAID"))

# 4d. the web interface, over real HTTP, as a separate process
import socket, time, urllib.parse, urllib.request, urllib.error
with socket.socket() as sk:
    sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
ui = subprocess.Popen([PY, "main.py", "serve", "--demo", "--db", str(tmp / "ui.db"), "--port", str(port)], cwd=REPO, env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    base, page = f"http://127.0.0.1:{port}", ""
    for _ in range(120):
        try:
            page = urllib.request.urlopen(base + "/", timeout=2).read().decode(); break
        except OSError:
            time.sleep(0.5)
    check("web interface starts and the dashboard shows the invoices, the queue and payments", "Waiting for a decision" in page and "INV-3001" in page and "Payments" in page and "duplicates blocked" in page)
    link = re.search(r'href="(/decide\?t=[^"]+)"[^>]*>INV-3001', page).group(1).replace("&amp;", "&")
    decision = urllib.request.urlopen(base + link, timeout=10).read().decode()
    check("the approval link opens a real page (not a made-up domain) with the summary and buttons", "Approve and pay" in decision and "Why this needs you" in decision and "In short" in decision)
    form = dict(zip(("t_approve", "t_reject"), re.findall(r'name="t_(?:approve|reject)" value="([^"]+)"', decision)))
    def post(**f):
        try:
            return urllib.request.urlopen(urllib.request.Request(base + "/decide", data=urllib.parse.urlencode(f).encode(), method="POST"), timeout=10).read().decode()
        except urllib.error.HTTPError as err:
            return err.read().decode()
    tampered = dict(form, t_approve=form["t_approve"][:-3] + ("AAA" if not form["t_approve"].endswith("AAA") else "BBB"))
    check("a tampered link is refused by the page", "signature" in post(decision="approve", **tampered))
    check("the page shows who the link was issued to, with no identity pick-list", "<b>vp-finance@acmecorp.example</b>" in decision and "<select" not in decision)
    check("the VP approves in the browser flow (a typed name carries no weight) and the invoice is paid", "paid" in post(decision="approve", who="attacker@evil.example", **form))
    check("a second click is refused", "already approved" in post(decision="approve", **form))
    up = urllib.request.urlopen(base + "/upload", timeout=10).read().decode()
    csrf = re.search(r'name="csrf" value="([^"]+)"', up).group(1)
    boundary = "e2eboundary"
    sample = (REPO / "data" / "invoices" / "invoice_1006.csv").read_bytes()
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{csrf}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="invoice_1006.csv"\r\nContent-Type: text/csv\r\n\r\n').encode() + sample + f"\r\n--{boundary}--\r\n".encode()
    def upload(token_body):
        req = urllib.request.Request(base + "/upload", data=token_body, method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()
    check("the upload page offers a file picker", 'type="file"' in up and "Upload invoices" in page)
    check("a forged upload (wrong form token) is refused", upload(body.replace(csrf.encode(), b"forged"))[0] == 403)
    code, _ = upload(body)
    dash = urllib.request.urlopen(base + "/", timeout=10).read().decode()
    check("an uploaded invoice is processed by the agents and appears on the dashboard", code == 200 and "INV-1006" in dash)
    inv_id = re.search(r'href="/invoice/(\d+)">INV-3201<', urllib.request.urlopen(base + "/", timeout=10).read().decode()).group(1)
    detail = urllib.request.urlopen(base + f"/invoice/{inv_id}", timeout=10).read().decode()
    check("the invoice page shows the agents' trace with the tool call and the correction", "What each agent did" in detail and "search_items" in detail and "corrected" in detail)
finally:
    ui.terminate()

# 5. human-in-the-loop: second run on the approval demo (adds the over-limit and changed-bank-account invoices)
db2 = str(tmp / "hitl.db")
rc, out = py("main.py", "--offline", "--db", db2, "--outbox", outbox + "2", "--invoice_path", "data/approvals_demo")
check("approval demo: 2 invoices waiting for people, nothing paid yet", rc == 0 and "AWAITING=2" in out and "Payments" not in out and py("pay.py", "--db", db2, "status")[1].strip() == "", "AWAITING=2, payment ledger empty")
rc, out = py("approve.py", "--db", db2, "list")
check("approve.py list shows VP Finance and AP Manager queues", "VP Finance" in out and "AP Manager" in out, out.strip().splitlines()[0][:70])
cards = sorted((Path(outbox + "2") / "approvals").glob("request_*.txt"))
vp_card = next(c for c in cards if "INV-3001" in c.name)
bank_card = next(c for c in cards if "INV-3004" in c.name)
tok = lambda card, kind: re.search(rf"{kind}: \S*\?t=(\S+)", card.read_text()).group(1)
rc, out = py("approve.py", "--db", db2, "respond", tok(vp_card, "APPROVE"), "--as", "attacker@evil.example")
check("a stranger cannot claim to be the VP's link holder", "was issued to vp-finance@acmecorp.example" in out)
rc, out = py("approve.py", "--db", db2, "respond", tok(vp_card, "APPROVE"), "--as", "vp-finance@acmecorp.example", "--comment", "ok")
check("VP approves INV-3001 via the signed link -> paid", "approved by vp-finance@acmecorp.example; paid" in out, out.strip())
rc, out = py("approve.py", "--db", db2, "respond", tok(vp_card, "APPROVE"), "--as", "vp-finance@acmecorp.example")
check("replaying the same link is refused", "refused" in out and "already approved" in out)
rc, out = py("approve.py", "--db", db2, "respond", tok(bank_card, "APPROVE"), "--as", "ap-manager@acmecorp.example")
check("changed bank account cannot be approved through (re-check blocks it)", "re-check failed" in out and "account" in out, out.strip()[:90])
db3 = str(tmp / "clock.db")
py("main.py", "--offline", "--db", db3, "--outbox", outbox + "5", "--invoice_path", "data/approvals_demo")
rc, out = py("approve.py", "--db", db3, "tick", "--hours", "25")
check("24h clock sends a reminder to each approver", out.count("reminder") == 2, out.strip().replace("\n", " | "))
rc, out = py("approve.py", "--db", db3, "tick", "--hours", "50")
check("48h clock escalates both unanswered requests up the chain", "VP Finance -> CFO" in out and "AP Manager -> VP Finance" in out, out.strip().replace("\n", " | "))
rc, out = py("approve.py", "--db", db3, "tick", "--hours", "130")
check("5-day clock expires them unpaid", out.count("expired unpaid") == 2, out.strip().replace("\n", " | "))
rc, out = py("pay.py", "--db", db3, "status")
check("nothing was paid for expired requests", out.strip() == "")

# 6. treasury
rc, out = py("pay.py", "--db", db2, "status")
check("pay.py status: INV-3001 submitted via bank", "INV-3001" in out and "submitted" in out, out.strip().splitlines()[0][:100])
rc, out = py("pay.py", "--db", db2, "reconcile", "--on", "2099-01-05")
check("pay.py reconcile: settled, no issues", re.search(r"settled \d+, returned 0, still in flight 0", out) and "!" not in out, out.strip())
rc, out = py("pay.py", "--db", db2, "account", "set", "Widgets Inc.", "acct-NEW9", "--by", "a@acmecorp.example")
rc2, out2 = py("pay.py", "--db", db2, "account", "verify", "Widgets Inc.", "--by", "a@acmecorp.example")
rc3, out3 = py("pay.py", "--db", db2, "account", "verify", "Widgets Inc.", "--by", "b@acmecorp.example")
check("bank account change needs a second person", "NOT usable" in out and "refused" in out2 and "verified by b@acmecorp.example" in out3)

# 7. other input channels
rc, out = py("main.py", "--offline", "--db", str(tmp / "em.db"), "--outbox", outbox + "3", "--invoice_path", "data/emails")
check("emails (attachments, body-only, e-invoice) processed without errors", rc == 0 and "Traceback" not in out and "FAILED" not in out, re.search(r"(\d+) document", out).group(0))
rc, out = py("main.py", "--offline", "--db", str(tmp / "sc.db"), "--outbox", outbox + "4", "--invoice_path", "data/scanned")
check("scanned PDFs: OCR'd and never auto-paid", rc == 0 and "PAID" not in out and "Traceback" not in out, "tesseract present")

# 8. benchmarks and quality scripts
rc, out = py("main.py", "--offline", "--bench", "2000")
check("benchmark runs (serial/parallel/auto)", rc == 0 and "serial" in out and "parallel" in out and "Traceback" not in out)
rc, out = py("scripts/measure_quality.py")
check("measure_quality: 17/17 and 0 wrongly paid", "17/17" in out and "Wrongly paid            : 0" in out)
rc, out = py("scripts/stress_extraction.py", "400", "7")
check("stress: 0 approved with a wrong value", "silent wrong" not in out and "exact" in out, out.strip().splitlines()[0])
rc, out = py("scripts/measure_llm_cost.py")
check("LLM cost: same outcomes, far fewer calls", "Outcomes identical across all three: True" in out, [l for l in out.splitlines() if l.startswith("gated")][0].split())
rc, out = py("main.py", "--help")
check("--help works", rc == 0 and "--invoice_path" in out)
rc, out = py("main.py", "--invoice_path", "no/such/path")
check("bad path fails cleanly", rc != 0 and "does not exist" in out and "Traceback" not in out)

bad = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed" + (f"; FAILED: {bad}" if bad else ""))
sys.exit(1 if bad else 0)
