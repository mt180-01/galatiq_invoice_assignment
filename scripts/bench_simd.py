"""Where SIMD pays: scan clean-up on a full-page image, vectorised numpy vs a per-pixel Python loop.

Usage: .venv/bin/python scripts/bench_simd.py
The scalar loop is timed on a small crop and scaled by pixel count (a full page in pure Python takes minutes).
"""
import sys
import time
from pathlib import Path

import numpy as np
import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from acme_ap.ingestion import ocr  # noqa: E402


def flatten_vector(gray, bg, thr):
    return np.where(np.minimum(gray.astype(np.float32) * (255.0 / np.maximum(bg, 1.0)), 255.0) > thr, 255, 0).astype(np.uint8)


def flatten_scalar(gray, bg, thr):
    h, w = len(gray), len(gray[0])
    out = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            v = min(gray[y][x] * 255.0 / max(bg[y][x], 1.0), 255.0)
            out[y][x] = 255 if v > thr else 0
    return out


def timed(fn, repeat):
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def main() -> None:
    from numpy._core._multiarray_umath import __cpu_features__ as feats
    print("numpy", np.__version__, "| CPU features used:", [k for k, v in feats.items() if v and k in ("NEON", "ASIMD", "AVX2", "AVX512F", "SSE42")])
    with pymupdf.open(Path(__file__).resolve().parent.parent / "data/scanned/scanned_invoice_1013.pdf") as d:
        pix = d[0].get_pixmap(matrix=pymupdf.Matrix(200 / 72, 200 / 72), colorspace=pymupdf.csGRAY)
    page = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    bg = np.full(page.shape, 235, dtype=np.float32)
    px = page.size
    vec = timed(lambda: flatten_vector(page, bg, 128), 20)
    crop = page[:150, :150].tolist()
    crop_bg = bg[:150, :150].tolist()
    scal = timed(lambda: flatten_scalar(crop, crop_bg, 128), 3) * px / (150 * 150)
    print(f"normalise+threshold, {px/1e6:.1f} MP page: numpy {vec*1000:8.1f} ms | per-pixel Python ~{scal*1000:9.0f} ms (extrapolated) | {scal/vec:,.0f}x")
    print(f"full clean_page (background, normalise, Otsu, binarise, rule removal): {timed(lambda: ocr.clean_page(page), 10)*1000:.0f} ms")
    clean = ocr.clean_page(page)
    print(f"Tesseract on the cleaned page:                                      {timed(lambda: ocr.recognise(clean), 2)*1000:.0f} ms")


if __name__ == "__main__":
    main()
