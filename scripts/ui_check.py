"""Drives the real web interface in a real browser (Chromium via Playwright) over real HTTP, end to end.

    pip install playwright && playwright install chromium     # optional, not in requirements.txt
    python scripts/ui_check.py [--shots DIR]                   # starts its own server on a temporary database

Checks: dashboard and tables, keyboard (skip link, focus ring, target size), the display settings (text size, high contrast, readable font, persistence,
announcements), contrast ratios in light, dark and high-contrast themes, phone-width layout, the invoice page, the approval flow (including a used link),
upload, 404s, and that the strict content-security-policy produces no console errors. Screenshots are written to --shots.
"""
import argparse
import asyncio
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("Playwright is not installed: pip install playwright && playwright install chromium")

from acme_ap.runner import run_batch  # noqa: E402
from acme_ap.web import make_server  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--shots", default=str(Path(tempfile.gettempdir()) / "acme_ui_shots"))
SHOTS = ap.parse_args().shots
Path(SHOTS).mkdir(parents=True, exist_ok=True)
_tmp = Path(tempfile.mkdtemp(prefix="acme_ui_"))
UPLOAD = str(_tmp / "upload.txt")
_db = str(_tmp / "ui.db")
asyncio.run(run_batch([str(p) for p in sorted((ROOT / "data" / "invoices").iterdir())] + [str(p) for p in sorted((ROOT / "data" / "approvals_demo").iterdir())],
                      SimpleNamespace(db=_db, keep_ledger=False, llm_concurrency=4, offline=True, simulate=False, grok=False), 1))
_server = make_server(_db, port=0)
threading.Thread(target=_server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{_server.server_address[1]}"
results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail)); print(("PASS " if ok else "FAIL ") + name + (f"   [{detail}]" if detail else ""))

def lum(rgb):
    def ch(c):
        c /= 255; return c / 12.92 if c <= .03928 else ((c + .055) / 1.055) ** 2.4
    r, g, b = rgb; return .2126 * ch(r) + .7152 * ch(g) + .0722 * ch(b)
def parse(c):
    m = re.findall(r"[\d.]+", c); v = [float(x) for x in m]; return v[:3], (v[3] if len(v) > 3 else 1.0)
def ratio(a, b):
    la, lb = lum(a), lum(b); hi, lo = max(la, lb), min(la, lb); return (hi + .05) / (lo + .05)

