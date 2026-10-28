"""What does this cost, and what could it save? Two kinds of number, kept apart on purpose:

  MEASURED   (from this repo's runs): the model cost per invoice (the live run: $0.0637 for 17 invoices) and the share of invoices that needed no human
             (7 of 17 on the provided sample, which is deliberately full of problem invoices, so real mailboxes should do better).
  ASSUMED    (yours to replace): how many invoices Acme processes, how long a person spends on one, what an hour costs, what an error costs to fix.
             The defaults below are illustrative placeholders, not Acme's numbers; the brief gives only the headline "$2M a year" and "30% error rate".

    python scripts/roi.py --invoices-per-year 100000 --minutes-manual 12 --hourly 45
"""
from __future__ import annotations

import argparse

MODEL_COST_PER_INVOICE = 0.0637 / 17   # measured: live Grok run, 17 invoices, as reported by xAI


def annual(a: argparse.Namespace, hands_off: float, error_rate_before: float, residual: float) -> tuple[float, float, float]:
    n = a.invoices_per_year
    before = n * a.minutes_manual / 60 * a.hourly + n * error_rate_before * a.rework_cost
    touched = n * (1 - hands_off)
    after = touched * a.minutes_touch / 60 * a.hourly + n * residual * a.rework_cost + n * MODEL_COST_PER_INVOICE
    return before, after, n * MODEL_COST_PER_INVOICE


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--invoices-per-year", type=int, default=100_000, help="ASSUMED")
    ap.add_argument("--minutes-manual", type=float, default=12, help="ASSUMED: minutes a person spends on one invoice today")
    ap.add_argument("--minutes-touch", type=float, default=4, help="ASSUMED: minutes on an invoice that still needs a person (review a prepared card, not re-key)")
    ap.add_argument("--hourly", type=float, default=45, help="ASSUMED: fully loaded cost of an AP hour, USD")
    ap.add_argument("--error-rate", type=float, default=0.30, help="from the brief: share of invoices with an error today")
    ap.add_argument("--rework-cost", type=float, default=60, help="ASSUMED: cost of finding and fixing one error, USD")
    ap.add_argument("--residual-error", type=float, default=0.02, help="ASSUMED: errors that still reach payment after automation (0 wrongly paid on the sample; not a forecast)")
    ap.add_argument("--hands-off", type=float, default=7 / 17, help="MEASURED on the sample: share needing no person")
    a = ap.parse_args()
    before, after, model = annual(a, a.hands_off, a.error_rate, a.residual_error)
    print(f"MEASURED  model cost per invoice ${MODEL_COST_PER_INVOICE:.4f}  ->  ${model:,.0f} a year at {a.invoices_per_year:,} invoices (the model is not where the money goes)")
    print(f"MEASURED  invoices needing no person on the provided sample: {a.hands_off:.0%}\n")
    print(f"ASSUMED   {a.invoices_per_year:,} invoices/yr, {a.minutes_manual:g} min each at ${a.hourly:g}/h, {a.error_rate:.0%} errors at ${a.rework_cost:g} each")
    print(f"          today      ${before:>12,.0f} a year")
    print(f"          automated  ${after:>12,.0f} a year  ({a.minutes_touch:g} min on the {1 - a.hands_off:.0%} that need a person, {a.residual_error:.0%} residual errors, model fees)")
    print(f"          difference ${before - after:>12,.0f} a year\n")
    print("Sensitivity (difference, USD/yr): rows = share needing no person, columns = share of invoices with errors today")
    cols = (0.15, 0.30, 0.45)
    print("                  " + "".join(f"{c:>13.0%}" for c in cols))
    for h in (0.2, a.hands_off, 0.6):
        print(f"  hands-off {h:>4.0%}  " + "".join(f"{(lambda b, x: b - x)(*annual(a, h, c, a.residual_error)[:2]):>13,.0f}" for c in cols))
    print("\nChange the ASSUMED inputs to Acme's real figures before quoting any of this. The structure is the point: savings come from fewer minutes per invoice and fewer")
    print("errors reaching payment; the model costs a fraction of a cent per invoice.")


if __name__ == "__main__":
    main()
