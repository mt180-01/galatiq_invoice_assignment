"""Cost of the approval machinery per request, to show it is not a compute problem.
Usage: .venv/bin/python scripts/bench_approvals.py"""
import asyncio
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from acme_ap.approvals import ApprovalService, route  # noqa: E402
from acme_ap.approvals.tokens import evidence_hash, make_token, read_token, utcnow  # noqa: E402
from acme_ap.erp import MockNetSuite  # noqa: E402
from acme_ap.llm import OfflineLLM  # noqa: E402
from acme_ap.pipeline import Pipeline  # noqa: E402

DEMO = Path(__file__).resolve().parent.parent / "data/approvals_demo/invoice_3001.json"


def us(fn, n):
    t = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t) / n * 1e6


def main() -> None:
    db = tempfile.mkdtemp() + "/b.db"
    erp = MockNetSuite(db)
    svc = ApprovalService(db, erp)
    (o,) = asyncio.run(Pipeline(erp, OfflineLLM(), workers=1, approvals=None).run([str(DEMO)]))
    secret, now = b"s" * 32, utcnow()
    token = make_token(secret, 1, "approve", "h", now + timedelta(days=1))
    print(f"route()                         {us(lambda: route(o), 20000):8.1f} us")
    print(f"evidence_hash()                 {us(lambda: evidence_hash(o.invoice), 20000):8.1f} us")
    print(f"sign token                      {us(lambda: make_token(secret, 1, 'approve', 'h', now + timedelta(days=1)), 20000):8.1f} us")
    print(f"verify token (HMAC-SHA256)      {us(lambda: read_token(secret, token, now), 20000):8.1f} us")
    print(f"open_request + render card      {us(lambda: svc.render_card(svc.open_request(o, 'k')[0]), 300):8.1f} us")
    print("\nA human takes minutes to days to respond (the SLA here is 24 h to remind, 48 h to escalate).")
    print("Even 100,000 pending requests would cost well under a minute of compute in total; people are the bottleneck.")


if __name__ == "__main__":
    main()
