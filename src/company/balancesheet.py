"""Balance sheet query layer for company explainer.

Given a ticker, returns the most recent balance sheet data with point-in-time
correctness, highlighting missing concepts and data quality.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import structlog

from src.storage.db import session_scope

log = structlog.get_logger(__name__)


# Map from display names to fundamentals table metric names
BALANCE_SHEET_CONCEPTS = {
    # Assets
    "cash": "cash",
    "short_term_investments": "short_term_investments",
    "receivables": "receivables",
    "inventory": "inventory",
    "property_plant_equipment": "ppe",
    "goodwill": "goodwill",
    "intangibles": "intangibles",
    "other_assets": "other_assets",
    "current_assets": "current_assets",
    "total_assets": "total_assets",
    # Liabilities
    "short_term_debt": "short_term_debt",
    "accounts_payable": "accounts_payable",
    "current_liabilities": "current_liabilities",
    "long_term_debt": "long_term_debt",
    "other_liabilities": "other_liabilities",
    "total_liabilities": "total_liabilities",
    # Equity
    "shareholders_equity": "total_equity",
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
    period_end: dt.date
    filing_date: dt.date
    assets: dict[str, BalanceSheetValue]  # concept -> value
    liabilities: dict[str, BalanceSheetValue]
    equity: dict[str, BalanceSheetValue]
    missing_concepts: list[str]


def get_balance_sheet(ticker: str, as_of: dt.date | None = None) -> BalanceSheet | None:
    """Fetch the most recent balance sheet for a ticker.

    Returns None if the ticker has no usable fundamentals data.
    Point-in-time: only shows data as of `as_of` and earlier.
    """
    if as_of is None:
        as_of = dt.date.today()

    with session_scope() as session:
        from src.storage.models import UniverseSnapshot
        from sqlalchemy import select

        # Get company name from universe
        company_name = None
        univ = session.execute(
            select(UniverseSnapshot)
            .where(UniverseSnapshot.ticker == ticker.upper())
            .where(UniverseSnapshot.as_of_date <= as_of)
            .order_by(UniverseSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        if univ:
            company_name = univ.company_name

        # Get fundamentals for this ticker, most recent period first
        from src.storage.models import Fundamental
        from sqlalchemy import and_

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

        # Use most recent period
        most_recent_period = max(by_period.keys())
        period_data = {f.metric: f for f in by_period[most_recent_period]}

        # Get period end and filing date
        sample = period_data[next(iter(period_data))]
        period_end = sample.period_end
        filing_date = sample.filing_date

        # Extract requested concepts
        assets = {}
        liabilities = {}
        equity = {}
        missing = []

        asset_concepts = [
            "cash", "short_term_investments", "receivables", "inventory",
            "property_plant_equipment", "goodwill", "intangibles", "other_assets",
        ]
        liability_concepts = [
            "short_term_debt", "accounts_payable", "long_term_debt", "other_liabilities",
        ]
        equity_concepts = ["shareholders_equity"]

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

        return BalanceSheet(
            ticker=ticker.upper(),
            company_name=company_name,
            period_end=period_end,
            filing_date=filing_date,
            assets=assets,
            liabilities=liabilities,
            equity=equity,
            missing_concepts=missing,
        )
