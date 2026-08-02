"""Historical backfill.

Stage 1 needs 400 trading days of bars before it can compute anything, so a
fresh deploy must backfill before the first real run. Polygon grouped-daily
gives one day of the entire market per call, which makes this cheap:
~500 calls for two years of history.

    python -m src.backfill --days 600
    python -m src.backfill --days 600 --fundamentals

Fundamentals are backfilled from SEC XBRL (as-reported, with true filing dates)
rather than from a vendor, because that is the only source that supports an
honest point-in-time backtest.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest import polygon, sec_edgar, yahoo
from src.ingest.base import gather_bounded
from src.logging_config import configure_logging
from src.storage import repository
from src.storage.db import init_db, session_scope
from src.storage.pit import get_universe

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Live, source-agnostic backfill diagnostics.
#
# The website's first-run loader polls /status; when a bar load stalls it needs
# to say *why* instead of spinning forever. This module-level state is updated
# as the backfill runs (Polygon grouped-daily or the keyless Yahoo fallback) and
# surfaced verbatim under /status `backfill`. `last_progress_at` moving proves the
# loop is alive; `last_error` explains a stall; `phase == "error"` with rows == 0
# tells the UI to stop waiting.
# ---------------------------------------------------------------------------
_BACKFILL_STATE: dict[str, Any] = {
    "phase": "idle",             # idle | running | done | error
    "source": None,              # "polygon" | "yahoo"
    "unit": "sessions",          # what units_done/units_total count
    "units_done": 0,
    "units_total": 0,
    "rows": 0,
    "target_sessions": 0,        # `days` requested
    "last_error": None,
    "polygon_key_present": None,
    "yfinance_available": None,
    "last_progress_at": None,    # ISO ts; moves every iteration -> "still alive"
    "started_at": None,
}


def get_backfill_state() -> dict[str, Any]:
    """A copy of the live backfill diagnostics, for /status."""
    return dict(_BACKFILL_STATE)


def _update_state(**kw: Any) -> None:
    _BACKFILL_STATE.update(kw)
    _BACKFILL_STATE["last_progress_at"] = dt.datetime.now(dt.UTC).isoformat()


def record_backfill_error(msg: str) -> None:
    """Mark the backfill failed (called by the API's background wrapper too)."""
    _update_state(phase="error", last_error=msg[:300])


async def backfill_bars(days: int, end: dt.date | None = None) -> int:
    """Load `days` sessions of history. Uses Polygon if a key is set, else the
    free Yahoo path (no key)."""
    end = end or dt.date.today()
    key = bool(get_settings().polygon_api_key)
    _update_state(
        phase="running", rows=0, units_done=0, last_error=None,
        target_sessions=days, polygon_key_present=key,
        yfinance_available=None, started_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    if key:
        return await _backfill_bars_polygon(days, end)
    return await _backfill_bars_free(days, end)


async def _backfill_bars_polygon(days: int, end: dt.date) -> int:
    """One grouped-daily call per session. Holidays return empty and are skipped.

    Each day's call is bounded by a hard timeout so a single hung request can
    never freeze the whole backfill (and hold `_backfill_lock`). Progress is
    published to /status after every session so the loader visibly climbs -- on
    the free Polygon tier (5 calls/min) this is slow but must never stall
    silently.
    """
    timeout = get_settings().polygon_fetch_timeout
    _update_state(source="polygon", unit="sessions", units_total=days)
    total = 0
    day = end
    fetched_sessions = 0
    calendar_days = 0

    while fetched_sessions < days and calendar_days < days * 2:
        calendar_days += 1
        if day.weekday() < 5:
            try:
                rows = await asyncio.wait_for(
                    polygon.fetch_grouped_daily(day), timeout=timeout
                )
            except TimeoutError:
                log.warning("backfill_day_timeout", date=str(day), timeout=timeout)
                _update_state(last_error=f"grouped-daily {day} timed out after {timeout:.0f}s")
                rows = []
            except Exception as exc:  # noqa: BLE001 - one bad day is survivable
                log.warning("backfill_day_failed", date=str(day), error=str(exc)[:200])
                _update_state(last_error=f"{day}: {str(exc)[:200]}")
                rows = []
            if rows:
                with session_scope() as session:
                    repository.save_bars(session, rows)
                total += len(rows)
                fetched_sessions += 1
                if fetched_sessions % 25 == 0:
                    log.info(
                        "backfill_progress", sessions=fetched_sessions,
                        rows=total, at=str(day),
                    )
            # Publish progress every iteration (even skipped days) so the loader
            # can see last_progress_at advancing and knows the loop is alive.
            _update_state(units_done=fetched_sessions, rows=total)
        day -= dt.timedelta(days=1)

    if fetched_sessions == 0:
        _update_state(
            phase="error",
            last_error=_BACKFILL_STATE["last_error"] or "Polygon returned no bars",
        )
    else:
        _update_state(phase="done")
    log.info("backfill_bars_complete", sessions=fetched_sessions, rows=total)
    return total


async def _backfill_bars_free(days: int, end: dt.date) -> int:
    """Keyless history load: SEC universe seed, Yahoo bars, batched.

    `days` is trading sessions; Yahoo returns a calendar window, so we ask for
    ~1.5x calendar days to cover it. Bars are saved chunk-by-chunk so progress
    (and /status) climbs steadily and a mid-run failure keeps what it loaded.
    """
    start = end - dt.timedelta(days=int(days * 1.5) + 10)
    yf_ok = yahoo.yfinance_available()
    reference = await sec_edgar.fetch_company_tickers()
    tickers = [r["ticker"] for r in reference]
    cap = get_settings().free_universe_max
    if cap and cap > 0:
        tickers = tickers[:cap]
    chunk = 200
    chunks_total = (len(tickers) + chunk - 1) // chunk
    _update_state(
        source="yahoo", unit="chunks", units_total=chunks_total, units_done=0,
        yfinance_available=yf_ok,
    )
    log.info("backfill_free_start", companies=len(tickers), start=str(start), end=str(end))

    total = 0
    for idx, i in enumerate(range(0, len(tickers), chunk)):
        batch = tickers[i : i + chunk]
        rows = await yahoo.fetch_daily_bars_batch(batch, start, end, chunk=chunk)
        if rows:
            with session_scope() as session:
                repository.save_bars(session, rows)
            total += len(rows)
        _update_state(units_done=idx + 1, rows=total, last_error=yahoo.last_error())
        log.info(
            "backfill_free_progress", done=min(i + chunk, len(tickers)),
            total=len(tickers), rows=total,
        )

    if total == 0:
        if not yf_ok:
            err = "yfinance is not installed on the server (keyless price source)"
        else:
            err = yahoo.last_error() or "Yahoo returned no bars for any chunk"
        _update_state(phase="error", last_error=err)
    else:
        _update_state(phase="done")
    log.info("backfill_bars_complete", source="yahoo", rows=total)
    return total


async def backfill_fundamentals(
    as_of: dt.date | None = None, limit: int | None = None, concurrency: int = 8
) -> int:
    """SEC XBRL as-reported facts for the current universe.

    Slow -- roughly 17 concept calls per company at 8 req/sec. For 6000 names
    budget a couple of hours. Run it once, then the daily pipeline only needs
    the incremental filings.
    """
    as_of = as_of or dt.date.today()
    with session_scope() as session:
        universe = get_universe(session, as_of)
    if universe.empty:
        log.error("backfill_fundamentals_no_universe", as_of=str(as_of))
        return 0

    rows_with_cik = universe[universe["cik"].notna()]
    if limit:
        rows_with_cik = rows_with_cik.head(limit)
    log.info("backfill_fundamentals_start", companies=len(rows_with_cik))

    client = sec_edgar.make_client(concurrency=concurrency)
    total = 0

    async def one(ticker: str, cik: str) -> int:
        rows = await sec_edgar.fetch_pit_fundamentals(client, ticker, cik)
        if not rows:
            return 0
        with session_scope() as session:
            repository.save_fundamentals(session, rows)
        return len(rows)

    async with client:
        results = await gather_bounded(
            [
                one(str(r["ticker"]), str(r["cik"]))
                for _, r in rows_with_cik.iterrows()
            ],
            concurrency,
        )
    for r in results:
        if isinstance(r, int):
            total += r

    log.info("backfill_fundamentals_complete", rows=total)
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical backfill")
    parser.add_argument(
        "--days", type=int, default=600,
        help="trading sessions of price history to fetch (default 600)",
    )
    parser.add_argument(
        "--fundamentals", action="store_true",
        help="also backfill SEC XBRL as-reported fundamentals",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of companies for the fundamentals pass",
    )
    parser.add_argument("--skip-bars", action="store_true")
    args = parser.parse_args()

    configure_logging()
    init_db()

    async def run() -> None:
        if not args.skip_bars:
            await backfill_bars(args.days)
        if args.fundamentals:
            await backfill_fundamentals(limit=args.limit)

    asyncio.run(run())


if __name__ == "__main__":
    main()
