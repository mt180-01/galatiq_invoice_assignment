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
