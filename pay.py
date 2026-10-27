"""Treasury-side commands against the database left by the last `main.py` run.

  python pay.py status                                  every payment and where it stands
  python pay.py release [--on 2026-12-31]               send what is due (use --on to fast-forward the clock)
  python pay.py reconcile [--on 2027-01-05]             compare with the bank: settled, returned, unknown
  python pay.py recover                                 after a crash: ask the bank about half-finished payments
  python pay.py account list
  python pay.py account set "Widgets Inc." acct-123 --by ap@acmecorp.example        (holds that vendor's payments)
  python pay.py account verify "Widgets Inc." --by controller@acmecorp.example      (a different person)
"""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

from acme_ap.erp import MockNetSuite
from acme_ap.payments import MockBank, PaymentService


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="inventory.db")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("release", "reconcile"):
        sub.add_parser(name).add_argument("--on", help="pretend today is this date (YYYY-MM-DD)")
    sub.add_parser("recover")
    acc = sub.add_parser("account").add_subparsers(dest="op", required=True)
    acc.add_parser("list")
    s = acc.add_parser("set"); s.add_argument("vendor"); s.add_argument("account"); s.add_argument("--by", required=True)
    v = acc.add_parser("verify"); v.add_argument("vendor"); v.add_argument("--by", required=True)
    a = ap.parse_args()
    today = date.fromisoformat(a.on) if getattr(a, "on", None) else date.today()
    svc = PaymentService(a.db, MockNetSuite(a.db, keep_ledger=True), MockBank(a.db + ".bank"), reset=False, today=lambda: today)
    if a.cmd == "status":
        for r in svc.rows():
            print(f"{r['state']:<10} {r['release_on']}  {r['rail'].upper():<4} {Decimal(r['amount']):>10,.2f}  {r['bank_ref'] or '-':<16} {r['bill_key'][:34]:<34} {r['last_error'] or r['note'] or ''}")
    elif a.cmd == "release":
        res = svc.release_due()
        print(f"{sum(r.submitted for r in res)} of {len(res)} due payment(s) sent")
        for r in res:
            print(f"  {r.state:<10} {r.amount:>10,.2f}  {r.message}")
    elif a.cmd == "reconcile":
        rec = svc.reconcile()
        print(f"settled {rec.settled}, returned {rec.returned}, still in flight {rec.pending}")
        for issue in rec.issues:
            print("  !", issue)
    elif a.cmd == "recover":
        for line in svc.recover() or ["nothing half-finished"]:
            print(line)
    elif a.op == "list":
        for r in svc.accounts():
            print(f"{r['vendor']:<32} {r['account']:<16} {r['status']}")
    elif a.op == "set":
        print(svc.set_account(a.vendor, a.account, a.by))
    else:
        print(svc.verify_account(a.vendor, a.by))


if __name__ == "__main__":
    main()
