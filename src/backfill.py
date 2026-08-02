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
from src.ingest import fmp, polygon, sec_edgar, stooq
from src.ingest import sic as sic_map
from src.ingest.base import PermanentAPIError, gather_bounded
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
    "source": None,              # "stooq" | "fmp_batch_eod" | "polygon"
    "unit": "sessions",          # what units_done/units_total count
    "units_done": 0,
    "units_total": 0,
    "rows": 0,
    "target_sessions": 0,        # `days` requested
    "last_error": None,
    "polygon_key_present": None,
    "polygon_tier": None,        # "free" | "paid" -- why polygon is/ isn't used
    "sources_available": None,   # the ordered wide-end chain for this config
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


def _wide_end_backfill_chain(s) -> list[tuple[str, Any]]:
    """The ordered wide-end bar sources for a *multi-day* backfill.

    Chosen by CAPABILITY, not key presence. Whole-market-in-one-call sources come
    first: Stooq bulk (keyless, one download of the entire market's history) is
    primary, FMP batch EOD (whole market per day, paid FMP endpoint) is the
    fallback. Polygon grouped-daily is one call PER SESSION, so a multi-day
    backfill is `days` calls -- fine on a paid plan, but on the rate-limited free
    tier it 429s for hours, so it is included ONLY when POLYGON_TIER=paid, and
    even then it sits last behind the bulk sources.
    """
    chain: list[tuple[str, Any]] = [("stooq", _backfill_bars_stooq)]
    if s.fmp_api_key:
        chain.append(("fmp_batch_eod", _backfill_bars_fmp_batch))
    if s.polygon_api_key and s.polygon_tier == "paid":
        chain.append(("polygon", _backfill_bars_polygon))
    return chain


