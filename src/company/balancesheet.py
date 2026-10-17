"""Balance sheet query layer for company explainer.

Given a ticker, returns the most recent balance sheet data with point-in-time
correctness, highlighting missing concepts and data quality.

Data strategy: Use totals that actually populate (total_assets, total_equity,
etc.) rather than trying to sum components. Component concepts (goodwill,
inventory, receivables, PPE) can be added when they're backfilled.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import structlog

from src.company.periods import is_month_end, period_aliases
from src.storage.db import session_scope

log = structlog.get_logger(__name__)


# Map from display names to fundamentals table metric names.
# Focus on metrics that actually populate from SEC XBRL backfill.
BALANCE_SHEET_CONCEPTS = {
    # Assets
    "total_assets": "total_assets",
    "current_assets": "current_assets",
    "cash": "cash",
    "receivables": "receivables",
    "inventory": "inventory",
    "property_plant_equipment": "property_plant_equipment",
    "goodwill": "goodwill",
    "intangibles": "intangibles",
    # Bank-specific assets.
    "loans": "loans",
    "trading_securities": "trading_securities",
    "investment_securities": "investment_securities",
    "interbank_deposits": "interbank_deposits",
    # Liabilities
    "total_liabilities": "total_liabilities",
    # The filer's own stated right-hand side, where they report it.
    "liabilities_and_equity": "liabilities_and_equity",
    "current_liabilities": "current_liabilities",
    "long_term_debt": "long_term_debt",
    "accounts_payable": "accounts_payable",
    # Bank-specific claims.
    "deposits": "deposits",
    "short_term_borrowings": "short_term_borrowings",
    # Equity. Parent-only and NCI-inclusive are both carried: the first is
    # what "shareholders' equity" means to a reader, the second is what the
    # accounting identity balances against.
    "shareholders_equity": "total_equity",
    "total_equity_incl_nci": "total_equity_incl_nci",
    "minority_interest": "minority_interest",
    # Mezzanine (temporary) equity: presented between liabilities and permanent
    # equity, so it is in neither term of a plain A = L + E and its absence
    # reads as identity drift on a filing that is fine. `temporary_equity` is
    # the section total; `redeemable_preferred_stock` is a component of it.
    "temporary_equity": "temporary_equity",
    "redeemable_preferred_stock": "redeemable_preferred_stock",
    # Also a component of the section: a noncontrolling interest the holder
    # can put back to the company, which is what carries it out of permanent
    # equity. Not reached by `total_equity_incl_nci` nor by
    # `minority_interest` -- a different line about a different holder.
    "redeemable_noncontrolling_interest": "redeemable_noncontrolling_interest",
    # UPREIT operating-partnership units held by outside partners: outside
    # permanent equity, and not reached by `minority_interest`.
    "minority_interest_operating_partnership": "minority_interest_operating_partnership",
}


@dataclass
class BalanceSheetValue:
    """One line item with data quality metadata."""
    concept: str  # Display name
    value: float | None  # In USD
    period_end: dt.date | None
    filing_date: dt.date | None
    missing: bool = False
    restated: bool = False


@dataclass
class BalanceSheet:
    """Complete balance sheet for a company as of a date."""
    ticker: str
    company_name: str | None
    # SEC's previous name for this CIK, and when it stopped applying. The
    # page shows them because `company_name` is today's name over filings
    # made under the old one.
    former_name: str | None
    former_name_until: dt.date | None
    period_end: dt.date
    filing_date: dt.date
    assets: dict[str, BalanceSheetValue]  # concept -> value
    liabilities: dict[str, BalanceSheetValue]
    equity: dict[str, BalanceSheetValue]
    missing_concepts: list[str]
    data_quality_issues: list[str]  # e.g., negative equity, zero assets


# Which source wins when two filings carry the same date. Same order and same
# reasoning as `pit.get_fundamentals`: SEC as-reported beats a vendor's
# restated figure, because as-reported is what this site claims to show.
_SOURCE_PREFERENCE: tuple[str, ...] = ("sec", "fmp", "yahoo")


def _recency(f: Any) -> tuple[dt.date, int, int]:
    """Sort key for "which filing of this metric is the one to show".

    Later filing wins; on a tie, the more-preferred source wins; on a further tie
    the row carrying the real period end wins over its month-end-rounded twin
    (see src/company/periods.py), which would otherwise be decided by row order.
    Returned as a tuple so `max()` orders on all three without a second pass.
    """
    rank = {s: i for i, s in enumerate(_SOURCE_PREFERENCE)}
    return (f.filing_date, -rank.get(f.source, 99), 0 if is_month_end(f.period_end) else 1)


def _resolve_restatements(rows: list[Any]) -> dict[str, Any]:
    """One row per metric: the LATEST filing, not the earliest.

    This is the bug that made the site quietly disagree with itself. The query
    orders `filing_date.desc()` and the collapse was
    `{f.metric: f for f in rows}` -- last write wins, so descending order meant
    the EARLIEST filing survived and every restatement was discarded. A company
    that revised its balance sheet showed its original figures on the drawing,
    on the company page and through the API, while `pit.get_fundamentals`
    returned the revised ones for the same ticker and period.

    `src/storage/pit.py` states the contract in its own docstring -- "we take
    the one with the LATEST filing_date" -- and says nothing else may query the
    table directly. This path does, so it now applies the same policy: latest
    filing, source preference breaking ties.
    """
    winners: dict[str, Any] = {}
    for f in rows:
        held = winners.get(f.metric)
        if held is None or _recency(f) > _recency(held):
            winners[f.metric] = f
    return winners


def _first_reported(rows: list[Any], winners: dict[str, Any]) -> dict[str, dt.date]:
    """For each metric, the filing that FIRST reported the value being shown.

    `_resolve_restatements` picks the latest filing per metric, and that is the
    right VALUE: a restatement must beat the original. But it is the wrong
    PROVENANCE. A figure a later filing merely repeats (every 10-Q repeats the
    prior year end as a comparative, every 10-K and 10-Q carries the equity
    statement's opening balance) has the same value in two filings, and the
    sheet then said it was "filed" on the later one: Apple's 2026-03-28 equity
    read 2026-07-31 beside assets that read 2026-05-01, and the header took the
    newest of them. A year later the same sheet would have said a year later.

    So the date is the earliest filing in the unbroken run of filings that
    report the winning value (same source). A real restatement still moves the
    date: the run starts at the filing that introduced the new value.
    """
    by_metric: dict[str, list[Any]] = {}
    for f in rows:
        by_metric.setdefault(f.metric, []).append(f)
    out: dict[str, dt.date] = {}
    for metric, win in winners.items():
        same_source = sorted(
            (r for r in by_metric.get(metric, []) if r.source == win.source),
            key=lambda r: r.filing_date,
        )
        at = [i for i, r in enumerate(same_source) if r.filing_date == win.filing_date]
        if not at:
            out[metric] = win.filing_date
            continue
        k = max(at)
        while k > 0 and same_source[k - 1].value == win.value:
            k -= 1
        out[metric] = same_source[k].filing_date
    return out


def get_balance_sheet(
    ticker: str,
    as_of: dt.date | None = None,
    period_end: dt.date | None = None,
) -> BalanceSheet | None:
    """Fetch a balance sheet for a ticker.

    Returns None if the ticker has no usable fundamentals data.
    Point-in-time: only figures FILED on or before `as_of` are read, so the
    answer is what an observer could have known that day -- the original
    figure before a restatement was filed, the restated one after. Until
    2026-09-24 this filtered on `period_end` alone, which let a filing made
    after `as_of` leak into an "as of" answer (lookahead).

    `period_end` pins the answer to ONE reporting period instead of "whichever
    is newest". A caller holding a figure read off a specific filing -- the
    reference set in `src.company.verify` is the whole reason this exists --
    must compare against that period or it is not comparing like with like: a
    balance sheet grows between filings, so an unpinned comparison reports the
    passage of time as an extraction error. Returns None when that period is
    not loaded, which is a different answer from "this ticker has no data" and
    must not be collapsed into one.
    """
    if as_of is None:
        as_of = dt.date.today()

    # A filer with several share classes has ONE set of rows, stored under one
    # canonical ticker -- see `company.lookup.canonical_ticker`. Resolving here
    # rather than at each call site means every reader of a balance sheet gets
    # the same answer for `RDI` and `RDIB`, which are one company.
    from src.company.lookup import canonical_ticker

    ticker = canonical_ticker(ticker)

    with session_scope() as session:
        from sqlalchemy import select

        from src.storage.models import UniverseSnapshot

        # Get company name from universe
        company_name = None
        former_name = None
        former_name_until = None
        univ = session.execute(
            select(UniverseSnapshot)
            .where(UniverseSnapshot.ticker == ticker.upper())
            .where(UniverseSnapshot.as_of_date <= as_of)
            .order_by(UniverseSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if univ is None:
            # A past `as_of` predates every universe snapshot; the name the
            # company files under now is better than no name at all.
            univ = session.execute(
                select(UniverseSnapshot)
                .where(UniverseSnapshot.ticker == ticker.upper())
                .order_by(UniverseSnapshot.as_of_date.asc())
                .limit(1)
            ).scalar_one_or_none()
        if univ:
            # The column is `name`. Reading `company_name` here raised
            # AttributeError for any ticker actually present in the universe
            # table -- invisible in tests only because that table was empty.
            company_name = univ.name
            former_name = univ.former_name
            former_name_until = univ.former_name_until

        # Get fundamentals for this ticker, most recent period first
        from sqlalchemy import and_

        from src.storage.models import Fundamental

        fundamentals = session.execute(
            select(Fundamental)
            .where(
                and_(
                    Fundamental.ticker == ticker.upper(),
                    Fundamental.period_end <= as_of,
                    Fundamental.filing_date <= as_of,
                )
            )
            .order_by(Fundamental.period_end.desc(), Fundamental.filing_date.desc())
        ).scalars().all()

        if not fundamentals:
            log.info("no_fundamentals", ticker=ticker)
            return None

        # One quarter, one period end: SEC's bulk datasets round the date to a month
        # end, the frames carry the real one, and a quarter loaded by both routes is
        # stored twice -- in one filing, or across filings (a 10-K on the frames
        # route, the next 10-Q quoting the same year end as a bulk-route comparative).
        # The real date wins (see src/company/periods.py). `cross_filing` is the same
        # rule `history` and `restatements` use, so the headline, the history list
        # and the restatement feed always name the same period. Rows are never
        # mutated -- they are attached to the session -- only re-keyed.
        aliases = period_aliases(
            ((f.period_end, f.filing_date) for f in fundamentals), cross_filing=True
        )

        def canon(d: dt.date) -> dt.date:
            return aliases.get(d, d)

        # Group by period to find the most recent complete period
        by_period: dict[dt.date, list[Fundamental]] = {}
        for f in fundamentals:
            by_period.setdefault(canon(f.period_end), []).append(f)

        if not by_period:
            return None

        # Pinned period when the caller named one, most recent otherwise.
        if period_end is not None:
            period_end = canon(period_end)
            if period_end not in by_period:
                log.info(
                    "no_fundamentals_for_period",
                    ticker=ticker,
                    period_end=str(period_end),
                )
                return None
            selected_period = period_end
        else:
            selected_period = max(by_period.keys())
        period_data = _resolve_restatements(by_period[selected_period])

        # The date each figure is shown as filed is the filing that FIRST reported
        # its value (`_first_reported`), and the sheet's own date is the filing
        # that supplied TOTAL ASSETS: the balance sheet itself. It used to be the
        # newest filing behind any line, which a later filing's repeat of an
        # equity or cash figure dragged forward by up to a year. The balance
        # sheet's date is when the balance sheet was filed; if total assets are
        # missing the newest line date stands in, as before.
        first_filed = _first_reported(by_period[selected_period], period_data)
        period_end = selected_period
        filing_date = first_filed.get("total_assets") or max(first_filed.values())

        # Extract requested concepts
        assets = {}
        liabilities = {}
        equity = {}
        missing = []
        quality_issues = []

        # Asset concepts
        asset_concepts = [
            "total_assets", "current_assets", "cash", "receivables",
            "inventory", "property_plant_equipment", "goodwill", "intangibles",
            "loans", "trading_securities", "investment_securities",
            "interbank_deposits",
        ]
        liability_concepts = [
            "total_liabilities", "liabilities_and_equity", "current_liabilities",
            "long_term_debt", "accounts_payable", "deposits",
            "short_term_borrowings",
        ]
        equity_concepts = [
            "shareholders_equity", "total_equity_incl_nci", "minority_interest",
            "temporary_equity", "redeemable_preferred_stock",
            "redeemable_noncontrolling_interest",
            "minority_interest_operating_partnership",
        ]

        for concept in asset_concepts:
            metric = BALANCE_SHEET_CONCEPTS.get(concept)
            if not metric or metric not in period_data:
                assets[concept] = BalanceSheetValue(
                    concept=concept, value=None, period_end=None, filing_date=None, missing=True
                )
                missing.append(concept)
            else:
                f = period_data[metric]
                assets[concept] = BalanceSheetValue(
                    concept=concept,
                    value=f.value,
                    period_end=canon(f.period_end),
                    filing_date=first_filed[metric],
                    restated=f.restated,
                )

        for concept in liability_concepts:
            metric = BALANCE_SHEET_CONCEPTS.get(concept)
            if not metric or metric not in period_data:
                liabilities[concept] = BalanceSheetValue(
                    concept=concept, value=None, period_end=None, filing_date=None, missing=True
                )
                missing.append(concept)
            else:
                f = period_data[metric]
                liabilities[concept] = BalanceSheetValue(
                    concept=concept,
                    value=f.value,
                    period_end=canon(f.period_end),
                    filing_date=first_filed[metric],
                    restated=f.restated,
                )

        for concept in equity_concepts:
            metric = BALANCE_SHEET_CONCEPTS.get(concept)
            if not metric or metric not in period_data:
                equity[concept] = BalanceSheetValue(
                    concept=concept, value=None, period_end=None, filing_date=None, missing=True
                )
                missing.append(concept)
            else:
                f = period_data[metric]
                equity[concept] = BalanceSheetValue(
                    concept=concept,
                    value=f.value,
                    period_end=canon(f.period_end),
                    filing_date=first_filed[metric],
                    restated=f.restated,
                )

        # Validate data quality
        total_assets = assets.get("total_assets")
        shareholders_equity = equity.get("shareholders_equity")

        if total_assets and total_assets.value is not None:
            if total_assets.value == 0:
                quality_issues.append("total_assets is zero (likely segment or quarterly change, not balance)")
            elif total_assets.value < 0:
                quality_issues.append(f"total_assets is negative (${total_assets.value:,.0f})")

        if shareholders_equity and shareholders_equity.value is not None:
            if shareholders_equity.value < 0:
                quality_issues.append(f"shareholders_equity is negative (${shareholders_equity.value:,.0f}) — may be quarterly change or sign error")

        return BalanceSheet(
            ticker=ticker.upper(),
            company_name=company_name,
            former_name=former_name,
            former_name_until=former_name_until,
            period_end=period_end,
            filing_date=filing_date,
            assets=assets,
            liabilities=liabilities,
            equity=equity,
            missing_concepts=missing,
            data_quality_issues=quality_issues,
        )
