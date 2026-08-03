"""Test balance sheet query layer with 5 sample companies.

Sample selection:
- JPM: Bank (asset-heavy, liability-heavy)
- AAL: Airline (capital-heavy, debt-heavy)
- MSFT: Software (asset-light, equity-funded)
- WMT: Retailer (inventory-heavy, working capital)
- FCX: Miner (capital-heavy, cyclical)
"""

import datetime as dt
from src.company.balancesheet import get_balance_sheet, BALANCE_SHEET_CONCEPTS
from src.storage.db import session_scope
from sqlalchemy import text


def test_balancesheet_query():
    """Test the balance sheet query with sample companies."""

    # First, check what metrics are actually populated
    with session_scope() as session:
        result = session.execute(text("""
            SELECT metric, COUNT(DISTINCT ticker) as tickers, COUNT(*) as rows
            FROM fundamentals
            GROUP BY metric
            ORDER BY metric
        """)).fetchall()

        print("\n" + "="*80)
        print("FUNDAMENTALS TABLE POPULATION")
        print("="*80)

        if not result:
            print("ERROR: fundamentals table is empty. SEC backfill not run.")
            print("\nTo populate fundamentals, run the SEC quarterly loader:")
            print("  POST /backfill?fundamentals=true")
            print("\nOr in code:")
            print("  from src.backfill import backfill_pit_fundamentals")
            print("  backfill_pit_fundamentals(session)")
            return

        total_tickers = set()
        total_rows = 0
        print(f"\n{'Metric':<40} {'Tickers':>8} {'Rows':>10}")
        print("-" * 60)

        for metric, tickers, rows in sorted(result, key=lambda x: x[2], reverse=True):
            print(f"{metric:<40} {tickers:>8} {rows:>10}")
            total_tickers.add(metric)
            total_rows += rows

        print("-" * 60)
        print(f"{'TOTAL':<40} {'':>8} {total_rows:>10}")
        print(f"\nMetrics available: {len(result)}")
        print(f"Total data points: {total_rows}")

    # Sample companies for testing
    samples = {
        "JPM": "JPMorgan Chase (Bank)",
        "AAL": "American Airlines (Airline)",
        "MSFT": "Microsoft (Software)",
        "WMT": "Walmart (Retailer)",
        "FCX": "Freeport-McMoRan (Miner)",
    }

    print("\n" + "="*80)
    print("SAMPLE COMPANY BALANCE SHEETS")
    print("="*80)

    as_of = dt.date.today()
    all_missing = set()
    companies_with_data = 0

    for ticker, description in samples.items():
        print(f"\n{ticker}: {description}")
        print("-" * 60)

        bs = get_balance_sheet(ticker, as_of=as_of)

        if not bs:
            print(f"  ❌ NO DATA (no fundamentals for period ending on or before {as_of})")
            continue

        companies_with_data += 1
        print(f"  Period: {bs.period_end} (filed {bs.filing_date})")
        print(f"\n  ASSETS:")

        total_assets = 0
        for name, value in bs.assets.items():
            if value.missing:
                print(f"    {name:<35} [MISSING]")
                all_missing.add(name)
            else:
                print(f"    {name:<35} ${value.value:>15,.0f}" if value.value else f"    {name:<35} {value.value}")
                if value.value:
                    total_assets += value.value

        print(f"\n  LIABILITIES:")
        total_liabilities = 0
        for name, value in bs.liabilities.items():
            if value.missing:
                print(f"    {name:<35} [MISSING]")
                all_missing.add(name)
            else:
                print(f"    {name:<35} ${value.value:>15,.0f}" if value.value else f"    {name:<35} {value.value}")
                if value.value:
                    total_liabilities += value.value

        print(f"\n  EQUITY:")
        total_equity = 0
        for name, value in bs.equity.items():
            if value.missing:
                print(f"    {name:<35} [MISSING]")
                all_missing.add(name)
            else:
                print(f"    {name:<35} ${value.value:>15,.0f}" if value.value else f"    {name:<35} {value.value}")
                if value.value:
                    total_equity += value.value

        print(f"\n  BALANCE CHECK:")
        print(f"    Total assets:      ${total_assets:>15,.0f}")
        print(f"    Liabilities + Eq:  ${total_liabilities + total_equity:>15,.0f}")
        if total_assets > 0:
            diff = abs((total_assets - (total_liabilities + total_equity)) / total_assets * 100)
            status = "✓" if diff < 1 else "⚠️"
            print(f"    Balance ({status}):      {diff:.2f}% difference")

    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    print(f"Companies with data:              {companies_with_data}/5")
    print(f"Unique missing concepts:         {len(all_missing)}")
    if all_missing:
        print(f"  Missing: {', '.join(sorted(all_missing))}")

    # Check how many tickers have enough data to render
    with session_scope() as session:
        # A company is "renderable" if it has total_assets and shareholders_equity
        result = session.execute(text("""
            WITH assets_tickers AS (
                SELECT DISTINCT ticker FROM fundamentals
                WHERE metric = 'total_assets'
            ),
            equity_tickers AS (
                SELECT DISTINCT ticker FROM fundamentals
                WHERE metric = 'total_equity'
            )
            SELECT COUNT(DISTINCT assets_tickers.ticker)
            FROM assets_tickers
            JOIN equity_tickers ON assets_tickers.ticker = equity_tickers.ticker
        """)).scalar()

        print(f"\nTickers with both assets & equity: {result or 0} / 6,251")


if __name__ == "__main__":
    test_balancesheet_query()