async def backfill_bars(days: int, end: dt.date | None = None) -> int:
    """Load `days` sessions of history from a whole-market bulk source.

    Tries the capability-ordered source chain (see `_wide_end_backfill_chain`)
    and returns the first that yields rows, logging which one actually loaded the
    data so the choice is visible next time. Polygon grouped-daily is never used
    for this multi-day load on the free tier -- that is the 429 flood this fixes.
    """
    end = end or dt.date.today()
    s = get_settings()
    chain = _wide_end_backfill_chain(s)
    names = [n for n, _ in chain]
    _update_state(
        phase="running", source=None, rows=0, units_done=0, last_error=None,
        target_sessions=days, polygon_key_present=bool(s.polygon_api_key),
        polygon_tier=s.polygon_tier, sources_available=names,
        started_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    log.info(
        "backfill_bars_start", days=days, sources=names,
        polygon_tier=s.polygon_tier, polygon_key=bool(s.polygon_api_key),
    )
    if not chain:
        _update_state(phase="error", last_error="no wide-end bar source available")
        log.error("backfill_no_source")
        return 0

    errors: dict[str, str] = {}
    for name, run in chain:
        _update_state(phase="running", source=name, last_error=None)
        log.info("backfill_source_try", source=name)
        try:
            rows = await run(days, end)
        except Exception as exc:  # noqa: BLE001 - try the next source, then report
            errors[name] = str(exc)[:200]
            log.warning("backfill_source_failed", source=name, error=str(exc)[:200])
            continue
        if rows > 0:
            _update_state(phase="done", source=name, rows=rows)
            log.info(
                "backfill_source_used", source=name, rows=rows,
                also_tried=[n for n in names if n != name and n in errors],
            )
            return rows
        errors.setdefault(name, "returned no rows")
        log.warning("backfill_source_empty", source=name)

    detail = "; ".join(f"{n}: {e}" for n, e in errors.items())
    _update_state(phase="error", last_error=f"all wide-end sources failed: {detail}")
    log.error("backfill_all_sources_failed", errors=errors)
    return 0


async def _backfill_bars_polygon(days: int, end: dt.date) -> int:
    """One grouped-daily call per session. Holidays return empty and are skipped.

    Reached ONLY on a paid Polygon tier (see the chain): a multi-day loop of
    per-session calls would 429 for hours on the free tier. Each day's call is
    bounded by a hard timeout so a single hung request can never freeze the whole
    backfill (and hold `_backfill_lock`). Progress is published to /status after
    every session so the loader visibly climbs.
    """
    timeout = get_settings().polygon_fetch_timeout
    _update_state(unit="sessions", units_total=days)

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

    # Phase/source are owned by the orchestrator (backfill_bars); this source just
    # reports how much it loaded.
    log.info(
        "backfill_polygon_complete", sessions=covered, fetched=fetched_sessions,
        rows=total,
    )
    return total


async def _backfill_bars_stooq(days: int, end: dt.date) -> int:
    """Keyless history load from the Stooq bulk daily archive -- the primary
    wide-end source.

    ONE download of the whole US market, parsed and loaded locally: no key, no
    rate limit, whole market and all of history in a single request. `days`
    bounds how far back to keep. Rows are saved in batches so /status climbs and
    a mid-load failure keeps what it loaded. A hard failure RAISES (descriptive
    message) so the orchestrator can record it and try the next source rather
    than silently loading nothing.
    """
    since = end - dt.timedelta(days=int(days * 1.5) + 10)
    cap = get_settings().free_universe_max or None
    _update_state(unit="tickers", units_total=0, units_done=0, rows=0)
    log.info("backfill_stooq_start", since=str(since), cap=cap)

    def _save(batch: list[dict[str, Any]]) -> int:
        with session_scope() as session:
            return repository.save_bars(session, batch)

    def _progress(tickers_done: int, rows: int) -> None:
        _update_state(units_done=tickers_done, rows=rows)

    try:
        zip_path = await stooq.download_bulk()
    except Exception as exc:
        raise RuntimeError(f"Stooq download failed: {str(exc)[:200]}") from exc

    try:
        total = stooq.load_bulk_from_zip(
            zip_path, _save, since=since, max_tickers=cap, on_progress=_progress
        )
    except Exception as exc:
        raise RuntimeError(f"Stooq parse failed: {str(exc)[:200]}") from exc
    finally:
        try:
            Path(zip_path).unlink(missing_ok=True)
        except OSError:
            pass

    log.info("backfill_stooq_complete", rows=total)
    return total


async def _backfill_bars_fmp_batch(days: int, end: dt.date) -> int:
    """Whole-market EOD from FMP's batch endpoint -- the bulk fallback.

    One call per session (every symbol for one date), like Polygon grouped-daily
    but on FMP's higher paid rate limits. It is a paid-tier endpoint, so we PROBE
    it on the first needed day: if the plan lacks it (402/403 -> PermanentAPIError)
    we log that and raise so the chain falls through to Polygon-or-error rather
    than looping a dead endpoint. Skips days already stored (same as Polygon).
    """
    _update_state(unit="sessions", units_total=days)
    with session_scope() as session:
        existing: set[dt.date] = set(
            session.execute(select(DailyBar.date).distinct()).scalars().all()
        )

    # Capability probe on the most recent business day we still need.
    probe_day = end
    while probe_day.weekday() >= 5 or probe_day in existing:
        probe_day -= dt.timedelta(days=1)
        if (end - probe_day).days > 10:  # everything recent already stored
            break
    try:
        first = await fmp.fetch_batch_eod(probe_day)
    except PermanentAPIError as exc:
        log.warning("fmp_batch_eod_unavailable", detail=str(exc)[:200])
        raise RuntimeError(
            f"FMP plan lacks batch-request-end-of-day-prices ({str(exc)[:160]})"
        ) from exc
    log.info("fmp_batch_eod_capability", available=True, probe_day=str(probe_day))

    total = 0
    fetched = 0
    if first:
        with session_scope() as session:
            repository.save_bars(session, first)
        total += len(first)
        fetched += 1
        existing.add(probe_day)
        _update_state(units_done=1, rows=total)

    covered = len(existing)
    day = end
    calendar_days = 0
    while covered < days and calendar_days < days * 2:
        calendar_days += 1
        if day.weekday() < 5 and day not in existing:
            try:
                rows = await fmp.fetch_batch_eod(day)
            except Exception as exc:  # noqa: BLE001 - one bad day is survivable
                log.warning("fmp_batch_day_failed", date=str(day), error=str(exc)[:200])
                _update_state(last_error=f"{day}: {str(exc)[:200]}")
                rows = []
            if rows:
                with session_scope() as session:
                    repository.save_bars(session, rows)
                total += len(rows)
                fetched += 1
                existing.add(day)
            covered = len(existing)
            _update_state(units_done=min(covered, days), rows=total)
        day -= dt.timedelta(days=1)

    log.info("fmp_batch_eod_complete", fetched=fetched, rows=total)
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
        sector = sic_map.sic_to_gics(sic)
        return {
            "ticker": str(row["ticker"]).upper(),
            "cik": str(row["cik"]),
            "sic": sic,
            "sic_description": desc,
            "sector": sector,
            # Provenance for IC: a real mapping vs an unmappable SIC. Never a guess.
            "sector_source": "sic" if sector else "unknown",
        }

    total = 0
    mapped = 0
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
                mapped += sum(1 for r in rows if r["sector"])
            _update_state(units_done=min(i + batch_size, len(todo)), rows=total)
            log.info("backfill_sectors_progress", done=total, total=len(todo))

    unmapped = total - mapped
    ratio = round(mapped / total, 3) if total else 0.0
    _update_state(phase="done")
    # A sudden shift in this ratio between runs means SEC changed the SIC data
    # or the feed shape -- worth an eyeball.
    log.info(
        "backfill_sectors_complete", mapped=mapped, unmapped=unmapped,
        total=total, mapped_ratio=ratio,
    )
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
