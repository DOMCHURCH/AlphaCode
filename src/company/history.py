"""Balance sheet history and "what changed" -- the Starter features.

Both are read straight off `fundamentals`, which already keeps every
(ticker, metric, period_end, filing_date) it has seen: one row per filing that
reported the figure. So a restatement is not something to detect upstream --
it is two rows for the same period with different values, and the table has
been holding them all along.

History reuses `get_balance_sheet(period_end=...)` per period rather than a
second assembly path, so a figure in the history is the same figure the
company page and /api/company return for that period.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from typing import Any

from sqlalchemy import and_, select

from src.storage.db import session_scope

# Balance sheet periods kept per response, whatever the tier allows: ten
# years of quarters plus slack. A bound on work, not a product limit.
MAX_PERIODS = 48

# The headline figures a period-over-period change is reported on. Keys of
# `BalanceSheet.assets/liabilities/equity` -- CONCEPT names, which differ from
# metric names for equity ("shareholders_equity" is metric "total_equity").
HEADLINE = (
    "total_assets", "total_liabilities", "shareholders_equity",
    "total_equity_incl_nci", "cash", "current_assets", "current_liabilities",
    "long_term_debt",
)


# Two period ends this close together are one quarter reported twice. SEC's
# bulk datasets round `ddate` to the month end (Apple's 2026-03-28 quarter
# arrives as 2026-03-31) while the XBRL frames carry the real date, so the
# same balance sheet otherwise shows up as two periods three days apart.
SAME_QUARTER_DAYS = 10


def _periods(
    ticker: str, since: dt.date | None, as_of: dt.date | None = None
) -> tuple[list[dt.date], dt.date | None]:
    """Periods with a total-assets figure, newest first, and the oldest held.

    Near-duplicate period ends (see SAME_QUARTER_DAYS) collapse to the one
    with the most figures behind it.
    """
    from sqlalchemy import func

    from src.storage.models import Fundamental

    with session_scope() as session:
        known = Fundamental.filing_date <= (as_of or dt.date.today())
        with_assets = set(session.execute(
            select(Fundamental.period_end)
            .where(and_(Fundamental.ticker == ticker,
                        Fundamental.metric == "total_assets", known))
            .distinct()
        ).scalars().all())
        counts = dict(session.execute(
            select(Fundamental.period_end, func.count(func.distinct(Fundamental.metric)))
            .where(and_(Fundamental.ticker == ticker, known))
            .group_by(Fundamental.period_end)
        ).all())
    held: list[dt.date] = []
    for p in sorted(with_assets, reverse=True):
        if held and (held[-1] - p).days <= SAME_QUARTER_DAYS:
            if counts.get(p, 0) > counts.get(held[-1], 0):
                held[-1] = p
            continue
        held.append(p)
    oldest = held[-1] if held else None
    if since is not None:
        held = [p for p in held if p >= since]
    return held[:MAX_PERIODS], oldest


def history(
    ticker: str, years: int | None, as_of: dt.date | None = None
) -> dict[str, Any] | None:
    """Every loaded balance sheet for `ticker` in the last `years` (None = all).

    `available_from` is the oldest period the database holds for this company
    regardless of the caller's plan, so "you asked for ten years and there are
    three" reads as exactly that rather than as a silent short answer.

    `as_of` answers as of that date: only filings made by then, and `years`
    counted back from it rather than from today.
    """
    from src.company.balancesheet import get_balance_sheet
    from src.company.lookup import canonical_ticker

    symbol = canonical_ticker(ticker).upper()
    anchor = as_of or dt.date.today()
    since = (
        anchor - dt.timedelta(days=int(years * 365.25))
        if years else None
    )
    periods, oldest = _periods(symbol, since, as_of)
    if oldest is None:
        return None
    sheets = []
    for p in periods:
        sheet = get_balance_sheet(symbol, as_of=anchor, period_end=p)
        if sheet is not None:
            sheets.append(asdict(sheet))
    return {
        "ticker": symbol,
        "as_of": as_of.isoformat() if as_of else None,
        "years_requested": years,
        "periods": len(sheets),
        "available_from": oldest.isoformat(),
        "balance_sheets": sheets,
    }


def _headline(sheet: Any) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for section in (sheet.assets, sheet.liabilities, sheet.equity):
        for key, v in section.items():
            if key in HEADLINE:
                out[key] = v.value
    return out


def _delta(new: float | None, old: float | None) -> dict[str, Any]:
    change = None if new is None or old is None else new - old
    pct = (
        round(change / abs(old) * 100, 2)
        if change is not None and old not in (None, 0) else None
    )
    return {"previous": old, "current": new, "change": change, "change_pct": pct}


def restatements(ticker: str, years: int = 3) -> list[dict[str, Any]]:
    """Figures a later filing changed for a period an earlier filing reported.

    One entry per (metric, period) whose value differs between its first and
    latest filing. Equal re-reports -- every 10-K repeats last year's balance
    sheet as a comparative -- are not restatements and are left out.
    """
    from src.company.balancesheet import BALANCE_SHEET_CONCEPTS
    from src.storage.models import Fundamental

    metrics = set(BALANCE_SHEET_CONCEPTS.values())
    since = dt.date.today() - dt.timedelta(days=int(years * 365.25))
    with session_scope() as session:
        rows = session.execute(
            select(Fundamental)
            .where(and_(Fundamental.ticker == ticker,
                        Fundamental.period_end >= since,
                        Fundamental.metric.in_(metrics)))
            .order_by(Fundamental.filing_date.asc())
        ).scalars().all()
        grouped: dict[tuple[str, dt.date], list[Any]] = {}
        for r in rows:
            if r.value is None:
                continue
            grouped.setdefault((r.metric, r.period_end), []).append(
                (r.filing_date, float(r.value))
            )
    out = []
    for (metric, period), seen in grouped.items():
        first_date, first = seen[0]
        last_date, last = seen[-1]
        if first_date == last_date or abs(last - first) <= max(1.0, abs(first) * 1e-9):
            continue
        entry = _delta(last, first)
        entry.update({
            "metric": metric, "period_end": period.isoformat(),
            "originally_filed": first_date.isoformat(),
            "revised_in_filing": last_date.isoformat(),
        })
        out.append(entry)
    out.sort(key=lambda e: (e["period_end"], e["metric"]), reverse=True)
    return out


def changes(ticker: str) -> dict[str, Any] | None:
    """What moved between the last two periods, and what got restated."""
    from src.company.balancesheet import get_balance_sheet
    from src.company.lookup import canonical_ticker

    symbol = canonical_ticker(ticker).upper()
    periods, _oldest = _periods(symbol, None)
    if not periods:
        return None
    latest = get_balance_sheet(symbol, period_end=periods[0])
    if latest is None:
        return None
    previous = get_balance_sheet(symbol, period_end=periods[1]) if len(periods) > 1 else None
    now = _headline(latest)
    before = _headline(previous) if previous else {}
    return {
        "ticker": symbol,
        "period_end": latest.period_end.isoformat(),
        "filing_date": latest.filing_date.isoformat(),
        "previous_period_end": previous.period_end.isoformat() if previous else None,
        "since_previous_period": {
            k: _delta(now.get(k), before.get(k))
            for k in HEADLINE if k in now or k in before
        } if previous else {},
        "data_quality_issues": list(latest.data_quality_issues or []),
        "restatements": restatements(symbol),
    }
