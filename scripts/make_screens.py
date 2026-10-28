"""Regenerates the pictures in docs/img/ from a real run: the web interface (screenshots taken with headless Chrome) and the command-line
output (terminal SVGs from rich). Nothing is mocked up: if the interface changes, run this again.

    python scripts/make_screens.py          # needs Google Chrome for the web screenshots
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["XAI_API_KEY_FILE"] = "/nonexistent"        # screenshots always come from the free local model
os.environ.pop("XAI_API_KEY", None)

from acme_ap.runner import run_batch  # noqa: E402
from acme_ap.web import make_server  # noqa: E402

OUT = ROOT / "docs" / "img"
CHROME = next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome") or "", shutil.which("chromium") or "") if p and Path(p).exists()), None)


def shot(url: str, name: str, width: int = 1180, height: int = 1000) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1", f"--window-size={width},{height}",
                    f"--screenshot={OUT / name}", url], check=True, capture_output=True, timeout=90)
    print("wrote", (OUT / name).relative_to(ROOT))


def web(tmp: Path) -> None:
    db = tmp / "shots.db"
    paths = [str(p) for d in ("invoices", "self_correction", "approvals_demo") for p in sorted((ROOT / "data" / d).iterdir())]
    asyncio.run(run_batch(paths, SimpleNamespace(db=str(db), keep_ledger=False, llm_concurrency=8, offline=False, simulate=True, grok=False), 1))
    server = make_server(db, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    home = urllib.request.urlopen(base + "/").read().decode()
    decide = base + re.search(r'href="(/decide\?t=[^"]+)"[^>]*>INV-3001', home).group(1).replace("&amp;", "&")
    shot(base + "/", "dashboard.png", height=1500)
    inv = {m.group(2): m.group(1) for m in re.finditer(r'href="/invoice/(\d+)">([^<]+)<', home)}
    shot(base + f"/invoice/{inv['INV-3201']}", "invoice-trace.png", height=1250)       # the self-corrected one: tool calls and the fix
    shot(decide, "approval.png", width=900, height=1050)
    server.shutdown()


def terminal(tmp: Path) -> None:
    from acme_ap import demo, report
    report.console.record, report.console.width = True, 110
    demo.console = report.console
    shutil.copytree(ROOT / "data", tmp / "data")
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        demo.demo()
    finally:
        os.chdir(cwd)
    report.console.save_svg(str(OUT / "demo.svg"), title="python main.py demo")
    print("wrote", (OUT / "demo.svg").relative_to(ROOT))


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as t:
        if CHROME:
            web(Path(t))
        else:
            print("Chrome not found: skipping the web screenshots")
        terminal(Path(t))
