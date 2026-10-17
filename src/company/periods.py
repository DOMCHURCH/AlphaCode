"""One quarter, one period end.

SEC's bulk Financial Statement Data Sets round every `ddate` (and `sub.period`)
to the NEAREST month end -- Apple's quarter that ended 2026-03-28 arrives as
2026-03-31, Costco's 2026-05-10 as 2026-04-30. The XBRL frames carry the real
date. A filing loaded by both routes is therefore stored twice, three days apart,
and `max(period_end)` returned whichever date was larger: the rounded one when the
rounding went forward (Apple, Nvidia, Adobe, J&J), the real one when it went
back (Deere). Read side and write side now agree on one rule:

    A date that sits on a month end, and has a date that does NOT sit on a month
    end 1 to 15 days away in the SAME filing, is the rounded copy of that date.

The real date always wins. Two real quarters are about 91 days apart, so they
can never be merged by this rule, and a calendar-aligned filer (every date a
month end) is never touched.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping

# SEC rounds to the nearest month end, so the real date is at most ~15 days away.
WINDOW_DAYS = 15


def is_month_end(d: dt.date) -> bool:
    return (d + dt.timedelta(days=1)).day == 1


def _nearest_real(month_end: dt.date, real: Iterable[dt.date]) -> list[dt.date]:
    near = [r for r in real if 1 <= abs((r - month_end).days) <= WINDOW_DAYS]
    if not near:
        return []
    best = min(abs((r - month_end).days) for r in near)
    return sorted({r for r in near if abs((r - month_end).days) == best})


def period_aliases(
    rows: Iterable[tuple[dt.date, dt.date]], *, cross_filing: bool = False
) -> dict[dt.date, dt.date]:
    """{rounded month end: real date} for ONE ticker.

    `rows` are (period_end, filing_date) pairs. The twin has to share the filing
    date. A month end that would map to two different real dates is left alone:
    guessing between them would be inventing a date.

    `cross_filing` additionally maps a month end that has no same-filing twin to
    a real date 1 to 15 days away from ANOTHER filing of the same ticker -- what
    the restatement and history views need, where the same quarter is quoted by
    later filings that were loaded by the other route. It still only ever moves
    a month end onto a non-month-end date.
    """
    by_filing: dict[dt.date, set[dt.date]] = {}
    everything: set[dt.date] = set()
    for pe, fd in rows:
        by_filing.setdefault(fd, set()).add(pe)
        everything.add(pe)

    found: dict[dt.date, set[dt.date]] = {}
    for dates in by_filing.values():
        real = [d for d in dates if not is_month_end(d)]
        for m in (d for d in dates if is_month_end(d)):
            for r in _nearest_real(m, real):
                found.setdefault(m, set()).add(r)
    out = {m: next(iter(rs)) for m, rs in found.items() if len(rs) == 1}

    if cross_filing:
        real_all = [d for d in everything if not is_month_end(d)]
        for m in (d for d in everything if is_month_end(d) and d not in out and d not in found):
            near = _nearest_real(m, real_all)
            if len(near) == 1:
                out[m] = near[0]
    return out


def canonical(period_end: dt.date, aliases: Mapping[dt.date, dt.date]) -> dt.date:
    return aliases.get(period_end, period_end)


def filing_twins(dates: Iterable[dt.date]) -> dict[dt.date, list[dt.date]]:
    """{month end: the real dates equally nearest to it} for the dates of ONE filing.

    `period_aliases` keeps only the months with exactly one such date; this returns
    them all, so a caller can tell "no twin" from "ambiguous" (two or more) and
    report the second instead of silently skipping it. A real date claimed by two
    different month ends of the same filing (only possible at exactly 15 days from
    both) is also a collision the caller must treat as ambiguous.
    """
    unique = set(dates)
    real = [d for d in unique if not is_month_end(d)]
    out: dict[dt.date, list[dt.date]] = {}
    for m in (d for d in unique if is_month_end(d)):
        near = _nearest_real(m, real)
        if near:
            out[m] = near
    return out


def newest_periods(rows: Iterable[tuple[str, dt.date, dt.date]]) -> dict[str, dt.date]:
    """{ticker: the newest CANONICAL period end} from (ticker, period_end, filing_date) rows.

    What `get_balance_sheet` returns as `period_end` for each ticker, computed for
    every ticker in one pass -- for callers that compare a stored page's period
    against "the newest period" and must not use a raw `max(period_end)`, which is
    the rounded date wherever the rounding went forward.
    """
    by_ticker: dict[str, list[tuple[dt.date, dt.date]]] = {}
    for ticker, pe, fd in rows:
        by_ticker.setdefault(ticker, []).append((pe, fd))
    out: dict[str, dt.date] = {}
    for ticker, pairs in by_ticker.items():
        aliases = period_aliases(pairs, cross_filing=True)   # the rule get_balance_sheet applies
        out[ticker] = max(aliases.get(pe, pe) for pe, _ in pairs)
    return out


def drop_rounded_month_ends(dates: Iterable[dt.date]) -> list[dt.date]:
    """`dates`, newest first, without the month ends that a real date stands behind.

    A month end with a non-month-end date 1 to 15 days away is that date's rounded
    copy (the same-quarter test `period_aliases` applies inside one filing, applied
    here across filings for the history view). Two non-month-end dates are never
    merged, whatever the gap, and neither are two month ends.
    """
    unique = set(dates)
    real = [d for d in unique if not is_month_end(d)]
    kept = [d for d in unique if not (is_month_end(d) and _nearest_real(d, real))]
    return sorted(kept, reverse=True)
