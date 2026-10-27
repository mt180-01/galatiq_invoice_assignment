import pytest

from acme_ap.agents import validation
from acme_ap.erp import MockNetSuite
from acme_ap.ingestion import heuristic
from scripts.stress_extraction import run

BASE = "Vendor: Widgets Inc.\nDate: 2026-02-01\nWidgetA qty: 2 unit price: $250.00\nTotal: $500.00\n"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_corrupted_invoice_is_approved_with_a_wrong_value(seed):
    tally, bad = run(300, seed)
    assert tally["silent wrong"] == 0, bad[:1]
    assert tally["exact"] >= 0.7 * 300                      # and it is not "safe" merely because it refuses everything


def test_stray_invoice_references_do_not_become_the_invoice_number():
    inv = heuristic.extract("See also invoice 31337\nInvoice INV-8100\n" + BASE)
    assert inv.invoice_number == "INV-8100" and not inv.parse_warnings


def test_two_equally_plausible_invoice_numbers_are_a_hold_not_a_guess(tmp_path):
    inv = heuristic.extract("Invoice INV-6001 was cancelled\nInvoice INV-8100\n" + BASE)
    assert any(w.startswith("extractor disagreement") and "INV-6001" in w and "INV-8100" in w for w in inv.parse_warnings)
    assert "extractor_disagreement" in {f.code for f in validation.validate(inv, MockNetSuite(tmp_path / "x.db"))}


def test_conflicting_totals_are_a_hold_not_a_guess():
    inv = heuristic.extract("Invoice INV-8101\n" + BASE + "Total: $400.00\n")
    assert any("several different totals" in w for w in inv.parse_warnings)