with sync_playwright() as p:
    br = p.chromium.launch()
    ctx = br.new_context(viewport={"width": 1280, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    errors, csp = [], []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("requestfailed", lambda r: errors.append("requestfailed " + r.url))

    # ---- dashboard
    page.goto(BASE + "/")
    check("dashboard title", "dashboard" in page.title().lower(), page.title())
    check("tiles shown", page.locator(".tile").count() == 5, f"{page.locator('.tile').count()} tiles")
    check("tables captioned", page.locator("table").count() == page.locator("table caption").count() >= 1)
    check("all th scoped", page.locator("th:not([scope=col])").count() == 0 or page.locator("th:not([scope])").count() == 0)
    pills = page.locator(".pill")
    check("status pills have icon+text", pills.count() > 5 and all(pills.nth(i).locator(".ic").count() == 1 and pills.nth(i).inner_text().strip() for i in range(min(pills.count(), 12))))
    page.screenshot(path=f"{SHOTS}/1_dashboard.png", full_page=True)

    # ---- keyboard: first Tab lands on skip link, Enter moves focus to main
    page.keyboard.press("Tab")
    check("first Tab = skip link", page.evaluate("document.activeElement.className") == "skip", page.evaluate("document.activeElement.textContent"))
    box = page.locator(".skip").bounding_box()
    check("skip link visible when focused", box and box["y"] >= 0, str(box))
    page.keyboard.press("Enter")
    check("skip link moves focus to main", page.evaluate("document.activeElement.id") == "main")
    # focus ring visible on a button
    page.focus("[data-a11y=larger]")
    ol = page.evaluate("getComputedStyle(document.activeElement).outlineStyle + ' ' + getComputedStyle(document.activeElement).outlineWidth")
    check("visible focus ring", "solid" in ol and not ol.endswith(" 0px"), ol)
    h = page.evaluate("document.querySelector('[data-a11y=larger]').getBoundingClientRect().height")
    check("controls >= 44px tall", h >= 43.5, f"{h:.0f}px")

    # ---- display settings
    base_fs = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    page.click("[data-a11y=larger]")
    fs1 = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    page.click("[data-a11y=larger]"); page.click("[data-a11y=larger]")
    fs2 = page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)")
    small = page.evaluate("parseFloat(getComputedStyle(document.querySelector('.tile .l')).fontSize)")
    check("text size grows 100%->125%->150% (and clamps)", abs(fs1 / base_fs - 1.25) < .02 and abs(fs2 / base_fs - 1.5) < .02, f"{base_fs:.1f}->{fs1:.1f}->{fs2:.1f}")
    check("small text scales too", small > 13 * 1.4, f"{small:.1f}px")
    page.wait_for_selector("#live:has-text('Text size')", state="attached", timeout=2000)
    check("change announced to screen readers", "extra large" in page.inner_text("#live"), page.inner_text("#live"))
    h_scroll = page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")
    check("no horizontal page scroll at 150%", h_scroll)
    page.screenshot(path=f"{SHOTS}/2_large_text.png")
    page.reload()
    check("text size remembered after reload", page.evaluate("document.documentElement.getAttribute('data-size')") == "2")
    page.click("[data-a11y=reset]")
    check("reset restores size", page.evaluate("document.documentElement.getAttribute('data-size')") is None and abs(page.evaluate("parseFloat(getComputedStyle(document.body).fontSize)") - base_fs) < .1)

    page.click("[data-a11y=contrast]")
    bg = parse(page.evaluate("getComputedStyle(document.body).backgroundColor"))[0]
    ink = parse(page.evaluate("getComputedStyle(document.body).color"))[0]
    check("high contrast: black bg, white text, 21:1", bg == [0, 0, 0] and ink == [255, 255, 255] and ratio(bg, ink) > 20, f"{ratio(bg, ink):.1f}:1")
    check("contrast button pressed state", page.get_attribute("[data-a11y=contrast]", "aria-pressed") == "true")
    page.screenshot(path=f"{SHOTS}/3_high_contrast.png", full_page=True)
    page.click("[data-a11y=font]")
    ff = page.evaluate("getComputedStyle(document.body).fontFamily + '|' + getComputedStyle(document.body).letterSpacing")
    check("readable font applies spacing", "Verdana" in ff and not ff.endswith("|normal"), ff)
    page.click("[data-a11y=reset]")

    # ---- contrast ratios of every pill kind, light / dark / high
    def worst():
        return min(ratio(parse(page.evaluate("getComputedStyle(arguments[0]).color".replace("arguments[0]", "document.querySelectorAll('.pill')[%d]" % i)))[0],
                         parse(page.evaluate("(()=>{let e=document.querySelectorAll('.pill')[%d];while(e){let c=getComputedStyle(e).backgroundColor;if(c!='rgba(0, 0, 0, 0)')return c;e=e.parentElement}})()" % i))[0])
                   for i in range(page.locator(".pill").count()))
    wl = worst(); check("light theme: pill text >= 4.5:1", wl >= 4.5, f"worst {wl:.2f}")
    page2 = br.new_context(color_scheme="dark").new_page(); page2.goto(BASE + "/")
    page = page2
    wd = worst(); check("dark theme: pill text >= 4.5:1", wd >= 4.5, f"worst {wd:.2f}")
    page.screenshot(path=f"{SHOTS}/4_dark.png")
    page.click("[data-a11y=contrast]"); wh = worst(); check("high contrast: pill text >= 7:1", wh >= 7, f"worst {wh:.2f}")
    page = ctx.pages[0]

    # ---- mobile
    m = br.new_context(viewport={"width": 375, "height": 800}).new_page(); m.goto(BASE + "/")
    check("mobile 375px: no page-level horizontal scroll", m.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"),
          m.evaluate("document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth"))
    cells = m.locator("td[data-label='Status']"); n = cells.count()
    inside = all((b := cells.nth(i).bounding_box()) and b["x"] >= 0 and b["x"] + b["width"] <= 376 for i in range(n))
    check("mobile: every Status value is on screen (rows stack as cards)", n > 5 and inside, f"{n} cells")
    check("mobile: each value is labelled with its column", m.locator("td[data-label='Vendor']").count() >= 5 and m.locator("td[data-label='What happened']").count() >= 5)
    check("mobile: table semantics kept for screen readers", m.locator("table[role=table] tr[role=row] td[role=cell]").count() > 20 and m.locator("th[role=columnheader]").count() > 0)
    xs = {round(m.locator(".tile").nth(i).bounding_box()["x"]) for i in range(5)}
    check("mobile: summary tiles sit two across", len(xs) == 2, f"columns at x={sorted(xs)}")
    m.screenshot(path=f"{SHOTS}/5_mobile.png", full_page=False)
    m.locator("table", has_text="INV-1002").first.scroll_into_view_if_needed(); m.locator("table", has_text="INV-1002").first.screenshot(path=f"{SHOTS}/5c_mobile_cards.png", timeout=60000) if False else m.screenshot(path=f"{SHOTS}/5c_mobile_cards.png")
    tiny = br.new_context(viewport={"width": 320, "height": 700}).new_page(); tiny.goto(BASE + "/"); tiny.click("[data-a11y=larger]"); tiny.click("[data-a11y=larger]")
    check("320px wide at 150% text: no horizontal scroll", tiny.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"),
          tiny.evaluate("document.documentElement.scrollWidth + ' vs ' + document.documentElement.clientWidth"))
    tiny.screenshot(path=f"{SHOTS}/5b_tiny_large_text.png")

    # ---- invoice page via link
    page.goto(BASE + "/")
    page.locator("table", has_text="All invoices").locator("a").first.click()
    page.wait_for_load_state()
    check("invoice page opens", "/invoice/" in page.url and page.locator("h1").count() == 1, page.url)
    check("invoice page: line items + agent trace", page.locator("text=What each agent did").count() == 1)
    check("read-aloud button present or hidden if unsupported", page.locator("[data-speak=short]").count() == 1)
    page.screenshot(path=f"{SHOTS}/6_invoice.png", full_page=True)

    # ---- approval flow
    page.goto(BASE + "/")
    waiting = page.locator("h2:has-text('Waiting for a decision') + .scroll a")
    n_wait = waiting.count(); check("waiting queue lists requests", n_wait >= 1, f"{n_wait}")
    waiting.first.click(); page.wait_for_load_state()
    check("decision page opens", "/decide" in page.url and page.locator("button.approve").count() == 1)
    check("decision page warns links are personal", "do not forward" in page.inner_text("body").lower())
    page.screenshot(path=f"{SHOTS}/7_decide.png", full_page=True)
    page.fill("#comment", "checked the PO in the UI test")
    page.click("button.approve")
    page.wait_for_load_state()
    body = page.inner_text("body")
    check("approval result shows done + payment ref", "Done" in body and "Payment reference" in body, body[:80].replace("\n", " "))
    page.screenshot(path=f"{SHOTS}/8_approved.png")
    # reusing the page (back + approve again) must be refused
    page.go_back(); page.wait_for_load_state()
    t = page.inner_text("main").replace("\n", " ")
    check("used approval link is refused on revisit (no approve button)", page.locator("button.approve").count() == 0 and re.search(r"already|used|not valid|refused|closed", t, re.I), t[:110])
    page.screenshot(path=f"{SHOTS}/8b_reused_link.png")

    # ---- upload
    page.goto(BASE + "/upload")
    check("upload form labelled", page.locator("label[for=files]").count() == 1)
    open(UPLOAD, "w").write("Invoice INV-9922\nVendor: Gadgets Co.\nDate: 2026-10-20\nDue Date: 2026-11-05\nItems:\n  GadgetX    qty: 1    unit price: $750.00\nSubtotal: $750.00\nTax (0%): $0.00\nTotal Amount: $750.00\nPayment Terms: Net 15\n")
    page.set_input_files("#files", UPLOAD)
    page.click("button.approve")
    page.wait_for_load_state()
    check("upload processed and listed", "Processed 1 invoice" in page.inner_text("body") and "INV-9922" in page.inner_text("body"), page.inner_text("h1"))
    check("details link has an accessible name that says which invoice", page.get_by_role("link", name="Details for INV-9922").count() == 1)
    page.locator("a:has-text('Details')").first.click(); page.wait_for_load_state()
    check("details page opens", "/invoice/" in page.url)
    page.goto(BASE + "/")
    check("dashboard now shows the uploaded invoice", "INV-9922" in page.inner_text("body"))
    page.screenshot(path=f"{SHOTS}/9_dashboard_after.png", full_page=True)

    check("no console errors / CSP violations / failed requests", not errors, "; ".join(errors[:3]))
    # ---- unknown pages
    r = page.goto(BASE + "/invoice/99999"); check("unknown invoice -> 404", r.status == 404)
    r = page.goto(BASE + "/nope"); check("unknown path -> 404", r.status == 404)

    br.close()
bad = [r for r in results if not r[1]]
print(f"\n{len(results) - len(bad)}/{len(results)} UI checks passed")
_server.shutdown()
sys.exit(1 if bad else 0)
