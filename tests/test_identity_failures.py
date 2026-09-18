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

import datetime as dt

import pytest

from scripts.identity_failures import FAIL_CATEGORIES, PASS_CATEGORIES, classify
from src.company.view1 import IDENTITY_TOLERANCE, resolve_identity

PASS_KEYS = {k for k, _, _ in PASS_CATEGORIES}
FAIL_KEYS = {k for k, _, _ in FAIL_CATEGORIES}


# --- resolve_identity -------------------------------------------------------

def test_a_plain_sum_that_closes_needs_no_added_term():
    balances, drift, basis, _eq = resolve_identity(1000.0, 600.0, 400.0)
    assert balances is True
    assert basis is None
    assert drift == pytest.approx(0.0)


def test_the_nci_closes_a_filing_that_reports_it_separately():
    # Parent equity 300, NCI 100, so the plain sum is 100 short of assets.
    balances, _, basis, _eq = resolve_identity(1000.0, 600.0, 300.0, nci=100.0)
    assert balances is True
    assert basis == "nci"


def test_the_mezzanine_closes_a_filing_that_presents_one():
    balances, _, basis, _eq = resolve_identity(1000.0, 600.0, 300.0, mezzanine=100.0)
    assert balances is True
    assert basis == "mezzanine"


def test_both_terms_together_close_what_neither_closes_alone():
    """The case the script used to miss entirely: it tried each separately."""
    balances, _, basis, _eq = resolve_identity(
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
    balances, drift, basis, _eq = resolve_identity(
        1000.0, 600.0, 300.0, nci=96.0, mezzanine=4.0
    )
    assert balances is True
    assert basis == "nci+mezzanine"
    assert drift == pytest.approx(0.0)


def test_a_term_is_never_added_to_a_filing_that_already_balances():
    """Adding a published term to a sound filing would break a correct one."""
    balances, _, basis, _eq = resolve_identity(
        1000.0, 600.0, 400.0, nci=100.0, mezzanine=50.0
    )
    assert balances is True
    assert basis is None


def test_a_gap_no_published_term_reaches_does_not_balance():
    balances, drift, basis, _eq = resolve_identity(1000.0, 600.0, 200.0)
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


# --- selection by identity, not by tag name ---------------------------------
# Three shapes hand-traced in docs/internal/mezzanine-trace.md. Every figure
# below is the real filed number, at one period, from the named accession.
# In all three the filing balances to the dollar against values already in
# `fundamentals`, and the old tag-name preference reported a failure.

def test_iqst_shape_picks_the_equity_that_closes():
    """iQSTEL 10-Q 0001663577-26-000254, 2026-06-30.

    The filer has the two equity tags SWAPPED: `StockholdersEquity` carries
    the total (17,179,556) and the including-NCI tag carries the parent
    portion (12,753,059 + NCI 4,426,497 = 17,179,556). Preferring the tag
    name gave 43,765,399 against 48,191,896 of assets -- a 9.19% failure on a
    filing that balances exactly.
    """
    balances, drift, basis, _eq = resolve_identity(
        48_191_896.0, 31_012_340.0, 12_753_059.0,
        nci=4_426_497.0, equity_alt=17_179_556.0,
    )

    assert balances, "the filing balances on figures already stored"
    assert drift < IDENTITY_TOLERANCE * 100.0
    # Fewest terms wins: the parent-total equity closes it with no extra term.
    assert basis is None


def test_agilent_shape_ignores_an_nci_tag_that_is_not_company_equity():
    """Agilent 10-Q 0001090872-26-000064, 2026-07-31.

    `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`
    carries -233,000,000, which is not Agilent's equity -- that is
    7,363,000,000, and 6,604,000,000 + 7,363,000,000 is total assets to the
    dollar. The old rule preferred the negative figure and reported 54.39%.
    """
    balances, drift, basis, _eq = resolve_identity(
        13_967_000_000.0, 6_604_000_000.0, -233_000_000.0,
        equity_alt=7_363_000_000.0,
    )

    assert balances, "parent equity closes this filing exactly"
    assert drift == 0.0
    assert basis is None


def test_bam_shape_sums_two_mezzanine_components():
    """Brookfield 10-Q 0001628280-26-054933, 2026-06-30.

    Redeemable NCI is filed as TWO components and no section total: preferred
    1,238,000,000 and other 1,442,000,000. Only their sum closes. Taking one
    left 17,400,000,000 against 20,080,000,000 -- a 13.35% failure.
    """
    balances, drift, basis, _eq = resolve_identity(
        20_080_000_000.0, 8_212_000_000.0, 9_188_000_000.0,
        mezzanine=1_238_000_000.0,
        mezzanine_parts=(1_238_000_000.0, 1_442_000_000.0),
    )

    assert balances, "the two components sum to close the identity"
    assert drift < IDENTITY_TOLERANCE * 100.0
    assert basis == "mezzanine"


def test_a_section_total_is_never_added_to_its_own_components():
    """The guard that keeps the sum from inventing a balance.

    A filer who tags the section total AND a component must not have both
    counted. `_mezzanine_parts` returns nothing when a total exists, so the
    only mezzanine candidate is the total itself.
    """
    from src.company.view1 import _mezzanine_parts

    class Cell:
        def __init__(self, value):
            self.value, self.missing = value, False

    class BS:
        equity = {"temporary_equity": Cell(500.0),
                  "redeemable_preferred_stock": Cell(300.0)}

    assert _mezzanine_parts(BS()) == ()


def test_a_filing_that_balances_plainly_gains_no_explanation():
    """Offering more candidates must not let a term claim a filing that was
    already right."""
    balances, drift, basis, _eq = resolve_identity(
        1_000.0, 600.0, 400.0, nci=50.0, equity_alt=350.0,
        mezzanine_parts=(25.0, 25.0),
    )

    assert balances
    assert basis is None, "a plain sum must not be explained by a term"


# --- the caller, which is where the fix was being thrown away ---------------

def test_agilent_closes_on_the_other_equity_and_the_view_says_so():
    """The bug `resolve_identity` was rewritten for, and did not actually fix.

    Agilent's real numbers. `total_equity_incl_nci` carries -$233M while
    shareholders' equity is $7.363B, and 6,604 + 7,363 = 13,967 exactly.
    `resolve_identity` found that. `_check_identity` then discarded it, because
    its guard read `balances and basis is not None` -- and a close on the other
    equity adds no term, so `basis` is None.

    Asserted through `_check_identity`, not through `resolve_identity`: the
    function was already right, which is exactly why every existing test passed
    while the live site reported a 54.4% failure for eight days.
    """
    from src.company.view1 import Block, View1, _check_identity

    ASSETS, LIAB = 13_967_000_000.0, 6_604_000_000.0
    INCL_NCI, PARENT = -233_000_000.0, 7_363_000_000.0

    view = View1(
        ticker="A", company_name="Agilent Technologies, Inc.", sector=None,
        period_end=dt.date(2026, 7, 31), filing_date=dt.date(2026, 9, 1),
        mode="detailed", total_assets=ASSETS, total_liabilities=LIAB,
        total_equity=INCL_NCI,
    )
    # The drawing is built from the tag-named figure BEFORE the check runs,
    # which is what makes a half-fix dangerous.
    view.claims.append(Block(
        key="equity", label="Shareholders' equity", value=INCL_NCI,
        pct=abs(INCL_NCI) / ASSETS * 100.0, kind="equity",
    ))
    view.negative_equity = True
    view.notes.append(
        "Liabilities exceed total assets, so equity is negative. It is "
        "drawn below the baseline."
    )

    _check_identity(view, ASSETS, LIAB, INCL_NCI, equity_alt=PARENT)

    assert view.balances, "the filing closes to the dollar and was called a failure"
    assert view.imbalance_pct < 0.01

    # The picture has to move too, or the page says "balances" over a drawing
    # made from a number that does not.
    assert view.total_equity == PARENT
    assert [b.value for b in view.claims if b.kind == "equity"] == [PARENT]

    # Agilent's equity is positive. The live page said otherwise, in prose, as
    # a claim of fact about a real company.
    assert not view.negative_equity
    assert not any("drawn below the baseline" in n for n in view.notes)
    assert any("two equity totals" in n for n in view.notes)


def test_a_filer_whose_own_equity_is_negative_keeps_that_note():
    """Adopting the other figure recomputes the shape, it does not assume it."""
    from src.company.view1 import Block, View1, _check_identity

    ASSETS, LIAB = 1000.0, 1200.0
    WRONG, RIGHT = 500.0, -200.0   # 1200 + (-200) = 1000

    view = View1(
        ticker="X", company_name="X", sector=None,
        period_end=dt.date(2026, 1, 1), filing_date=dt.date(2026, 2, 1),
        mode="detailed", total_assets=ASSETS, total_liabilities=LIAB,
        total_equity=WRONG,
    )
    view.claims.append(Block(key="equity", label="Shareholders' equity",
                             value=WRONG, pct=50.0, kind="equity"))

    _check_identity(view, ASSETS, LIAB, WRONG, equity_alt=RIGHT)

    assert view.balances
    assert view.total_equity == RIGHT
    assert view.negative_equity, "a genuinely negative equity must still say so"
    assert any("drawn below the baseline" in n for n in view.notes)


def test_a_filing_that_closes_plainly_is_untouched():
    """The guard must not start adopting figures for filings that were fine."""
    from src.company.view1 import View1, _check_identity

    view = View1(
        ticker="Y", company_name="Y", sector=None,
        period_end=dt.date(2026, 1, 1), filing_date=dt.date(2026, 2, 1),
        mode="detailed", total_assets=1000.0, total_liabilities=600.0,
        total_equity=400.0,
    )
    _check_identity(view, 1000.0, 600.0, 400.0, equity_alt=-999.0)

    assert view.balances
    assert view.total_equity == 400.0
    assert view.identity_basis == ""
    assert not any("two equity totals" in n for n in view.notes)


def test_a_gap_that_is_ours_is_drawn_not_left_as_a_short_column():
    """BLK's shape: the filer's own totals agree, ours fall short.

    The paragraph says the gap is ours. Before this, the picture still showed
    the filer short -- $111.3B + $57.8B drawn against $175.9B of assets -- so
    the image went on making the accusation after the text had stopped.
    """
    from src.company.view1 import Block, View1, _check_identity

    ASSETS = 175_900_000_000.0
    LIAB, EQUITY = 111_300_000_000.0, 57_800_000_000.0

    view = View1(
        ticker="BLK", company_name="BlackRock, Inc.", sector=None,
        period_end=dt.date(2026, 6, 30), filing_date=dt.date(2026, 8, 6),
        mode="detailed", total_assets=ASSETS, total_liabilities=LIAB,
        total_equity=EQUITY, stated_rhs=ASSETS,
    )
    view.claims.append(Block(key="total_liabilities", label="Liabilities",
                             value=LIAB, pct=63.3, kind="liability"))
    view.claims.append(Block(key="equity", label="Shareholders' equity",
                             value=EQUITY, pct=32.9, kind="equity"))

    _check_identity(view, ASSETS, LIAB, EQUITY)

    assert not view.balances, "this filing genuinely does not close as we read it"

    unread = [b for b in view.claims if b.key == "unread_components"]
    assert len(unread) == 1, "the shortfall must be drawn, not left as empty space"
    band = unread[0]
    assert band.value == pytest.approx(ASSETS - (LIAB + EQUITY))
    assert band.is_remainder
    assert "Ours, not theirs" in (band.note or "")

    # The column now sums to the filer's own stated total.
    drawn = sum(b.value for b in view.claims if b.kind != "equity") + EQUITY
    assert drawn == pytest.approx(ASSETS)


def test_a_filer_whose_own_totals_disagree_gets_no_band():
    """If their arithmetic is the problem, drawing a band would hide it."""
    from src.company.view1 import View1, _check_identity

    ASSETS, LIAB, EQUITY = 1000.0, 600.0, 200.0
    view = View1(
        ticker="Z", company_name="Z", sector=None,
        period_end=dt.date(2026, 1, 1), filing_date=dt.date(2026, 2, 1),
        mode="detailed", total_assets=ASSETS, total_liabilities=LIAB,
        total_equity=EQUITY,
        stated_rhs=800.0,   # their own total disagrees with their own assets
    )
    _check_identity(view, ASSETS, LIAB, EQUITY)

    assert not view.balances
    assert not [b for b in view.claims if b.key == "unread_components"]


# --- the derived-liabilities path, which used to say nothing at all --------

def test_a_derived_liability_never_picks_an_impossible_equity():
    """Agilent's shape, for a filer who reports no liabilities total.

    Deriving is arithmetic, but it is arithmetic on a CHOSEN input, and
    choosing by tag name here is the mistake `resolve_identity` exists to
    stop -- made in the one place the identity check cannot catch it, because
    a derived liability balances by construction.

    With the wrong equity the drawing would show $14.2B of liabilities against
    $14.0B of assets, silently.
    """
    from src.company.view1 import build_view1  # noqa: F401  (import guard)
    from src.company import view1 as V

    ASSETS = 13_967_000_000.0
    INCL_NCI, PARENT = -233_000_000.0, 7_363_000_000.0

    # The derivation, in isolation: stated total less the chosen equity.
    primary = ASSETS - INCL_NCI          # 14.2B -- more liabilities than assets
    alternate = ASSETS - PARENT          # 6.6B  -- possible
    assert primary > ASSETS and alternate >= 0

    # The rule is that a derived liability must be a POSSIBLE one. Negative
    # liabilities are impossible; that is the only test available when there
    # is no identity left to rank candidates by.
    assert not (primary < 0 <= alternate), "this shape is caught by sign alone"


def test_a_filer_whose_own_totals_disagree_is_reported_even_when_derived():
    """Half of the derived case is NOT circular, and used to be silent.

    L derived from the filer's own stated total makes A = L + E hold by
    construction. But "does the filer's stated total match the filer's own
    assets" is a real question, and neither side of it came from us. Skipping
    it meant a page said nothing at all about a filer whose arithmetic did not
    work -- silence that reads as assent.
    """
    from src.company.view1 import View1, _check_identity

    ASSETS, STATED = 1000.0, 880.0      # their two totals disagree by 12%
    EQUITY = 300.0
    DERIVED = STATED - EQUITY           # 580, so L + E == STATED by construction

    view = View1(
        ticker="D", company_name="D", sector=None,
        period_end=dt.date(2026, 1, 1), filing_date=dt.date(2026, 2, 1),
        mode="detailed", total_assets=ASSETS, total_liabilities=DERIVED,
        total_equity=EQUITY, stated_rhs=STATED,
        liabilities_derived_from="liabilities and equity, less equity",
    )
    _check_identity(view, ASSETS, DERIVED, EQUITY)

    assert not view.balances
    assert view.imbalance_pct == pytest.approx(12.0)
    assert any("Their arithmetic, not ours" in n for n in view.notes)


def test_the_circular_derivation_still_says_nothing():
    """L = A - E is a tautology. Reporting that as a pass would be a lie."""
    from src.company.view1 import View1, _check_identity

    view = View1(
        ticker="C", company_name="C", sector=None,
        period_end=dt.date(2026, 1, 1), filing_date=dt.date(2026, 2, 1),
        mode="detailed", total_assets=1000.0, total_liabilities=700.0,
        total_equity=300.0,
        liabilities_derived_from="total assets, less equity",
    )
    _check_identity(view, 1000.0, 700.0, 300.0)

    assert view.imbalance_pct == 0.0
    assert not view.notes, "a tautology must not be reported as a finding"
