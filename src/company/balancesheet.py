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


def _recency(f: Any) -> tuple[dt.date, int]:
    """Sort key for "which filing of this metric is the one to show".

    Later filing wins; on a tie, the more-preferred source wins. Returned as a
    tuple so `max()` orders on both without a second pass.
    """
    rank = {s: i for i, s in enumerate(_SOURCE_PREFERENCE)}
    return (f.filing_date, -rank.get(f.source, 99))


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


def get_balance_sheet(
    ticker: str,
    as_of: dt.date | None = None,
    period_end: dt.date | None = None,
) -> BalanceSheet | None:
    """Fetch a balance sheet for a ticker.

    Returns None if the ticker has no usable fundamentals data.
    Point-in-time: only shows data as of `as_of` and earlier.

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
                )
            )
            .order_by(Fundamental.period_end.desc(), Fundamental.filing_date.desc())
        ).scalars().all()

        if not fundamentals:
            log.info("no_fundamentals", ticker=ticker)
            return None

        # Group by period to find the most recent complete period
        by_period: dict[dt.date, list[Fundamental]] = {}
        for f in fundamentals:
            if f.period_end not in by_period:
                by_period[f.period_end] = []
            by_period[f.period_end].append(f)

        if not by_period:
            return None

        # Pinned period when the caller named one, most recent otherwise.
        if period_end is not None:
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

        # The period's own filing date is the NEWEST filing behind any of the
        # figures shown, not whichever metric happened to be first in a dict.
        # It is the date the header prints and the API returns, so it has to
        # describe the numbers actually on the page.
        newest = max(period_data.values(), key=_recency)
        period_end = newest.period_end
        filing_date = newest.filing_date

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
                    period_end=f.period_end,
                    filing_date=f.filing_date,
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
                    period_end=f.period_end,
                    filing_date=f.filing_date,
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
                    period_end=f.period_end,
                    filing_date=f.filing_date,
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
