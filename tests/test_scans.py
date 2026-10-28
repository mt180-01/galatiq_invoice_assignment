import asyncio
from pathlib import Path

import numpy as np
import pymupdf
import pytest

from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import heuristic, ocr, parse_file
from acme_ap.llm import OfflineLLM
from acme_ap.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
SCANS = ROOT / "data" / "scanned"
needs_tesseract = pytest.mark.skipif(not ocr.available(), reason="tesseract not installed")


def test_clean_page_flattens_uneven_lighting():
    h, w = 256, 320
    paper = np.linspace(255, 150, w, dtype=np.float32)[None, :].repeat(h, axis=0)  # shadow across the page
    page = paper.copy()
    for x in range(40, 280, 30):  # short dark strokes, like glyphs (a long solid bar would be treated as a table rule)
        page[100:112, x:x + 20] = paper[100:112, x:x + 20] * 0.25
    out = ocr.clean_page(page.astype(np.uint8))
    assert (out[:90] == 255).all() and (out[120:] == 255).all()  # the shadowed paper became uniformly white
    assert (out[103:109, 45:55] == 0).all() and (out[103:109, 75:85] == 0).all()  # the ink survived


def test_table_rules_are_removed_but_glyph_strokes_survive():
    img = np.full((200, 300), 255, dtype=np.uint8)
    img[50, 10:290] = 0      # horizontal rule
    img[20:180, 150] = 0     # vertical rule
    img[120:135, 60:63] = 0  # a 15 px "letter" stroke
    out = ocr._remove_rules(img)
    assert (out[50] == 255).all() and (out[20:180, 150] == 255).all()
    assert (out[120:135, 60:63] == 0).all()


def test_otsu_separates_two_levels():
    img = np.concatenate([np.full(500, 30, np.uint8), np.full(500, 220, np.uint8)])
    assert 30 <= ocr._otsu(img) < 220


def test_extractor_survives_ocr_noise_lines():
    inv = heuristic.extract(":\n  :  \n— : —\nInvoice: INV-1234\nTotal: $10.00\n")
    assert inv.invoice_number == "INV-1234"


@needs_tesseract
def test_scanned_invoice_is_never_auto_paid_and_vendor_not_asked(tmp_path):
    erp = MockNetSuite(tmp_path / "t.db")
    outs = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1).run(
        [str(p) for p in sorted(SCANS.glob("*.pdf"))]))
    for o in outs:
        assert o.invoice.extractor == "heuristic:pdf-ocr"
        assert o.status == "ESCALATE" and not o.paid and o.followup is None
        assert "ocr_source" in {f.code for f in o.findings}


def test_unreadable_scan_without_ocr_is_held_with_one_plain_ask(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "available", lambda: False)
    erp = MockNetSuite(tmp_path / "t.db")
    (o,) = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, origins=None).run([str(next(SCANS.glob("*1011.pdf")))]))
    assert [f.code for f in o.findings] == ["unreadable_document"] and o.status == "ESCALATE"
    assert o.followup and "resend" in o.followup and o.followup.count("\n  ") == 1


# ---- adaptive resolution: found by the independent scan evaluation (9 pt text at a fixed 200 dpi was unreadable) --------------------------------
def fake_page():
    import pymupdf
    return pymupdf.open().new_page(width=200, height=200)


def stub_recognise(monkeypatch, confidence_by_dpi):
    from acme_ap.ingestion import ocr
    calls = []

    def fake(gray, dpi=ocr.DPI):
        calls.append(dpi)
        return [(0, 0, 1, 1, f"read-at-{dpi}")], confidence_by_dpi[dpi]
    monkeypatch.setattr(ocr, "recognise", fake)
    return calls


def test_a_page_that_reads_confidently_is_not_read_again(monkeypatch):
    from acme_ap.ingestion import ocr
    calls = stub_recognise(monkeypatch, {200: 94.0, 300: 99.0, 400: 99.0})
    assert ocr.read_page(fake_page())[0][4] == "read-at-200" and calls == [200]


def test_small_type_is_retried_at_a_higher_resolution_and_the_better_reading_kept(monkeypatch):
    from acme_ap.ingestion import ocr
    calls = stub_recognise(monkeypatch, {200: 57.0, 300: 95.0, 400: 93.0})
    assert ocr.read_page(fake_page())[0][4] == "read-at-300" and calls == [200, 300]              # 300 was confident enough: no third pass


def test_if_nothing_is_confident_the_best_reading_wins_and_noise_does_not_trigger_a_swap(monkeypatch):
    from acme_ap.ingestion import ocr
    calls = stub_recognise(monkeypatch, {200: 60.0, 300: 61.0, 400: 70.0})
    assert ocr.read_page(fake_page())[0][4] == "read-at-400" and calls == [200, 300, 400]
    stub_recognise(monkeypatch, {200: 60.0, 300: 61.5, 400: 61.0})
    assert ocr.read_page(fake_page())[0][4] == "read-at-200"                                       # +1.5 is noise: the cheaper reading stands


def test_the_independent_scan_evaluation_is_current_and_says_the_model_helps():
    import os
    import subprocess
    import sys
    env = {**os.environ, "PYTHONWARNINGS": "ignore", "XAI_API_KEY_FILE": "/nonexistent"}                # replays the recorded OCR text and model answers: no Tesseract, no key
    p = subprocess.run([sys.executable, "scripts/evaluate_scans.py", "--check"], cwd=ROOT, capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    rows = {r.split("|")[1].strip(): [c.strip() for c in r.split("|")[2:-1]] for r in (ROOT / "docs" / "SCAN_EVAL.md").read_text().splitlines() if r.startswith(("| rules", "| with model"))}
    rules, model = int(rows["rules"][0].split()[0]), int(rows["with model"][0].split()[0])
    assert rules >= 15 and model > rules and model >= 20                                          # adaptive OCR made the rules usable; the model adds on top


def test_a_blank_scanned_page_is_empty_not_an_error(tmp_path):
    """Found while testing adaptive OCR: a uniform page crashed the clean-up ('All-NaN slice'), which would have made a whole multi-page scan unreadable."""
    from acme_ap.ingestion import ocr
    cleaned = ocr.clean_page(np.full((300, 300), 255, dtype=np.uint8))
    assert cleaned.shape == (300, 300) and (cleaned == cleaned.flat[0]).all()                       # still a flat page
    page = pymupdf.open().new_page(width=200, height=200)
    assert ocr.read_page(page) == []                                                                # one pass, no words, no crash


def test_a_scan_with_a_blank_second_page_still_reads_the_first(tmp_path):
    from acme_ap.ingestion import ocr
    if not ocr.available():
        pytest.skip("Tesseract is not installed")
    src = ROOT / "data" / "scanned" / "scanned_invoice_1011.pdf"
    doc = pymupdf.open(src)
    doc.new_page()                                                                                    # a blank back side, as scanners produce
    out = tmp_path / "two_pages.pdf"
    doc.save(out)
    from acme_ap.ingestion import parse_file
    inv, _ = parse_file(str(out))
    assert inv.invoice_number == "INV-1011" and any("ocr" in w for w in inv.parse_warnings)
