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
    # Liabilities
    "total_liabilities": "total_liabilities",
    "current_liabilities": "current_liabilities",
    "long_term_debt": "long_term_debt",
    "accounts_payable": "accounts_payable",
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
    data_quality_issues: list[str]  # e.g., negative equity, zero assets


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

        # Get period end and filing date from any concept in the period
        sample = period_data[next(iter(period_data))]
        period_end = sample.period_end
        filing_date = sample.filing_date

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
        ]
        liability_concepts = [
            "total_liabilities", "current_liabilities",
            "long_term_debt", "accounts_payable",
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
            period_end=period_end,
            filing_date=filing_date,
            assets=assets,
            liabilities=liabilities,
            equity=equity,
            missing_concepts=missing,
            data_quality_issues=quality_issues,
        )
