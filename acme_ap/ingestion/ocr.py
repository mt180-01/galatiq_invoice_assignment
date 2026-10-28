"""OCR for scanned PDFs (pages that are pictures with no text layer).

Pipeline: render page -> vectorised clean-up (numpy) -> Tesseract.

The clean-up is where SIMD applies: every step is a whole-array elementwise operation or reduction
over millions of pixels, which numpy runs with the CPU's vector instructions (NEON on arm64, AVX2/AVX-512
on x86). Tesseract itself is the expensive part (~seconds per page) and is why OCR runs inside the
process pool. Measured (scripts/bench_simd.py): the normalise+threshold step is ~27x faster than a per-pixel
Python loop, and the whole clean-up (~70 ms/page) is ~12% of the Tesseract time it feeds.
"""
from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pymupdf

DPI = 200
LOW_CONFIDENCE = 85.0  # mean Tesseract confidence below this means the text was too small for the resolution used (measured: 9 pt text at 200 dpi scored 57, at 300 dpi 95)
BLOCK = 32  # background-estimation tile, in pixels


def available() -> bool:
    return shutil.which("tesseract") is not None


def clean_page(gray: np.ndarray) -> np.ndarray:
    """uint8 grayscale -> uint8 black text on a flat white background, correcting uneven scan lighting.

    1. Background = per-tile max (ink is darker than paper, so the max is the paper level)   [SIMD reduction]
    2. Divide the page by the upsampled background to flatten shadows/gradients               [SIMD elementwise]
    3. Otsu threshold from a 256-bin histogram                                                [bincount + vector math]
    4. Binarise                                                                               [SIMD compare]
    """
    h, w = gray.shape
    ph, pw = (-h) % BLOCK, (-w) % BLOCK
    padded = np.pad(gray, ((0, ph), (0, pw)), mode="edge")
    tiles = padded.reshape(padded.shape[0] // BLOCK, BLOCK, padded.shape[1] // BLOCK, BLOCK)
    background = tiles.max(axis=(1, 3)).astype(np.float32)
    background = np.repeat(np.repeat(background, BLOCK, axis=0), BLOCK, axis=1)[:h, :w]
    flat = np.minimum(gray.astype(np.float32) * (255.0 / np.maximum(background, 1.0)), 255.0).astype(np.uint8)
    binary = np.where(flat > _otsu(flat), np.uint8(255), np.uint8(0))
    return _remove_rules(binary)


def _remove_rules(binary: np.ndarray, min_run: int = 60) -> np.ndarray:
    """Erase table borders: they make Tesseract merge cells and drop rows.

    A ruling line is a straight run of ink >= min_run pixels (no glyph at 200 dpi has one). Runs are found
    with prefix sums over whole rows/columns: a window of min_run pixels is all ink iff its sum equals
    min_run. Pixels covered by any solid window are erased. No per-pixel Python loops.
    """
    ink = binary == 0
    out = binary.copy()
    for transpose in (False, True):
        run = ink.T if transpose else ink
        n = run.shape[1]
        if n < min_run:
            continue
        cs = np.pad(np.cumsum(run, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
        starts = (cs[:, min_run:] - cs[:, :-min_run]) == min_run  # starts[:, i]: window [i, i+min_run) is solid ink
        sc = np.pad(np.cumsum(starts, axis=1, dtype=np.int32), ((0, 0), (1, 0)))
        j = np.arange(n)
        lo = np.maximum(j - min_run + 1, 0)           # starts that can cover pixel j lie in [lo, hi]
        hi = np.minimum(j, n - min_run)
        valid = hi >= lo
        covered = (sc[:, hi + 1] - sc[:, lo]) > 0
        covered &= valid[None, :]
        out[(covered.T if transpose else covered) & ink] = 255
    return out


def _otsu(img: np.ndarray) -> int:
    hist = np.bincount(img.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    levels = np.arange(256, dtype=np.float64)
    w0 = np.cumsum(hist)
    w1 = total - w0
    sum0 = np.cumsum(hist * levels)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean0 = sum0 / w0
        mean1 = (sum0[-1] - sum0) / w1
        between = w0 * w1 * (mean0 - mean1) ** 2
    if not np.isfinite(between).any():  # a single grey level (a blank page): nothing to separate, so everything is background
        return 0
    return int(np.nanargmax(between))


def recognise(gray: np.ndarray, dpi: int = DPI) -> tuple[list[tuple], float]:
    """Run Tesseract on a cleaned page rendered at `dpi`; returns (word boxes (x0, y0, x1, y1, text) in PDF points, the same
    shape PyMuPDF gives for text pages, so scans and digital PDFs share one layout reconstruction; mean word confidence 0-100).
    Raises RuntimeError if the engine fails."""
    h, w = gray.shape
    pgm = f"P5\n{w} {h}\n255\n".encode() + gray.tobytes()  # netpbm: no image library needed
    proc = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng", "tsv"], input=pgm,
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:200])
    k = 72.0 / dpi
    words, confidences = [], []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 12 and cols[0] == "5" and cols[11].strip():
            x, y, bw, bh = (int(c) for c in cols[6:10])
            words.append((x * k, y * k, (x + bw) * k, (y + bh) * k, cols[11].strip()))
            confidences.append(float(cols[10]))
    return words, (sum(confidences) / len(confidences) if confidences else 0.0)


def read_page(page) -> list[tuple]:
    """OCR a PDF page at the resolution that suits its text. Start at DPI; if Tesseract is not confident (small type), try 1.5x and 2x and keep the
    most confident reading. Pages that read well the first time cost no extra pass."""
    best: tuple[list[tuple], float] | None = None
    for dpi in (DPI, int(DPI * 1.5), DPI * 2):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), colorspace=pymupdf.csGRAY)
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        words, conf = recognise(clean_page(gray), dpi)
        if not words and best is None:
            return []  # a blank page (a back side, a separator sheet): no resolution will find text that is not there
        if best is None or conf > best[1] + 2:  # a clearly better reading, not noise
            best = (words, conf)
        if best[1] >= LOW_CONFIDENCE:
            break
    return best[0] if best else []
