"""Seed the throwaway e2e database with two companies for the smoke suite.

    python e2e/seed_smoke.py      (DATABASE_URL must point at e2e/.data/e2e.db)

Run by global-setup.ts after the migration. Writes ONLY to the database named in
DATABASE_URL, which server-env.ts sets to the disposable SQLite file; it refuses
to run against anything else. Idempotent: existing rows for these tickers are
replaced. Figures are round test values, not real filings.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

url = os.environ.get("DATABASE_URL", "")
if not url.startswith("sqlite") or "e2e" not in url:
    sys.exit(f"seed_smoke: refusing to seed {url!r}; expected the e2e SQLite file")

from src.storage.db import init_db, session_scope  # noqa: E402
from src.storage.models import Fundamental, JobState, SectorMap, UniverseSnapshot  # noqa: E402

COMPANIES = {
    # ticker: (name, sector, periods: [(period_end, filing_date, assets, liabilities, equity)])
    "MSFT": ("MICROSOFT CORP", "Information Technology", [
        (dt.date(2026, 6, 30), dt.date(2026, 7, 29), 758e9, 316e9, 442e9),
        (dt.date(2026, 3, 31), dt.date(2026, 4, 29), 694e9, 280e9, 414e9),
        (dt.date(2025, 12, 31), dt.date(2026, 1, 28), 665e9, 274e9, 391e9),
    ]),
    "JPM": ("JPMORGAN CHASE & CO", "Financials", [
        (dt.date(2026, 6, 30), dt.date(2026, 8, 6), 5_015e9, 4_640e9, 375e9),
        (dt.date(2026, 3, 31), dt.date(2026, 5, 2), 4_900e9, 4_540e9, 360e9),
    ]),
}
# Four more per sector: a sector hub exists only from five companies
# (sector_page.MIN_COMPANIES), and the company page links to its hub.
for i, sector in enumerate(["Information Technology"] * 4 + ["Financials"] * 4, start=1):
    COMPANIES[f"FIL{i}"] = (f"FILLER {i} INC", sector, [
        (dt.date(2026, 6, 30), dt.date(2026, 8, 1), 10e9 * i, 6e9 * i, 4e9 * i),
    ])


def main() -> None:
    init_db()
    with session_scope() as s:
        for ticker, (name, sector, periods) in COMPANIES.items():
            s.query(Fundamental).filter(Fundamental.ticker == ticker).delete()
            s.query(SectorMap).filter(SectorMap.ticker == ticker).delete()
            s.query(UniverseSnapshot).filter(UniverseSnapshot.ticker == ticker).delete()
            s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name=name, sector=sector))
            s.add(SectorMap(ticker=ticker, cik="0", sector=sector, sector_source="sic"))
            for pe, fd, a, liab, eq in periods:
                for metric, value in (
                    ("total_assets", a), ("total_liabilities", liab), ("total_equity", eq),
                    ("cash", a * 0.1), ("property_plant_equipment", a * 0.4),
                    ("long_term_debt", liab * 0.5),
                ):
                    s.add(Fundamental(
                        ticker=ticker, metric=metric, value=float(value), period_end=pe,
                        fiscal_period="FY", filing_date=fd, source="sec", restated=False,
                    ))
        # A scheduler success two days ago, so the home strip shows its seventh
        # cell ("Updated · 2d ago") exactly as production does.
        s.query(JobState).filter(JobState.name == "fundamentals").delete()
        s.add(JobState(name="fundamentals",
                       last_success_at=dt.datetime.utcnow() - dt.timedelta(days=2),
                       last_rows=0, consecutive_failures=0))
    print("seed_smoke: seeded", ", ".join(COMPANIES), "+ a scheduler success")


if __name__ == "__main__":
    main()
