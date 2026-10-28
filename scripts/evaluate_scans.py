"""Scanned invoices, end to end: independent layouts -> an image-only PDF -> OCR -> the readers (docs/SCAN_EVAL.md).

Each document from the extraction corpus (written by Grok from ground truth this repo chose) is typeset onto a page, rasterised, and degraded like a flatbed scan
(uneven lighting, sensor noise), so the only text is what Tesseract can recover. Then the rules reader and the model-assisted reader are compared with the truth.
Both slow or paid steps are recorded and replayed: the OCR text (data/extraction_eval/scans.ocr.json: OCR of 30 pages takes minutes and depends on the installed
Tesseract) and the model's answers (data/extraction_eval/scans.cassette.jsonl). `--record` redoes both (needs Tesseract and a key); the default and `--check` need neither.

    python scripts/evaluate_scans.py [--check|--record]

Scanned invoices are never auto-paid (policy: a person verifies OCR output), so the question here is how much typing the person is saved, not whether money moves.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from acme_ap.erp import MockNetSuite  # noqa: E402
from acme_ap.ingestion import heuristic, llm_extract, ocr, parse_file  # noqa: E402
from acme_ap.llm import as_agent, make_llm  # noqa: E402
from scripts.evaluate_extraction import CORPUS, CRITICAL, FIELDS, compare  # noqa: E402

CASSETTE = CORPUS / "scans.cassette.jsonl"
OCR_TEXT = CORPUS / "scans.ocr.json"
OUT = ROOT / "docs" / "SCAN_EVAL.md"
MAX_CHARS = 2600


def typeset(text: str, dest: Path, seed: int) -> None:
    """Text on a page, rasterised at 200 dpi, then degraded like a scan."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(40, 40, 555, 800), text, fontname="cour", fontsize=9)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), colorspace=pymupdf.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
    rng = np.random.default_rng(seed)
    h, w = img.shape
    shade = 1.0 - 0.35 * (np.linspace(0, 1, w)[None, :] * 0.7 + np.linspace(0, 1, h)[:, None] * 0.3)
    img = np.clip(img * shade + rng.normal(0, 9, img.shape), 0, 255).astype(np.uint8)
    out = pymupdf.open()
    p = out.new_page(width=pix.width * 72 / 200, height=pix.height * 72 / 200)
    p.insert_image(p.rect, stream=pymupdf.Pixmap(pymupdf.csGRAY, w, h, img.tobytes(), False).tobytes("png"))
    out.save(dest)


def ocr_baselines(manifest, record: bool) -> dict:
    """{id: (the rules' reading of the OCR text, the OCR text)}: typeset, scanned and OCR'd when recording, else loaded from the recording."""
    if record:
        if not ocr.available():
            sys.exit("Tesseract is not installed: needed to OCR the scans")
        tmp, texts = Path(tempfile.mkdtemp()), {}
        for m in manifest:
            pdf = tmp / f"{m['id']:03d}.pdf"
            typeset((CORPUS / m["file"]).read_text(), pdf, seed=m["id"])
            texts[str(m["id"])] = parse_file(str(pdf))[1]
        OCR_TEXT.write_text(json.dumps(texts, indent=1))
    texts = json.loads(OCR_TEXT.read_text())
    out = {}
    for m in manifest:
        text = texts[str(m["id"])]
        inv = heuristic.extract(text, "pdf")           # exactly what parse_file does with OCR text
        inv.extractor = "heuristic:pdf-ocr"
        inv.parse_warnings.append("ocr: text recovered from a scanned image; verify figures")
        out[m["id"]] = (inv, text)
    return out


