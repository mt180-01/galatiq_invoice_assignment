"""Reading documents: the CPU-bound half of the pipeline.

PDF layout analysis is CPU-bound, so PDFs go to a pool of worker processes (one per core, when there is enough work to pay for starting them).
Cheap formats (txt/csv/json/xml, ~0.05 ms each) are parsed inline: shipping them to a worker would cost more in IPC than the parse itself.
Each path gets a future that resolves to ("ok", (invoice, text)) or ("err", message); a bad file never raises into the batch.
"""
from __future__ import annotations

import asyncio
import os
from concurrent.futures import ProcessPoolExecutor

from .ingestion import heuristic, parse_file

PDFS_PER_WORKER = 100


def _warm(vendors: list[str] | None = None) -> None:  # pre-import heavy modules once per worker, and hand it the vendor master
    import pymupdf  # noqa: F401
    heuristic.set_known_vendors(vendors or [])


def _parse_chunk(paths: list[str], want_text: bool) -> list[tuple[str, object]]:
    """Runs in a worker process. Never raises: a bad file must not kill the batch."""
    out: list[tuple[str, object]] = []
    for p in paths:
        try:
            inv, text = parse_file(p)
            out.append(("ok", (inv, text if want_text else "")))
        except Exception as exc:  # noqa: BLE001 - isolate per-file failures
            out.append(("err", f"{type(exc).__name__}: {exc}"))
    return out


def plan_workers(n_pdfs: int, requested: int | None) -> int:
    """Worker processes cost ~0.7s each to start (imports); they pay back only with enough PDF work.
    In a long-lived service the pool is reused, so this only matters for one-shot CLI runs."""
    if requested is not None:
        return requested
    return min(os.cpu_count() or 1, n_pdfs // PDFS_PER_WORKER)


class ParsePool:
    """Starts parsing every path at once and hands back, per path, a future and the index of its result inside that future."""

    def __init__(self, paths: list[str], want_text: bool, workers: int | None, vendors: list[str]):
        loop = asyncio.get_running_loop()
        heavy = [p for p in paths if p.lower().endswith(".pdf")]  # CPU-heavy: layout analysis
        self.workers = plan_workers(len(heavy), workers)
        self._pool = ProcessPoolExecutor(self.workers, initializer=_warm, initargs=(vendors,)) if self.workers > 1 and heavy else None
        self.futures: dict[str, tuple[asyncio.Future, int]] = {}
        chunk = max(1, min(32, len(heavy) // (self.workers * 4) or 1)) if self._pool else 1
        for i in range(0, len(heavy), chunk):
            part = heavy[i:i + chunk]
            fut = loop.run_in_executor(self._pool, _parse_chunk, part, want_text)
            for j, p in enumerate(part):
                self.futures[p] = (fut, j)
        for p in paths:
            if p not in self.futures:
                done = loop.create_future()
                done.set_result(_parse_chunk([p], want_text))
                self.futures[p] = (done, 0)

    @property
    def pool_workers(self) -> int:
        return self.workers if self._pool else 1

    def close(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=True)
