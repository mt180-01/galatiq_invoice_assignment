"""Builds data/scanned/: image-only PDFs (no text layer) from a sample invoice, with scanner-like
uneven lighting and noise, to exercise OCR.  Usage: .venv/bin/python scripts/make_scanned_sample.py"""
from pathlib import Path

import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "scanned"


def scan(src: Path, dest: Path, seed: int = 7) -> None:
    with pymupdf.open(src) as doc:
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), colorspace=pymupdf.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).astype(np.float32)
    rng = np.random.default_rng(seed)
    h, w = img.shape
    shade = 1.0 - 0.35 * (np.linspace(0, 1, w)[None, :] * 0.7 + np.linspace(0, 1, h)[:, None] * 0.3)  # uneven lighting
    img = np.clip(img * shade + rng.normal(0, 9, img.shape), 0, 255).astype(np.uint8)
    out = pymupdf.open()
    page = out.new_page(width=pix.width * 72 / 200, height=pix.height * 72 / 200)
    page.insert_image(page.rect, stream=pymupdf.Pixmap(pymupdf.csGRAY, w, h, img.tobytes(), False).tobytes("png"))
    out.save(dest)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    scan(ROOT / "data" / "invoices" / "invoice_1011.pdf", OUT / "scanned_invoice_1011.pdf")
    scan(ROOT / "data" / "invoices" / "invoice_1013.pdf", OUT / "scanned_invoice_1013.pdf")