def build(record: bool) -> str:
    manifest = [m for m in json.loads((CORPUS / "truth.json").read_text()) if m["id"] % 2 == 0 and len((CORPUS / m["file"]).read_text()) <= MAX_CHARS and m["style"] != "ocr"]
    erp = MockNetSuite(Path(tempfile.mkdtemp()) / "e.db")
    heuristic.set_known_vendors(erp.vendor_names())
    parsed = ocr_baselines(manifest, record)
    llm = make_llm(record_to=CASSETTE) if record else make_llm(replay_from=CASSETTE)

    async def with_model():
        sem, out = asyncio.Semaphore(6), {}

        async def one(m):
            base, text = parsed[m["id"]]
            async with sem:
                with as_agent("intake"):
                    out[m["id"]] = await llm_extract.extract(text, base.model_copy(deep=True), llm, gate=True)
        await asyncio.gather(*(one(m) for m in manifest))
        return out
    model = asyncio.run(with_model())
    results = {}
    for name, invs in (("rules", {k: v[0] for k, v in parsed.items()}), ("with model", model)):
        per_field, crit, exact = Counter(), 0, 0
        for m in manifest:
            ok = compare(m["truth"], invs[m["id"]])
            for k, v in ok.items():
                per_field[k] += v
            crit += all(ok[k] for k in CRITICAL)
            exact += all(ok.values())
        results[name] = {"per_field": per_field, "critical": crit, "exact": exact}
    calls, cost = sum(llm.meter.calls.values()), llm.meter.spent_usd
    if getattr(llm, "replay", None) is not None and llm.replay.misses:
        print(f"warning: {llm.replay.misses} model call(s) not in the recording", file=sys.stderr)
    asyncio.run(llm.aclose())
    n = len(manifest)
    L = ["# Scanned invoices, end to end", "",
         "Generated by `python scripts/evaluate_scans.py`; do not edit by hand.", "",
         f"**{n} documents** from the independent extraction corpus (see [EXTRACTION_EVAL.md](EXTRACTION_EVAL.md)), each typeset onto a page, rasterised at 200 dpi and degraded like a flatbed scan "
         "(uneven lighting, sensor noise), so the only text is what Tesseract can recover. Then two readers, compared with the truth the corpus was built from:", "",
         "* **rules**: the regex reader on the OCR text.", "* **with model**: the pipeline's Intake: the model reads the OCR text (the rules are always in doubt on OCR) and is cross-checked.", "",
         "Scanned invoices are never auto-paid (a person verifies OCR output), so the question is how much typing the person is saved.", "",
         "| Reader | All critical fields right | Every field right | Model calls | Cost |", "|---|---|---|---|---|"]
    for name in ("rules", "with model"):
        r = results[name]
        L.append(f"| {name} | {r['critical']} / {n} ({r['critical'] / n:.0%}) | {r['exact']} / {n} | " + (f"{calls} | ${cost:.4f}" if name == "with model" else "0 | $0") + " |")
    L += ["", "| Field (correct out of %d) | rules | with model |" % n, "|---|---|---|"]
    for f in FIELDS:
        L.append(f"| {f}{' (critical)' if f in CRITICAL else ''} | {results['rules']['per_field'][f]} | {results['with model']['per_field'][f]} |")
    L += ["", "**Reading this.** Two things decide how well a scan reads, and the first is not the model. (1) **OCR resolution.** Pages were first OCR'd at a fixed 200 dpi, where small type "
          "(9 pt) came out as garbage and the model could not repair what OCR had destroyed: on this set that gave 37% of scans fully right with the rules and 43% with the model. Tesseract's own "
          "confidence turned out to track accuracy closely (mean 57 at 200 dpi for 9 pt text, 95 at 300 dpi), so the page is now re-read at 300 and 400 dpi when confidence is low, and the most "
          "confident reading kept; that alone took the rules to the figure above. (2) **The model**, which then adds the rest, mostly on the line items (multi-column tables are what OCR "
          "scrambles). Whatever the reader, a scanned invoice is held for a person; the value of a good reading is a pre-filled form to check instead of a blank one.", "",
          "Limits: typeset-then-degraded pages (one font family, flat), one OCR engine, one model, one recording: this is not a photograph of a crumpled receipt, and 30 documents shows patterns, "
          "not a precise rate.", ""]
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--record", action="store_true")
    a = ap.parse_args()
    text = build(a.record)
    if a.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/SCAN_EVAL.md is out of date: run python scripts/evaluate_scans.py")
            return 1
        print("docs/SCAN_EVAL.md is current")
        return 0
    OUT.write_text(text)
    print("wrote", OUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
