"""Stage 1 — Event-driven filtering.

"Did the company file something interesting today/this week? Focus the funnel
on companies with recent filings that might have moved markets."

Replaces the trend-based momentum gate. Uses SEC daily index to surface
companies with recent material filings, intersected with the liquid universe.

Target: 1200 -> ~400 candidates (enough to make the funnel interesting, few
enough that Stage 2 factors can run efficiently).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import pandas as pd
import structlog

from src.ingest.sec_edgar import TRACKED_FORMS, fetch_company_tickers, fetch_submissions, extract_filing_events
from src.ingest.sec_daily_index import make_client, extract_daily_index_filings
from src.ingest.base import APIClient

log = structlog.get_logger(__name__)


@dataclass
class EventGateResult:
    """Result of Stage 1 event filtering."""
    survivors: pd.Index  # Tickers that passed the event gate
    regime: str  # NORMAL | ALERT (for reporting)
    reject_counts: dict[str, int]  # Why names were rejected


async def run_event_gate(
    session: Any,
    tickers: list[str],
    universe: pd.DataFrame,
    as_of: dt.date,
) -> EventGateResult:
    """Filter universe to companies with recent SEC filings.

    Fetches the SEC daily index for the last N trading days, intersects with
    the liquid universe, and returns tickers with filing activity. This replaces
    the trend-based gate as the entry point to the funnel.

    Args:
        session: DB session (unused for now, kept for API compatibility)
        tickers: List of tickers in the universe
        universe: DataFrame with ticker index and sector
        as_of: Analysis date

    Returns:
        EventGateResult with survivors (tickers that have recent filings)
    """
    liquid_set = set(universe["ticker"].tolist())
    ticker_cik_map = {row["ticker"]: row.get("cik") for _, row in universe.iterrows()}

    # Fetch daily indices for the last 3 trading days
    client = make_client()
    filings_by_ticker: dict[str, list[dict[str, Any]]] = {}

    async with client:
        # Get last 3 trading days
        trading_days = _get_last_trading_days(as_of, 3)
        for date in trading_days:
            filings = await extract_daily_index_filings(
                client, date, ticker_cik_map, TRACKED_FORMS
            )
            for f in filings:
                if f["ticker"] not in filings_by_ticker:
                    filings_by_ticker[f["ticker"]] = []
                filings_by_ticker[f["ticker"]].append(f)

    # Count filings per ticker (event signal)
    filing_counts = {t: len(f) for t, f in filings_by_ticker.items() if t in liquid_set}

    log.info(
        "event_gate_filings",
        total_filings=sum(filing_counts.values()),
        tickers_with_filings=len(filing_counts),
        universe_size=len(liquid_set),
    )

    # Survivors: anyone with at least one filing in the lookback window
    survivors = pd.Index([t for t in liquid_set if filing_counts.get(t, 0) > 0])

    # Diagnostics
    reject_counts = {
        "no_recent_filings": len(liquid_set) - len(survivors),
    }

    regime = "NORMAL"
    if len(survivors) < 200:
        regime = "ALERT"
        log.warning("event_gate_low_filings", survivors=len(survivors))

    return EventGateResult(
        survivors=survivors,
        regime=regime,
        reject_counts=reject_counts,
    )


def _get_last_trading_days(as_of: dt.date, n: int = 3) -> list[dt.date]:
    """Get the last N trading days (skip weekends)."""
    days = []
    d = as_of
    while len(days) < n:
        if d.weekday() < 5:  # Monday = 0, Friday = 4
            days.append(d)
        d = d - dt.timedelta(days=1)
    return sorted(days)
