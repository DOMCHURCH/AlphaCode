"""The identity taxonomy, and the one definition of "balances" behind it.

Two things are pinned here:

  * `resolve_identity` -- the shared decision `build_view1` draws from and
    `scripts/identity_failures.py` counts from. It exists because those two had
    separately implemented it and disagreed.
  * `classify` -- which bucket a filing lands in, and crucially whether that
    bucket is a PASS. A filing that balances once its noncontrolling interest
    is included is drawn as balancing, so counting it as a failure made the
    script's headline rate disagree with the page's.
"""

from __future__ import annotations

import pytest

from scripts.identity_failures import FAIL_CATEGORIES, PASS_CATEGORIES, classify
from src.company.view1 import IDENTITY_TOLERANCE, resolve_identity

PASS_KEYS = {k for k, _, _ in PASS_CATEGORIES}
FAIL_KEYS = {k for k, _, _ in FAIL_CATEGORIES}


# --- resolve_identity -------------------------------------------------------

def test_a_plain_sum_that_closes_needs_no_added_term():
    balances, drift, basis = resolve_identity(1000.0, 600.0, 400.0)
    assert balances is True
    assert basis is None
    assert drift == pytest.approx(0.0)


def test_the_nci_closes_a_filing_that_reports_it_separately():
    # Parent equity 300, NCI 100, so the plain sum is 100 short of assets.
    balances, _, basis = resolve_identity(1000.0, 600.0, 300.0, nci=100.0)
    assert balances is True
    assert basis == "nci"


def test_the_mezzanine_closes_a_filing_that_presents_one():
    balances, _, basis = resolve_identity(1000.0, 600.0, 300.0, mezzanine=100.0)
    assert balances is True
    assert basis == "mezzanine"


def test_both_terms_together_close_what_neither_closes_alone():
    """The case the script used to miss entirely: it tried each separately."""
    balances, _, basis = resolve_identity(
        1000.0, 600.0, 300.0, nci=60.0, mezzanine=40.0
    )
    assert balances is True
    assert basis == "nci+mezzanine"


def test_neither_term_alone_would_have_closed_that_filing():
    assert resolve_identity(1000.0, 600.0, 300.0, nci=60.0)[0] is False
    assert resolve_identity(1000.0, 600.0, 300.0, mezzanine=40.0)[0] is False


def test_the_tightest_basis_wins_not_the_first_one_tried():
    """NCI would clear the tolerance; NCI + mezzanine lands exactly. The
    drawing must name the one the filing was actually written on."""
    tol = IDENTITY_TOLERANCE * 100.0
    assert tol == pytest.approx(0.5)
    # NCI alone leaves 0.4% -- inside tolerance. Both leaves 0.0%.
    balances, drift, basis = resolve_identity(
        1000.0, 600.0, 300.0, nci=96.0, mezzanine=4.0
    )
    assert balances is True
    assert basis == "nci+mezzanine"
    assert drift == pytest.approx(0.0)


def test_a_term_is_never_added_to_a_filing_that_already_balances():
    """Adding a published term to a sound filing would break a correct one."""
    balances, _, basis = resolve_identity(
        1000.0, 600.0, 400.0, nci=100.0, mezzanine=50.0
    )
    assert balances is True
    assert basis is None


def test_a_gap_no_published_term_reaches_does_not_balance():
    balances, drift, basis = resolve_identity(1000.0, 600.0, 200.0)
    assert balances is False
    assert basis is None
    assert drift == pytest.approx(20.0)


def test_impossible_assets_are_not_a_pass():
    assert resolve_identity(0.0, 0.0, 0.0)[0] is False


# --- classify ---------------------------------------------------------------

def _m(**kw: float) -> dict[str, float]:
    base = {"total_assets": 1000.0, "total_liabilities": 600.0}
    base.update(kw)
    return base


def test_the_pass_and_fail_keys_do_not_overlap():
    assert PASS_KEYS & FAIL_KEYS == set()


def test_a_balanced_filing_is_classified_as_a_pass():
    category, _ = classify(_m(total_equity=400.0))
    assert category == "balanced"
    assert category in PASS_KEYS


def test_a_filing_closed_by_the_nci_counts_as_a_pass():
    category, _ = classify(_m(total_equity=300.0, minority_interest=100.0))
    assert category == "nci"
    assert category in PASS_KEYS


def test_a_filing_closed_by_the_mezzanine_counts_as_a_pass():
    category, _ = classify(_m(total_equity=300.0, temporary_equity=100.0))
    assert category == "mezzanine"
    assert category in PASS_KEYS


def test_redeemable_noncontrolling_interest_closes_a_filing():
    """The tag the script named as a mezzanine metric while nothing ingested
    it -- so this category could never have been populated."""
    category, _ = classify(
        _m(total_equity=300.0, redeemable_noncontrolling_interest=100.0)
    )
    assert category == "mezzanine"


def test_a_combined_equity_total_never_double_counts_the_nci():
    """With `total_equity_incl_nci` published the NCI is already inside it.
    Adding it again would push a sound filing past the tolerance."""
    m = _m(total_equity_incl_nci=400.0, minority_interest=100.0)
    category, _ = classify(m)
    assert category == "balanced"


def test_the_mezzanine_total_is_preferred_over_its_component():
    """Summing a section total with a component of it would overshoot and
    turn a filing that balances into one that does not."""
    category, _ = classify(
        _m(
            total_equity=300.0,
            temporary_equity=100.0,
            redeemable_preferred_stock=60.0,
        )
    )
    assert category == "mezzanine"


def test_a_small_gap_past_the_tolerance_is_rounding_and_still_a_failure():
    # 0.8% -- past the 0.5% tolerance, inside the 1% rounding band.
    category, _ = classify(_m(total_equity=392.0))
    assert category == "rounding"
    assert category in FAIL_KEYS


def test_a_filing_that_balances_against_its_own_stated_total_is_our_bug():
    """The filer's stated right-hand side agrees with assets, so the shortfall
    is a line we failed to ingest."""
    category, _ = classify(
        _m(total_equity=200.0, liabilities_and_equity=1000.0)
    )
    assert category == "missing_tag"


def test_a_filing_that_misses_its_own_stated_total_is_the_filers_arithmetic():
    category, _ = classify(
        _m(total_equity=200.0, liabilities_and_equity=850.0)
    )
    assert category == "broken"


def test_without_a_stated_total_the_honest_answer_is_unexplained():
    category, _ = classify(_m(total_equity=200.0))
    assert category == "unexplained"


def test_a_filing_with_no_testable_identity_is_neither_pass_nor_fail():
    assert classify({"total_assets": 1000.0})[0] == ""
    assert classify({"total_liabilities": 1.0, "total_equity": 1.0})[0] == ""
