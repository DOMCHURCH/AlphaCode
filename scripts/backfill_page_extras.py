"""Build the pre-rendered company-page sections, for one ticker or all of them.

    python scripts/backfill_page_extras.py                  # every ticker
    python scripts/backfill_page_extras.py --ticker JPM     # just one
    python scripts/backfill_page_extras.py --limit 50       # a sample

Everything it writes is derived: the intro, the peers, the filings list and the
JSON-LD are all recoverable from `fundamentals`, `filing_events` and
`sector_map`. So this is safe to re-run at any time, and a row it fails to
build costs a section of one page rather than the page.

WHEN TO RUN IT. After an ingest that moved fundamentals or filings. A row
carries `source_period_end`, so a page built before the newest filing is
identifiable rather than merely suspected -- `--stale` rebuilds exactly those.
"""

from __future__ import annotations

import argparse
import sys
import time


def _tickers(limit: int | None, stale_only: bool) -> list[str]:
    """Every ticker with fundamentals, newest period first."""
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import CompanyPageExtras, Fundamental

    with session_scope() as session:
        rows = session.execute(
            select(Fundamental.ticker, func.max(Fundamental.period_end))
            .group_by(Fundamental.ticker)
            .order_by(func.max(Fundamental.period_end).desc())
        ).all()
        if stale_only:
            # A row is stale when the balance sheet it was written from is no
            # longer the newest one. Missing rows count as stale.
            built = dict(
                session.execute(
                    select(
                        CompanyPageExtras.ticker,
                        CompanyPageExtras.source_period_end,
                    )
                ).all()
            )
            rows = [r for r in rows if built.get(r[0]) != r[1]]
    out = [t for t, _ in rows]
    return out[:limit] if limit else out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", help="rebuild one ticker and stop")
    parser.add_argument("--limit", type=int, help="stop after N tickers")
    parser.add_argument(
        "--stale",
        action="store_true",
        help="only tickers whose row predates their newest filing",
    )
    args = parser.parse_args()

    from src.logging_config import configure_logging
    from src.report.home_page import SITE_ORIGIN
    from src.report.page_extras_store import compute_and_store

    configure_logging()

    targets = [args.ticker.upper()] if args.ticker else _tickers(args.limit, args.stale)
    if not targets:
        print("Nothing to build.")
        return 0

    print(f"Building page extras for {len(targets):,} tickers...")
    started = time.monotonic()
    built = skipped = failed = 0
    for i, ticker in enumerate(targets, 1):
        try:
            if compute_and_store(ticker, SITE_ORIGIN):
                built += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001 - one bad ticker is not a run
            failed += 1
            print(f"  {ticker}: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
        if i % 250 == 0:
            rate = i / (time.monotonic() - started)
            print(f"  {i:,}/{len(targets):,}  {rate:.0f}/s")

    elapsed = time.monotonic() - started
    print(
        f"\n{built:,} built, {skipped:,} skipped (no drawable balance sheet), "
        f"{failed:,} failed in {elapsed:.1f}s "
        f"({len(targets) / elapsed:.0f} tickers/s)."
    )
    return 1 if failed and not built else 0


if __name__ == "__main__":
    raise SystemExit(main())
