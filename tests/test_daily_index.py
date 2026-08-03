#!/usr/bin/env python3
"""Test script: fetch SEC daily index for last 3 trading days and generate report."""

import asyncio
import datetime as dt
from collections import defaultdict

from src.config.settings import get_settings
from src.ingest.sec_daily_index import (
    make_client,
    extract_daily_index_filings,
    fetch_daily_index,
)
from src.ingest.sec_edgar import TRACKED_FORMS, fetch_company_tickers, parse_company_tickers
from src.storage.pit import get_universe
from src.storage.db import session_scope, init_db


async def get_last_trading_days(n: int = 3) -> list[dt.date]:
    """Get the last N trading days (skip weekends)."""
    today = dt.date.today()
    days = []
    d = today
    while len(days) < n:
        if d.weekday() < 5:  # Monday = 0, Friday = 4
            days.append(d)
        d = d - dt.timedelta(days=1)
    return sorted(days)


async def main():
    init_db()
    s = get_settings()

    print("=" * 80)
    print("SEC DAILY INDEX REPORT")
    print("=" * 80)
    print()

    # Get ticker -> CIK mapping
    print("Fetching company tickers...")
    tickers = await fetch_company_tickers()
    ticker_cik_map = {t["ticker"]: t["cik"] for t in tickers if t["cik"]}
    print(f"  Loaded {len(ticker_cik_map)} tickers with CIKs")
    print()

    # Get liquid universe
    with session_scope() as session:
        as_of = dt.date.today()
        universe = get_universe(session, as_of)
        if universe.empty:
            print("ERROR: Universe is empty. Run backfill first.")
            return
        liquid_tickers = set(universe["ticker"].tolist())
        print(f"Liquid universe size: {len(liquid_tickers)} tickers")
        print()

    # Fetch daily index for last 3 trading days
    client = make_client()
    async with client:
        trading_days = await get_last_trading_days(3)
        print(f"Last 3 trading days: {trading_days}")
        print()

        all_filings = []
        daily_counts = {}

        for date in trading_days:
            print(f"Fetching daily index for {date}...")
            filings = await extract_daily_index_filings(
                client, date, ticker_cik_map, TRACKED_FORMS
            )
            all_filings.extend(filings)
            daily_counts[str(date)] = len(filings)
            print(f"  {len(filings)} filings from tracked forms")

        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)
        print()

        # Count filings per day
        print("Filings by day:")
        for date in trading_days:
            count = daily_counts.get(str(date), 0)
            print(f"  {date}: {count} filings")
        print()

        # Count universe intersections
        universe_filings = [f for f in all_filings if f["ticker"] in liquid_tickers]
        print(f"Total filings across 3 days: {len(all_filings)}")
        print(f"Filings in liquid universe: {len(universe_filings)}")
        if all_filings:
            pct = 100.0 * len(universe_filings) / len(all_filings)
            print(f"  ({pct:.1f}% of total)")
        print()

        # Form type distribution
        form_counts = defaultdict(int)
        for f in universe_filings:
            form_counts[f["form"]] += 1
        print("Form types in universe filings:")
        for form in sorted(form_counts.keys()):
            print(f"  {form}: {form_counts[form]}")
        print()

        # Sample 10
        print("Sample 10 filings (universe only):")
        print()
        for i, f in enumerate(universe_filings[:10], 1):
            print(f"  {i}. {f['ticker']:5s} {f['form']:10s} {f['filing_date']} {f['company_name'][:50]}")

        print()
        print("=" * 80)
        print("URL VERIFIED")
        print("=" * 80)
        print()
        print(f"Daily Index URL: https://www.sec.gov/Archives/edgar/daily-index/{{year}}/Q{{quarter}}/company.0.txt")
        print(f"Successfully fetched filings for dates: {trading_days}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
