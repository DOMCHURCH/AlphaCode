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
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from src.config.settings import get_settings
from src.ingest import polygon, sec_edgar, stooq
from src.ingest import sic as sic_map
from src.ingest.base import gather_bounded
from src.logging_config import configure_logging
from src.storage import repository
from src.storage.db import init_db, session_scope
from src.storage.models import DailyBar
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

    # Skip days already in the store. A re-run/resume otherwise walks backward
    # from today and re-downloads every loaded day before it can add a new one,
    # so bar_dates sits frozen (e.g. at 202) while rate-limited calls are spent
    # re-fetching history we already have. Loading the existing date set once
    # lets us jump straight to the missing days.
    with session_scope() as session:
        existing: set[dt.date] = set(
            session.execute(select(DailyBar.date).distinct()).scalars().all()
        )

    total = 0
    day = end
    covered = 0          # sessions we have data for (skipped-existing + fetched)
    fetched_sessions = 0  # sessions actually downloaded this run
    calendar_days = 0

    while covered < days and calendar_days < days * 2:
        calendar_days += 1
        if day.weekday() < 5:
            if day in existing:
                covered += 1  # already stored -- no API call
            else:
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
                    covered += 1
                    existing.add(day)
                    if fetched_sessions % 25 == 0:
                        log.info(
                            "backfill_progress", sessions=fetched_sessions,
                            rows=total, at=str(day),
                        )
            # Publish progress every iteration so the loader sees last_progress_at
            # advancing (and, once past the loaded days, units_done climbing).
            _update_state(units_done=covered, rows=total)
        day -= dt.timedelta(days=1)

    if covered == 0:
        _update_state(
            phase="error",
            last_error=_BACKFILL_STATE["last_error"] or "Polygon returned no bars",
        )
    else:
        _update_state(phase="done")
    log.info(
        "backfill_bars_complete", sessions=covered, fetched=fetched_sessions, rows=total
    )
    return total


async def _backfill_bars_free(days: int, end: dt.date) -> int:
    """Keyless history load from the Stooq bulk daily archive.

    ONE download of the whole US market, parsed and loaded locally -- no key and
    no rate limit, unlike the old Yahoo per-ticker loop (unreliable from
    datacenter IPs). `days` bounds how far back to keep. Rows are saved in
    batches so /status climbs and a mid-load failure keeps what it loaded. Any
    failure fails loudly -- never a silent empty load.
    """
    since = end - dt.timedelta(days=int(days * 1.5) + 10)
    cap = get_settings().free_universe_max or None
    _update_state(
        source="stooq", unit="tickers", units_total=0, units_done=0, rows=0,
    )
    log.info("backfill_free_start", source="stooq", since=str(since), cap=cap)

    def _save(batch: list[dict[str, Any]]) -> int:
        with session_scope() as session:
            return repository.save_bars(session, batch)

    def _progress(tickers_done: int, rows: int) -> None:
        _update_state(units_done=tickers_done, rows=rows)

    try:
        zip_path = await stooq.download_bulk()
    except Exception as exc:  # noqa: BLE001 - loud failure, no silent empty load
        _update_state(phase="error", last_error=f"Stooq download failed: {str(exc)[:200]}")
        log.error("stooq_download_failed", error=str(exc)[:300])
        return 0

    try:
        total = stooq.load_bulk_from_zip(
            zip_path, _save, since=since, max_tickers=cap, on_progress=_progress
        )
    except Exception as exc:  # noqa: BLE001
        _update_state(phase="error", last_error=f"Stooq parse failed: {str(exc)[:200]}")
        log.error("stooq_parse_failed", error=str(exc)[:300])
        return 0
    finally:
        try:
            Path(zip_path).unlink(missing_ok=True)
        except OSError:
            pass

    if total == 0:
        _update_state(phase="error", last_error="Stooq bulk archive yielded no rows")
    else:
        _update_state(phase="done")
    log.info("backfill_bars_complete", source="stooq", rows=total)
    return total


async def backfill_sectors(limit: int | None = None, concurrency: int = 8) -> int:
    """Cache a SIC-derived GICS sector for every SEC company. Pulled once.

    Without a paid sector feed (FMP), Stage-2 sector-neutral scoring would
    silently collapse to universe-neutral. SIC is near-static, so we fetch it
    once per CIK (skipping ones already mapped), map SIC -> GICS bucket, and
    cache it. Unmappable SICs are stored with sector=None -- honest, not forced.
    Returns the number of newly mapped names.
    """
    reference = await sec_edgar.fetch_company_tickers()
    with session_scope() as session:
        done = repository.sector_map_ciks(session)
    todo = [
        r for r in reference
        if r.get("cik") and str(r["cik"]) not in done
    ]
    if limit:
        todo = todo[:limit]
    _update_state(
        phase="running", source="sec_sic", unit="companies",
        units_total=len(todo), units_done=0, rows=0, last_error=None,
    )
    log.info("backfill_sectors_start", companies=len(todo), already_mapped=len(done))
    if not todo:
        _update_state(phase="done")
        return 0

    client = sec_edgar.make_client(concurrency=concurrency)

    async def one(row: dict[str, Any]) -> dict[str, Any] | None:
        sic, desc = await sec_edgar.fetch_sic(client, row["cik"])
        return {
            "ticker": str(row["ticker"]).upper(),
            "cik": str(row["cik"]),
            "sic": sic,
            "sic_description": desc,
            "sector": sic_map.sic_to_gics(sic),
        }

    total = 0
    batch_size = 500
    async with client:
        for i in range(0, len(todo), batch_size):
            chunk = todo[i : i + batch_size]
            results = await gather_bounded([one(r) for r in chunk], concurrency)
            rows = [r for r in results if isinstance(r, dict)]
            if rows:
                with session_scope() as session:
                    repository.save_sector_map(session, rows)
                total += len(rows)
            _update_state(units_done=min(i + batch_size, len(todo)), rows=total)
            log.info("backfill_sectors_progress", done=total, total=len(todo))

    _update_state(phase="done")
    log.info("backfill_sectors_complete", mapped=total)
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
        "--sectors", action="store_true",
        help="cache the SIC->GICS sector map (pull once, near-static)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="cap the number of companies for the fundamentals/sector pass",
    )
    parser.add_argument("--skip-bars", action="store_true")
    args = parser.parse_args()

    configure_logging()
    init_db()

    async def run() -> None:
        if not args.skip_bars:
            await backfill_bars(args.days)
        if args.sectors:
            await backfill_sectors(limit=args.limit)
        if args.fundamentals:
            await backfill_fundamentals(limit=args.limit)

    asyncio.run(run())


if __name__ == "__main__":
    main()
