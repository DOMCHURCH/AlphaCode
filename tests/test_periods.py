"""One quarter, one period end: the helper and the accession lookup behind it.

SEC's bulk datasets round `ddate` to the NEAREST month end; the XBRL frames
carry the real date. The cases below are the real filings the rule was drawn
from (dates checked against EDGAR's `reportDate`), not invented ones:

    AAPL  real 2026-03-28  bulk 2026-03-31  (+3)
    NVDA  real 2026-04-26  bulk 2026-04-30  (+4)
    ADBE  real 2026-05-29  bulk 2026-05-31  (+2)
    JNJ   real 2026-03-29  bulk 2026-03-31  (+2)
    DE    real 2026-05-03  bulk 2026-04-30  (-3)   rounded BACK, not forward
    COST  real 2026-05-10  bulk 2026-04-30  (-10)  the edge of the window
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from src.company.periods import (
    WINDOW_DAYS,
    canonical,
    drop_rounded_month_ends,
    is_month_end,
    period_aliases,
)
from src.ingest.sec_periods import (
    pick_real_period,
    quarters_for,
    real_periods_for,
    real_periods_for_sync,
    real_periods_from_frame,
)

D = dt.date

# (ticker, real period end, bulk period end, filing date)
REAL_FILINGS = [
    ("AAPL", D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)),
    ("NVDA", D(2026, 4, 26), D(2026, 4, 30), D(2026, 5, 20)),
    ("ADBE", D(2026, 5, 29), D(2026, 5, 31), D(2026, 6, 15)),
    ("JNJ", D(2026, 3, 29), D(2026, 3, 31), D(2026, 4, 22)),
    ("DE", D(2026, 5, 3), D(2026, 4, 30), D(2026, 5, 28)),
    ("COST", D(2026, 5, 10), D(2026, 4, 30), D(2026, 6, 3)),
]


def test_window_is_fifteen_days():
    assert WINDOW_DAYS == 15


def test_month_end_test():
    assert is_month_end(D(2026, 4, 30))
    assert is_month_end(D(2024, 2, 29))   # leap year
    assert is_month_end(D(2025, 2, 28))
    assert not is_month_end(D(2026, 3, 28))
    assert not is_month_end(D(2026, 5, 10))


@pytest.mark.parametrize("ticker,real,bulk,filed", REAL_FILINGS)
def test_a_rounded_month_end_maps_to_the_real_date(ticker, real, bulk, filed):
    """Forward (+3, +4, +2), backward (-3) and the -10 edge all resolve to the
    real date, whichever side of it the rounding fell on."""
    aliases = period_aliases([(real, filed), (bulk, filed)])
    assert aliases == {bulk: real}
    assert canonical(bulk, aliases) == real
    assert canonical(real, aliases) == real


def test_deere_keeps_its_real_date_when_the_real_date_is_the_earlier_one():
    """DE is the case `max(period_end)` got RIGHT by accident (the real date is
    the larger one); it must keep that answer after the fix, not flip to the
    rounded 04-30."""
    aliases = period_aliases([(D(2026, 5, 3), D(2026, 5, 28)), (D(2026, 4, 30), D(2026, 5, 28))])
    assert canonical(D(2026, 4, 30), aliases) == D(2026, 5, 3)
    assert canonical(D(2026, 5, 3), aliases) == D(2026, 5, 3)


def test_costco_at_minus_ten_is_inside_the_window():
    assert abs((D(2026, 4, 30) - D(2026, 5, 10)).days) == 10 <= WINDOW_DAYS
    aliases = period_aliases([(D(2026, 5, 10), D(2026, 6, 3)), (D(2026, 4, 30), D(2026, 6, 3))])
    assert aliases == {D(2026, 4, 30): D(2026, 5, 10)}


def test_the_window_has_an_edge():
    inside = period_aliases([(D(2026, 4, 15), D(2026, 6, 1)), (D(2026, 4, 30), D(2026, 6, 1))])
    assert inside == {D(2026, 4, 30): D(2026, 4, 15)}          # 15 days: merged
    outside = period_aliases([(D(2026, 4, 14), D(2026, 6, 1)), (D(2026, 4, 30), D(2026, 6, 1))])
    assert outside == {}                                        # 16 days: not


@pytest.mark.parametrize("quarters", [
    # Microsoft-style: every date a month end.
    [D(2025, 6, 30), D(2025, 9, 30), D(2025, 12, 31), D(2026, 3, 31), D(2026, 6, 30)],
    # A single filing with a single date.
    [D(2026, 6, 30)],
])
def test_calendar_aligned_filers_are_unchanged(quarters):
    rows = [(q, q + dt.timedelta(days=35)) for q in quarters]
    assert period_aliases(rows) == {}
    assert period_aliases(rows, cross_filing=True) == {}
    assert drop_rounded_month_ends(quarters) == sorted(quarters, reverse=True)


def test_a_calendar_filer_with_two_dates_in_one_filing_is_not_merged():
    """Month end next to month end is two different dates; only a month end next
    to a NON-month-end can be a rounded copy."""
    f = D(2026, 5, 1)
    assert period_aliases([(D(2026, 3, 31), f), (D(2026, 2, 28), f)]) == {}


def test_two_real_quarters_about_91_days_apart_are_never_merged():
    """Apple's Q1 and Q2 (2025-12-27, 2026-03-28), each loaded twice. Each
    rounded date goes to ITS OWN real date; the two real dates stay apart, in the
    same-filing rule, the cross-filing rule and the history collapse alike."""
    q1_real, q1_bulk = D(2025, 12, 27), D(2025, 12, 31)
    q2_real, q2_bulk = D(2026, 3, 28), D(2026, 3, 31)
    assert (q2_real - q1_real).days == 91
    f1, f2 = D(2026, 1, 30), D(2026, 5, 1)
    rows = [(q1_real, f1), (q1_bulk, f1), (q2_real, f2), (q2_bulk, f2)]
    for cross in (False, True):
        assert period_aliases(rows, cross_filing=cross) == {q1_bulk: q1_real, q2_bulk: q2_real}
    assert drop_rounded_month_ends([q1_real, q1_bulk, q2_real, q2_bulk]) == [q2_real, q1_real]


def test_a_month_end_is_not_pulled_across_a_quarter_boundary():
    """A rounded 2026-03-31 sits 94 days from a real 2025-12-27 of another
    filing: nowhere near the window, so cross-filing aliasing leaves it alone."""
    rows = [(D(2025, 12, 27), D(2026, 1, 30)), (D(2026, 3, 31), D(2026, 5, 1))]
    assert period_aliases(rows, cross_filing=True) == {}


def test_real_dates_are_never_moved_even_when_close():
    """Two non-month-end dates 7 days apart are two facts. Nothing merges them."""
    f = D(2026, 6, 1)
    rows = [(D(2026, 3, 21), f), (D(2026, 3, 28), f)]
    assert period_aliases(rows) == {}
    assert period_aliases(rows, cross_filing=True) == {}
    assert drop_rounded_month_ends([D(2026, 3, 21), D(2026, 3, 28)]) == [D(2026, 3, 28), D(2026, 3, 21)]


def test_the_twin_must_share_the_filing_date():
    """The same-filing rule is the strict one: a month end in filing A with the
    real date only in filing B is NOT merged by it (cross_filing=True does)."""
    a, b = D(2026, 5, 1), D(2026, 8, 1)
    rows = [(D(2026, 3, 31), a), (D(2026, 3, 28), b)]
    assert period_aliases(rows) == {}
    assert period_aliases(rows, cross_filing=True) == {D(2026, 3, 31): D(2026, 3, 28)}


def test_cross_filing_never_overrides_a_same_filing_twin():
    f1, f2 = D(2026, 5, 1), D(2026, 8, 1)
    rows = [(D(2026, 3, 31), f1), (D(2026, 3, 28), f1), (D(2026, 3, 29), f2)]
    # 03-31 has a same-filing twin 03-28; the other filing's 03-29 does not vote.
    assert period_aliases(rows, cross_filing=True) == {D(2026, 3, 31): D(2026, 3, 28)}


def test_an_ambiguous_month_end_is_left_alone():
    """03-31 with real dates 03-28 and 04-03 (both three days away) would be a
    guess between two dates, so nothing is invented."""
    f = D(2026, 5, 1)
    rows = [(D(2026, 3, 31), f), (D(2026, 3, 28), f), (D(2026, 4, 3), f)]
    assert period_aliases(rows) == {}


def test_nearest_real_date_wins_when_two_are_in_range():
    f = D(2026, 5, 1)
    rows = [(D(2026, 3, 31), f), (D(2026, 3, 28), f), (D(2026, 3, 21), f)]
    assert period_aliases(rows) == {D(2026, 3, 31): D(2026, 3, 28)}


def test_drop_rounded_month_ends_keeps_the_real_date_and_orders_newest_first():
    dates = [D(2026, 3, 31), D(2026, 3, 28), D(2025, 12, 31), D(2025, 9, 30), D(2025, 9, 27)]
    assert drop_rounded_month_ends(dates) == [D(2026, 3, 28), D(2025, 12, 31), D(2025, 9, 27)]


# ---- ingest side: accession -> real period end -----------------------------------------------------------------

@pytest.mark.parametrize("ticker,real,bulk,filed", REAL_FILINGS)
def test_pick_real_period_accepts_the_real_date_for_every_case(ticker, real, bulk, filed):
    assert pick_real_period(bulk, real) == real


def test_pick_real_period_falls_back_to_the_rounded_date():
    assert pick_real_period(D(2026, 3, 31), None) == D(2026, 3, 31)


def test_pick_real_period_refuses_a_date_from_another_quarter():
    """A frame row for the same accession but a different fact (the prior-year
    comparative) is not an un-rounding."""
    assert pick_real_period(D(2026, 3, 31), D(2025, 3, 29)) == D(2026, 3, 31)
    assert pick_real_period(D(2026, 4, 30), D(2026, 4, 14)) == D(2026, 4, 30)   # 16 days
    assert pick_real_period(D(2026, 4, 30), D(2026, 4, 15)) == D(2026, 4, 15)   # 15 days


def test_real_periods_from_frame_reads_accn_and_end_and_skips_junk():
    data = [
        {"accn": "0000320193-26-000011", "end": "2026-03-28", "val": 1},
        {"accn": "0001045810-26-000044", "end": "2026-04-26T00:00:00"},
        {"accn": None, "end": "2026-01-01"},
        {"accn": "x", "end": None},
        {"accn": "bad", "end": "not-a-date"},
    ]
    assert real_periods_from_frame(data) == {
        "0000320193-26-000011": D(2026, 3, 28),
        "0001045810-26-000044": D(2026, 4, 26),
    }


def test_quarters_for_spans_the_quarter_and_the_one_before():
    assert quarters_for(2026, 2) == [(2026, 2), (2026, 1)]
    assert quarters_for(2026, 1) == [(2026, 1), (2025, 4)]


def test_real_periods_for_merges_quarters_and_survives_a_failing_fetch():
    calls: list[str] = []

    async def fetch(tag, uom, ccp):
        calls.append(ccp)
        if ccp == "CY2026Q1I":
            raise RuntimeError("rate limited")
        return [{"accn": "a-1", "end": "2026-05-10"}]

    got = asyncio.run(real_periods_for(2026, 2, fetch))
    assert got == {"a-1": D(2026, 5, 10)}
    assert calls == ["CY2026Q2I", "CY2026Q1I"]


def test_real_periods_for_returns_empty_not_an_exception_when_everything_fails():
    async def fetch(tag, uom, ccp):
        raise RuntimeError("down")

    assert asyncio.run(real_periods_for(2026, 2, fetch)) == {}


def test_real_periods_for_sync_works_inside_and_outside_a_running_loop():
    async def fetch(tag, uom, ccp):
        return [{"accn": "a-1", "end": "2026-03-28"}]

    assert real_periods_for_sync(2026, 2, fetch) == {"a-1": D(2026, 3, 28)}

    async def inside():
        return real_periods_for_sync(2026, 2, fetch)

    assert asyncio.run(inside()) == {"a-1": D(2026, 3, 28)}


# ---- the newest period per ticker (what a page-staleness check must compare against) --------------------------

def test_newest_periods_is_the_canonical_date_not_the_raw_max():
    """AAPL rounded forward (raw max is the month end), DE rounded back (raw max is
    already the real date), MSFT a calendar filer, and a ticker with only a lone
    rounded row (nothing to prove it is rounded, so it stays as filed)."""
    from src.company.periods import newest_periods

    rows = [
        ("AAPL", D(2026, 6, 30), D(2026, 7, 31)), ("AAPL", D(2026, 6, 27), D(2026, 7, 31)),
        ("AAPL", D(2026, 3, 31), D(2026, 5, 1)),
        ("DE", D(2026, 8, 2), D(2026, 8, 27)), ("DE", D(2026, 7, 31), D(2026, 8, 27)),
        ("MSFT", D(2026, 6, 30), D(2026, 7, 30)), ("MSFT", D(2026, 3, 31), D(2026, 4, 30)),
        ("LONE", D(2026, 3, 31), D(2026, 5, 1)),
    ]
    assert newest_periods(rows) == {
        "AAPL": D(2026, 6, 27), "DE": D(2026, 8, 2), "MSFT": D(2026, 6, 30), "LONE": D(2026, 3, 31),
    }
