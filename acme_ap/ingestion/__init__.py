"""Document ingestion. `parse_file` is the CPU-bound unit of work executed in worker processes."""
from __future__ import annotations

from pathlib import Path

from ..models import Invoice
from . import heuristic, structured

SUPPORTED = {".txt", ".pdf", ".json", ".xml", ".csv"}


def pdf_to_text(path: Path) -> str:
    """Rebuild visual rows from word boxes so table cells stay on one line."""
    import pymupdf

    lines: list[str] = []
    with pymupdf.open(path) as doc:
        for page in doc:
            words = sorted(page.get_text("words"), key=lambda w: (round(w[3]), w[0]))
            rows: list[list[tuple]] = []
            for w in words:
                if rows and abs(rows[-1][0][3] - w[3]) < 3:
                    rows[-1].append(w)
                else:
                    rows.append([w])
            for row in rows:
                row.sort(key=lambda w: w[0])
                out, prev_x1 = "", None
                for w in row:
                    if prev_x1 is not None:
                        out += "  " if w[0] - prev_x1 > 12 else " "
                    out += w[4]
                    prev_x1 = w[2]
                lines.append(out)
    return "\n".join(lines)


def read_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return pdf_to_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def parse_file(path: str) -> tuple[Invoice, str]:
    """Read one file and extract with the cheapest reliable method. Returns (invoice, raw_text)."""
    p = Path(path)
    suffix = p.suffix.lower()
    text = read_text(p)
    if suffix == ".json":
        inv = structured.from_json(text)
    elif suffix == ".xml":
        inv = structured.from_xml(text)
    elif suffix == ".csv":
        inv = structured.from_csv(text)
    else:
        inv = heuristic.extract(text, suffix.lstrip("."))
    inv.source_path, inv.source_format = str(p), suffix.lstrip(".")
    return inv, text
