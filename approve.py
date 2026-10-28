"""Approver-side commands against the database left by the last `main.py` run.

  python approve.py list                         pending/decided requests
  python approve.py card 3                       show the decision card for request 3
  python approve.py respond <token> [--as vp-finance@acmecorp.example] [--comment "ok"]   (the link is issued to one mailbox; --as can only agree with it)
  python approve.py tick --hours 50              fast-forward the clock: reminders, escalation, expiry
"""
from __future__ import annotations

import argparse
import json
from datetime import timedelta

from acme_ap.approvals import ApprovalService
from acme_ap.approvals.tokens import utcnow
from acme_ap.erp import MockNetSuite


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="inventory.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("card"); c.add_argument("id", type=int)
    r = sub.add_parser("respond"); r.add_argument("token"); r.add_argument("--as", dest="who", default=None, help="optional; the link already says who it was issued to, and this must match"); r.add_argument("--comment", default="")
    t = sub.add_parser("tick"); t.add_argument("--hours", type=float, default=0)
    a = ap.parse_args()
    svc = ApprovalService(a.db, MockNetSuite(a.db, keep_ledger=True), reset=False)
    if a.cmd == "list":
        for row in svc.pending():
            print(f"#{row['id']:<3} {row['status']:<9} {row['role']:<12} {row['bill_key']:<40} {'; '.join(json.loads(row['reasons']))[:70]}")
    elif a.cmd == "card":
        print(svc.render_card(a.id))
    elif a.cmd == "respond":
        res = svc.respond(a.token, a.who, a.comment)
        print(("OK: " if res.ok else "NOT DONE: ") + res.message)
    elif a.cmd == "tick":
        for line in svc.tick(utcnow() + timedelta(hours=a.hours)) or ["nothing due"]:
            print(line)


if __name__ == "__main__":
    main()
