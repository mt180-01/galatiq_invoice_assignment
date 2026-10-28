"""Document ingestion. `parse_file` is the CPU-bound unit of work executed in worker processes."""
from __future__ import annotations

from pathlib import Path


from ..models import Invoice
from . import heuristic, structured

SUPPORTED = {".txt", ".pdf", ".json", ".xml", ".csv"}  # plus .eml, expanded by `intake` before parsing


def _rows(words: list) -> list[str]:
    """Rebuild visual rows from word boxes so table cells stay on one line."""
    ordered = sorted(words, key=lambda w: (round(w[3]), w[0]))
    rows: list[list[tuple]] = []
    for w in ordered:
        if rows and abs(rows[-1][0][3] - w[3]) < 3:
            rows[-1].append(w)
        else:
            rows.append([w])
    lines = []
    for row in rows:
        row.sort(key=lambda w: w[0])
        out, prev_x1 = "", None
        for w in row:
            if prev_x1 is not None:
                out += "  " if w[0] - prev_x1 > 12 else " "
            out += w[4]
            prev_x1 = w[2]
        lines.append(out)
    return lines


def read_pdf(path: Path, ignore_embedded: bool = False) -> tuple[str | None, str, bool]:
    """Open the PDF once. Returns (embedded e-invoice XML or None, visible text, text_came_from_ocr).

    Vendors who embed Factur-X/ZUGFeRD/UBL XML in an ordinary-looking PDF need no extraction at all.
    Pages with no text layer are scans and go through OCR.
    """
    import pymupdf

    from . import ocr

    lines: list[str] = []
    used_ocr = False
    with pymupdf.open(path) as doc:
        for name in ([] if ignore_embedded else doc.embfile_names()):
            if name.lower().endswith(".xml"):
                return doc.embfile_get(name).decode("utf-8", errors="replace"), "", False
        for page in doc:
            words = page.get_text("words")
            if words:
                lines.extend(_rows(words))
            elif page.get_images() and ocr.available():
                lines.extend(_rows(ocr.read_page(page)))
                used_ocr = True
    return None, "\n".join(lines), used_ocr


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_file(path: str) -> tuple[Invoice, str]:
    """Read one file and extract with the cheapest reliable method. Returns (invoice, raw_text)."""
    p = Path(path)
    suffix = p.suffix.lower()
    used_ocr = False
    if suffix == ".pdf":
        xml, text, used_ocr = read_pdf(p)
        if xml:
            try:
                inv = structured.from_xml(xml)
                inv.extractor = f"embedded-{inv.extractor}"
                inv.source_path, inv.source_format = str(p), "pdf"
                return inv, ""
            except Exception:  # noqa: BLE001 - unreadable attachment: fall back to the visible page
                _, text, used_ocr = read_pdf(p, ignore_embedded=True)
        if not text.strip():
            inv = Invoice(source_path=str(p), source_format="pdf", extractor="none",
                          parse_warnings=["no text layer and OCR unavailable or empty: scanned page cannot be read"])
            return inv, ""
    else:
        text = read_text(p)
    if suffix == ".json":
        inv = structured.from_json(text)
    elif suffix == ".xml":
        inv = structured.from_xml(text)
    elif suffix == ".csv":
        inv = structured.from_csv(text)
    else:
        inv = heuristic.extract(text, suffix.lstrip("."))
        if used_ocr:
            inv.extractor = "heuristic:pdf-ocr"
            inv.parse_warnings.append("ocr: text recovered from a scanned image; verify figures")
    inv.source_path, inv.source_format = str(p), suffix.lstrip(".")
    return inv, text
