#!/usr/bin/env python3
"""Seed ILLUSTRATIVE balance sheets so the drawing can be checked locally.

READ THIS BEFORE USING IT.

The component figures below are NOT filed data. They are hand-written to the
right order of magnitude and the right shape for each business, so that the
visualisation can be eyeballed on a machine with no production database. Only
the JPM and MSFT totals are real (dump-confirmed); everything else, including
every component breakdown, is invented for rendering purposes.

Numbers invented for a picture must never be mistaken for numbers read off a
filing -- that confusion is the entire class of bug this project spent its
history fixing. So:

  * it refuses to run against anything but a local SQLite file,
  * it refuses to run against a database that already has fundamentals,
  * every row it writes is stamped source="demo", never "sec", so it is
    trivially separable from real data and the PIT accessor can exclude it.

    python3 scripts/seed_demo_fundamentals.py --db /tmp/demo.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PERIOD = dt.date(2025, 12, 31)
FILED = dt.date(2026, 2, 13)

# Annual (FY) income-statement figures, so views 2 and 3 have a twelve-month
# basis without needing four seeded quarters. Illustrative, like everything else
# in this file.
INCOME: dict[str, dict[str, float]] = {
    "JPM": {"revenue": 177_600_000_000, "cogs": 0,
            "operating_income": 76_700_000_000, "income_tax": 16_400_000_000,
            "net_income": 58_500_000_000},
    "MSFT": {"revenue": 281_700_000_000, "cogs": 89_400_000_000,
             "gross_profit": 192_300_000_000, "operating_income": 128_500_000_000,
             "income_tax": 22_100_000_000, "net_income": 101_800_000_000},
    "WMT": {"revenue": 680_900_000_000, "cogs": 511_300_000_000,
            "gross_profit": 169_600_000_000, "operating_income": 29_300_000_000,
            "income_tax": 6_200_000_000, "net_income": 19_400_000_000},
    "AAL": {"revenue": 54_200_000_000, "cogs": 0,
            "operating_income": 2_600_000_000, "income_tax": 250_000_000,
            "net_income": 850_000_000},
    "FCX": {"revenue": 25_500_000_000, "cogs": 18_100_000_000,
            "gross_profit": 7_400_000_000, "operating_income": 5_600_000_000,
            "income_tax": 1_700_000_000, "net_income": 1_900_000_000},
}

# metric -> value. Shapes chosen so the five archetypes are visibly different:
# a bank is almost all financial assets with a sliver of equity; an airline is
# planes and debt with negative equity; software is cash and goodwill, mostly
# owner-funded; a retailer is inventory and stores; a miner is almost entirely
# property and equipment.
DEMO: dict[str, dict[str, float]] = {
    # Totals dump-confirmed. Components illustrative.
    "JPM": {
        "total_assets": 4_424_900_000_000,
        # Bank line items -- what JPM actually files.
        "cash": 23_000_000_000,
        "interbank_deposits": 690_000_000_000,
        "trading_securities": 640_000_000_000,
        "investment_securities": 680_000_000_000,
        "loans": 1_400_000_000_000,
        "goodwill": 53_000_000_000,
        "total_liabilities": 4_062_462_000_000,
        "deposits": 2_600_000_000_000,
        "short_term_borrowings": 310_000_000_000,
        "long_term_debt": 430_000_000_000,
        "total_equity": 362_438_000_000,
    },
    # Totals dump-confirmed. Components illustrative.
    "MSFT": {
        "total_assets": 665_302_000_000,
        "cash": 78_400_000_000,
        "receivables": 56_900_000_000,
        "inventory": 1_800_000_000,
        "property_plant_equipment": 210_500_000_000,
        "goodwill": 119_200_000_000,
        "intangibles": 25_600_000_000,
        "total_liabilities": 274_427_000_000,
        "accounts_payable": 24_100_000_000,
        "long_term_debt": 41_300_000_000,
        "total_equity": 390_875_000_000,
    },
    # Everything below is illustrative, including the totals.
    "WMT": {
        "total_assets": 260_800_000_000,
        "cash": 9_000_000_000,
        "receivables": 9_700_000_000,
        "inventory": 56_400_000_000,
        "property_plant_equipment": 118_600_000_000,
        "goodwill": 28_900_000_000,
        "liabilities_and_equity": 260_800_000_000,
        "accounts_payable": 58_700_000_000,
        "long_term_debt": 36_100_000_000,
        "total_equity": 91_200_000_000,
    },
    "AAL": {
        "total_assets": 62_600_000_000,
        "cash": 8_100_000_000,
        "receivables": 2_300_000_000,
        "inventory": 2_100_000_000,
        "property_plant_equipment": 39_400_000_000,
        "goodwill": 4_100_000_000,
        "total_liabilities": 66_500_000_000,
        "accounts_payable": 2_800_000_000,
        "long_term_debt": 31_600_000_000,
        "total_equity": -3_900_000_000,
    },
    "FCX": {
        "total_assets": 58_200_000_000,
        "cash": 5_600_000_000,
        "receivables": 2_400_000_000,
        "inventory": 6_100_000_000,
        "property_plant_equipment": 36_800_000_000,
        "total_liabilities": 30_400_000_000,
        "accounts_payable": 3_100_000_000,
        "long_term_debt": 11_200_000_000,
        "total_equity": 27_800_000_000,
    },
}

SECTORS = {
    "JPM": "Financial Services", "MSFT": "Technology", "WMT": "Consumer Defensive",
    "AAL": "Industrials", "FCX": "Basic Materials",
}
NAMES = {
    "JPM": "JPMorgan Chase & Co.", "MSFT": "Microsoft Corporation",
    "WMT": "Walmart Inc.", "AAL": "American Airlines Group Inc.",
    "FCX": "Freeport-McMoRan Inc.",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="path to a local SQLite file")
    args = ap.parse_args()

    if "://" in args.db:
        sys.exit("ERROR: pass a plain file path, not a database URL.")
    url = f"sqlite:///{Path(args.db).resolve()}"
    os.environ["DATABASE_URL"] = url

    from sqlalchemy import func, select

    from src.storage.db import init_db, session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    init_db()
    with session_scope() as s:
        existing = s.execute(select(func.count()).select_from(Fundamental)).scalar_one()
    if existing:
        sys.exit(
            f"ERROR: {args.db} already holds {existing:,} fundamentals rows. "
            "This writes invented figures and must never mix with real data."
        )

    rows = 0
    with session_scope() as s:
        for ticker, metrics in DEMO.items():
            merged = {**metrics, **{k: v for k, v in INCOME.get(ticker, {}).items() if v}}
            for metric, value in merged.items():
                s.add(Fundamental(
                    ticker=ticker, metric=metric, value=float(value),
                    period_end=PERIOD, fiscal_period="FY", filing_date=FILED,
                    # NOT "sec". Invented figures stay separable from filed ones.
                    source="demo", restated=False,
                ))
                rows += 1
            s.add(SectorMap(ticker=ticker, sic="0000", sector=SECTORS[ticker],
                            sector_source="demo"))
            s.add(UniverseSnapshot(
                ticker=ticker, as_of_date=PERIOD, name=NAMES[ticker],
                sector=SECTORS[ticker], sector_source="demo",
            ))

    print(f"Seeded {rows} ILLUSTRATIVE rows for {', '.join(DEMO)} into {args.db}")
    print("source='demo' on every row. These are not filed figures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
