"""Point-in-time / lookahead-bias tests.

This file is the difference between a real system and a toy. If any of these
fail, every backtest the system produces is worthless.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.storage.models import Fundamental
from src.storage.pit import (
    add_business_days,
    get_fundamentals,
    get_latest_fundamentals_wide,
    visible_from,
)


def _add(session, **kw):
    session.add(Fundamental(**kw))
    session.flush()


@pytest.fixture
def filed_q3(session):
    """A fiscal quarter ending 2025-09-30, not filed until 2025-11-08."""
    _add(
        session,
        ticker="ACME",
        metric="revenue",
        value=1000.0,
        period_end=dt.date(2025, 9, 30),
        fiscal_period="Q3",
        filing_date=dt.date(2025, 11, 8),
        source="sec",
    )
    return session


# ---------------------------------------------------------------------------
# THE test: nothing is readable before its filing date.
# ---------------------------------------------------------------------------
def test_fundamental_not_readable_before_filing_date(filed_q3):
    """A Q3 number filed on 2025-11-08 must be invisible on 2025-10-01.

    Using it earlier is lookahead bias, and the results built on it are a
    fantasy.
    """
    session = filed_q3
    invisible = get_fundamentals(session, "ACME", dt.date(2025, 10, 1))
    assert invisible.empty, "Q3 data leaked before its filing date"

    # Still invisible the day before filing.
    assert get_fundamentals(session, "ACME", dt.date(2025, 11, 7)).empty
    # And on the filing date itself, because of the ingestion-lag buffer.
    assert get_fundamentals(session, "ACME", dt.date(2025, 11, 8)).empty


def test_fundamental_readable_after_filing_plus_lag(filed_q3):
    session = filed_q3
    # 2025-11-08 is a Saturday; +2 business days lands on Tuesday 2025-11-11.
    visible_on = visible_from(dt.date(2025, 11, 8), 2)
    assert visible_on == dt.date(2025, 11, 11)

    assert get_fundamentals(session, "ACME", visible_on - dt.timedelta(days=1)).empty
    df = get_fundamentals(session, "ACME", visible_on)
    assert len(df) == 1
    assert df.iloc[0]["value"] == 1000.0


@pytest.mark.parametrize("as_of_offset", range(-30, 0))
def test_no_lookahead_across_a_range_of_dates(filed_q3, as_of_offset):
    """Sweep every date in the month before filing; none may see the data."""
    session = filed_q3
    as_of = dt.date(2025, 11, 8) + dt.timedelta(days=as_of_offset)
    df = get_fundamentals(session, "ACME", as_of)
    assert df.empty, f"leaked on {as_of}"


def test_wide_accessor_respects_pit(filed_q3):
    session = filed_q3
    wide = get_latest_fundamentals_wide(
        session, ["ACME"], dt.date(2025, 10, 1), ["revenue"]
    )
    assert wide.loc["ACME", "revenue"] != wide.loc["ACME", "revenue"]  # NaN

    wide2 = get_latest_fundamentals_wide(
        session, ["ACME"], dt.date(2025, 12, 1), ["revenue"]
    )
    assert wide2.loc["ACME", "revenue"] == 1000.0


# ---------------------------------------------------------------------------
# Restatements
# ---------------------------------------------------------------------------
def test_restatement_returns_original_figure_before_amendment(session):
    """An observer on 2025-12-01 knew the ORIGINAL number, not the restated one."""
    _add(
        session, ticker="RSTT", metric="revenue", value=1000.0,
        period_end=dt.date(2025, 9, 30), filing_date=dt.date(2025, 11, 10),
        source="sec", restated=False,
    )
    _add(
        session, ticker="RSTT", metric="revenue", value=850.0,
        period_end=dt.date(2025, 9, 30), filing_date=dt.date(2026, 2, 20),
        source="sec", restated=True,
    )

    before = get_fundamentals(session, "RSTT", dt.date(2025, 12, 1))
    assert len(before) == 1
    assert before.iloc[0]["value"] == 1000.0, "restated figure leaked backwards"

    after = get_fundamentals(session, "RSTT", dt.date(2026, 3, 15))
    assert len(after) == 1
    assert after.iloc[0]["value"] == 850.0


def test_sec_preferred_over_vendor_on_same_filing_date(session):
    for source, value in (("fmp", 990.0), ("sec", 1000.0)):
        _add(
            session, ticker="PREF", metric="revenue", value=value,
            period_end=dt.date(2025, 9, 30), filing_date=dt.date(2025, 11, 10),
            source=source, restated=(source == "fmp"),
        )
    df = get_fundamentals(session, "PREF", dt.date(2025, 12, 1))
    assert len(df) == 1
    assert df.iloc[0]["source"] == "sec"


# ---------------------------------------------------------------------------
# No forward-fill across reporting gaps
# ---------------------------------------------------------------------------
def test_no_forward_fill_across_reporting_gap(session):
    """A company that stops reporting must not have its last value carried."""
    _add(
        session, ticker="GAPS", metric="revenue", value=500.0,
        period_end=dt.date(2024, 3, 31), filing_date=dt.date(2024, 5, 1),
        source="sec",
    )
    df = get_fundamentals(session, "GAPS", dt.date(2025, 6, 1))
    # The old value is still readable (it was filed), but there is exactly one
    # record -- nothing has been synthesised for the missing quarters.
    assert len(df) == 1
    assert df.iloc[0]["period_end"] == dt.date(2024, 3, 31)

    wide = get_latest_fundamentals_wide(
        session, ["GAPS", "MISSING"], dt.date(2025, 6, 1), ["revenue", "eps"]
    )
    assert wide.loc["GAPS", "revenue"] == 500.0
    # A ticker with no data stays NaN, never the universe mean.
    assert wide.loc["MISSING", "revenue"] != wide.loc["MISSING", "revenue"]
    assert wide.loc["GAPS", "eps"] != wide.loc["GAPS", "eps"]


# ---------------------------------------------------------------------------
# Business-day arithmetic
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "start,n,expected",
    [
        (dt.date(2025, 11, 6), 2, dt.date(2025, 11, 10)),  # Thu +2 -> Mon
        (dt.date(2025, 11, 7), 2, dt.date(2025, 11, 11)),  # Fri +2 -> Tue
        (dt.date(2025, 11, 8), 2, dt.date(2025, 11, 11)),  # Sat +2 -> Tue
        (dt.date(2025, 11, 10), 0, dt.date(2025, 11, 10)),
        (dt.date(2025, 11, 10), 5, dt.date(2025, 11, 17)),
    ],
)
def test_add_business_days(start, n, expected):
    assert add_business_days(start, n) == expected
