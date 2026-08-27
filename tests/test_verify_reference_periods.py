"""A reference figure is a fact about one period, and must be checked against it.

The bug these lock down: `verify_companies` held figures read off a num.txt
dump for period 2025-12-31 and compared them against `get_balance_sheet`, which
returns the NEWEST period. Once JPM's 2026-03-31 10-Q loaded, a completely
correct extraction reported

    total_assets  actual 4,900,475,000,000  expected 4,424,900,000,000  10.75%

and the run said "the parser is wrong". It was not: 4,900,475,000,000 is the
consolidated fact for the newer period, its balance identity is clean, and
total_equity passed at 0.44% only because a bank's equity barely moves between
quarters while its balance sheet does.

That failure mode has three properties worth naming, because together they look
exactly like a dimensional-selection bug and are not one: it hits the fastest
growing metric first, it always overstates (balance sheets accumulate), and it
gets worse every quarter that loads. So these tests assert on the mechanism --
which period was compared -- and not merely on a passing number.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.company.verify import REFERENCE, Reference, verify_companies

DEC = dt.date(2025, 12, 31)   # the period JPM's confirmed references were read for
MAR = dt.date(2026, 3, 31)    # the newer period that was loaded afterwards
FILED_DEC = dt.date(2026, 2, 13)
FILED_MAR = dt.date(2026, 5, 8)
AS_OF = dt.date(2026, 8, 27)

# The real confirmed figures, and the real Q1 ones a correct parser produces.
JPM_DEC_ASSETS = 4_424_900_000_000
JPM_DEC_EQUITY = 362_438_000_000
JPM_MAR_ASSETS = 4_900_475_000_000
JPM_MAR_EQUITY = 364_038_000_000


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'v.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed(rows: list[tuple]) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        for ticker, metric, value, period_end, filing_date in rows:
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=float(value),
                period_end=period_end, filing_date=filing_date,
                source="sec", restated=False,
            ))


def _jpm_both_quarters() -> list[tuple]:
    """Two correctly-extracted JPM balance sheets. Both identities balance."""
    return [
        ("JPM", "total_assets", JPM_DEC_ASSETS, DEC, FILED_DEC),
        ("JPM", "total_equity", JPM_DEC_EQUITY, DEC, FILED_DEC),
        ("JPM", "liabilities_and_equity", JPM_DEC_ASSETS, DEC, FILED_DEC),
        ("JPM", "total_assets", JPM_MAR_ASSETS, MAR, FILED_MAR),
        ("JPM", "total_equity", JPM_MAR_EQUITY, MAR, FILED_MAR),
        ("JPM", "liabilities_and_equity", JPM_MAR_ASSETS, MAR, FILED_MAR),
    ]


def test_a_newer_period_does_not_fail_a_reference_read_for_an_older_one(db):
    """The reported bug, end to end.

    Both quarters are extracted correctly. The newer one is the one
    `get_balance_sheet` returns. Verification must still compare against the
    period its reference was read for, and pass.
    """
    _seed(_jpm_both_quarters())

    result = verify_companies(as_of=AS_OF)
    jpm = result["companies"]["JPM"]

    assets = jpm["metrics"]["total_assets"]
    assert assets["compared_period_end"] == DEC.isoformat()
    assert assets["actual"] == JPM_DEC_ASSETS
    assert assets["drift_pct"] == 0.0
    assert assets["passed"]
    # The newer period is still reported, so the operator can see both.
    assert assets["latest_period_end"] == MAR.isoformat()
    assert jpm["passed"]


def test_the_unpinned_comparison_is_what_produced_10_75_percent(db):
    """Name the arithmetic, so nobody re-derives it as an extraction bug.

    Comparing the confirmed 2025-12-31 figure against the 2026-03-31 balance
    sheet is what produced the reported 10.75% miss. This asserts the drift is
    period arithmetic and nothing else -- and that equity, which moves far less
    between quarters, slips under the same tolerance and hides the cause.
    """
    assets_drift = abs(JPM_MAR_ASSETS - JPM_DEC_ASSETS) / JPM_DEC_ASSETS
    equity_drift = abs(JPM_MAR_EQUITY - JPM_DEC_EQUITY) / JPM_DEC_EQUITY

    assert round(assets_drift * 100, 2) == 10.75
    assert round(equity_drift * 100, 2) == 0.44
    # Which is exactly why only one of them was ever reported as a failure.
    from src.company.verify import TOLERANCE
    assert assets_drift > TOLERANCE
    assert equity_drift <= TOLERANCE


def test_the_check_still_catches_a_genuinely_wrong_fact(db):
    """Period-matching must not turn the check into a rubber stamp.

    Same period, wrong value -- an EMEA-sized Assets row selected for
    2025-12-31 -- still fails, and the summary still names the parser.
    """
    _seed([
        ("JPM", "total_assets", 641_190_000_000, DEC, FILED_DEC),
        ("JPM", "total_equity", JPM_DEC_EQUITY, DEC, FILED_DEC),
    ])

    result = verify_companies(as_of=AS_OF)
    jpm = result["companies"]["JPM"]

    assert jpm["metrics"]["total_assets"]["compared_period_end"] == DEC.isoformat()
    assert not jpm["metrics"]["total_assets"]["passed"]
    assert not jpm["passed"]
    assert "JPM.total_assets" in result["summary"]
    assert "parser is wrong" in result["summary"]


def test_a_missing_reference_period_is_unverifiable_not_a_parser_failure(db):
    """Only the newer quarter is loaded, so there is nothing to compare against.

    "We did not check this" and "the parser is wrong" must not print the same
    way; a targeted load that skipped a quarter is not an extraction bug.
    """
    _seed([r for r in _jpm_both_quarters() if r[3] == MAR])

    result = verify_companies(as_of=AS_OF)
    jpm = result["companies"]["JPM"]

    assets = jpm["metrics"]["total_assets"]
    assert assets["verdict"] == "reference period not loaded — cannot verify"
    assert assets["drift_pct"] is None
    assert "JPM.total_assets" in result["unverifiable"]
    # Not counted as the parser disagreeing with a reference.
    assert "parser is wrong" not in result["summary"]


def test_an_unconfirmed_reference_cannot_fail_the_run(db):
    """WMT's ~$260B is a recollection with no period basis.

    It is over a year stale against the newest loaded period and reads ~11%
    low. A remembered round number is not evidence about a parser, so it must
    report as needing a dump rather than as a data failure.
    """
    _seed([
        ("WMT", "total_assets", 289_607_000_000, dt.date(2026, 4, 30),
         dt.date(2026, 5, 15)),
        ("WMT", "liabilities_and_equity", 289_607_000_000, dt.date(2026, 4, 30),
         dt.date(2026, 5, 15)),
    ])

    result = verify_companies(as_of=AS_OF)
    wmt = result["companies"]["WMT"]
    assets = wmt["metrics"]["total_assets"]

    assert not assets["passed"]
    assert assets["period_matched"] is False
    assert assets["verdict"] == "reference unconfirmed — dump to settle"
    assert wmt["passed"], "an unconfirmed reference must not fail its company"
    assert "WMT.total_assets" in result["unconfirmed_mismatches"]


def test_a_confirmed_reference_must_carry_the_period_it_was_read_for():
    """The invariant that keeps this bug from being reintroduced.

    A figure read off a dump is a fact about one balance sheet. Accepting one
    without its period is what let a 2025-12-31 fact be checked against
    2026-03-31 for a quarter before anyone noticed.
    """
    with pytest.raises(ValueError, match="period_end"):
        Reference(1_000.0, "num.txt dump", confirmed=True)


def test_every_confirmed_reference_in_the_live_set_is_pinned():
    for ticker, checks in REFERENCE.items():
        for metric, ref in checks.items():
            if ref.confirmed:
                assert ref.period_end is not None, f"{ticker}.{metric}"


def test_a_missing_ticker_is_reported_as_coverage_not_as_a_parser_bug(db):
    """`all_passed` has several causes and the summary must name the real one.

    Printing "the parser is wrong" for a ticker that was never loaded sends the
    reader to debug an extractor that is working correctly.
    """
    _seed(_jpm_both_quarters())  # JPM only; MSFT/WMT/FCX/AAL absent

    result = verify_companies(as_of=AS_OF)
    assert not result["passed"]
    assert "no fundamentals loaded for" in result["summary"]
    assert "parser is wrong" not in result["summary"]
