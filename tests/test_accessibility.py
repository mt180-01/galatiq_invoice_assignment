"""Accessibility: the web pages work with a keyboard and a screen reader, status never depends on colour, and the command line has a plain mode."""
import base64
import hashlib
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from acme_ap import web
from tests.test_web import ui  # noqa: F401 - the running server fixture

ROOT = Path(__file__).resolve().parent.parent


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode(), dict(r.headers)


def test_every_page_has_the_landmarks_a_screen_reader_and_keyboard_need(ui):  # noqa: F811
    base, _ = ui
    for path in ("/", "/upload", "/invoice/1"):
        html, _ = get(base + path)
        assert '<html lang="en">' in html and 'href="#main"' in html and '<main id="main"' in html and '<nav aria-label="Main">' in html.replace(' style="display:flex;gap:16px"', "")
        assert 'role="status" aria-live="polite"' in html                                  # changes to the display settings are announced
        assert "<th>" not in html and "<th class" not in html.replace('<th scope="col" class', "")   # every header cell is scoped to its column
    dash, _ = get(base + "/")
    assert dash.count("<table") == dash.count("<caption")                                    # every table is named


def test_status_is_a_word_and_a_symbol_never_colour_alone(ui):  # noqa: F811
    base, _ = ui
    dash, _ = get(base + "/")
    pills = re.findall(r'<span class="pill (\w+)"><span class="ic" aria-hidden="true">(.*?)</span>(.+?)</span>', dash)
    assert pills and all(icon and label.strip() for _, icon, label in pills)
    assert len({icon for kind, icon, _ in pills}) > 1


def test_the_display_controls_exist_and_the_script_is_allowed_by_hash_only(ui):  # noqa: F811
    base, _ = ui
    html, headers = get(base + "/")
    for control in ("smaller", "larger", "contrast", "font", "reset"):
        assert f'data-a11y="{control}"' in html
    assert 'data-speak="tiles"' in html
    csp = headers["Content-Security-Policy"]
    digest = base64.b64encode(hashlib.sha256(web.SCRIPT.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert html.count("<script>") == 1 and f"<script>{web.SCRIPT}</script>" in html           # exactly the hashed script, nothing else can run


def test_the_style_sheet_honours_the_users_own_settings():
    css = web.CSS
    for needed in (":focus-visible", "prefers-reduced-motion", "prefers-contrast:more", "forced-colors", 'data-size="2"', 'data-contrast="high"', 'data-font="readable"', ".skip", ".sr-only"):
        assert needed in css, needed
    assert not re.search(r"font-size:[\d.]+px", css)                                          # sizes are relative, so the text-size control scales everything
    assert "min-height:44px" in css                                                           # touch and pointer targets


def run_cli(tmp_path, *extra, env=None):
    e = {**os.environ, "PYTHONWARNINGS": "ignore", "COLUMNS": "100", **(env or {})}
    return subprocess.run([sys.executable, "main.py", "--db", str(tmp_path / "a.db"), "--outbox", str(tmp_path / "o"), "--reset", "--invoice_path", "data/invoices", *extra],
                          cwd=ROOT, capture_output=True, text=True, env=e, timeout=180)


def test_plain_mode_is_linear_sentences_without_tables_colour_or_box_drawing(tmp_path):
    for p in (run_cli(tmp_path, "--plain"), run_cli(tmp_path, env={"ACME_PLAIN": "1"})):
        out = p.stdout + p.stderr
        assert p.returncode == 0
        assert not re.search(r"[│┃┏┓┗┛━─┡┩╇]", out) and "\x1b[" not in out
        assert re.search(r"Invoice file invoice_1001\.txt, vendor Widgets Inc\., total USD 5000\.00: PAID\.", out)
        assert re.search(r"Invoice file invoice_1002\.txt, .*: REJECT\. .*GadgetX", out)
        assert "Totals:" in out and "7 PAID" in out


def test_the_default_output_is_unchanged(tmp_path):
    out = (lambda p: p.stdout + p.stderr)(run_cli(tmp_path))
    assert "Invoice results" in out and "Totals:" not in out


def test_tables_stack_into_labelled_cards_on_a_phone_without_losing_their_meaning(ui):  # noqa: F811
    """Found in a real browser at 375px: Status and "What happened" were off-screen inside a sideways-scrolling table."""
    base, _ = ui
    dash, _ = get(base + "/")
    assert "max-width:600px" in web.CSS and "tr.hd" in web.CSS and "attr(data-label)" in web.CSS and "minmax(140px" in web.CSS
    rows = re.findall(r'<tr role="row">(.*?)</tr>', dash, re.S)
    assert rows and all(r.count('role="cell"') == r.count("<td") for r in rows)           # explicit roles: stacked layout must not erase the table for a screen reader
    assert 'data-label="Status"' in dash and 'data-label="What happened"' in dash and 'data-label="Vendor"' in dash
    assert '<tr role="row" class="hd">' in dash and 'role="columnheader"' in dash          # the header row is visually hidden when stacked, never removed
