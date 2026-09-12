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
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from src.config.settings import get_settings
from src.ingest import (
    fmp,
    polygon,
    sec_cache,
    sec_datasets,
    sec_edgar,
    sec_frames,
    stooq,
    xbrl,
    yahoo,
)
from src.ingest import sic as sic_map
from src.ingest.base import PermanentAPIError, gather_bounded
from src.logging_config import configure_logging
from src.storage import repository
from src.storage.db import init_db, session_scope
from src.storage.models import DailyBar

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


# Last completed result per kind (bars/sectors/fundamentals/earnings), so the
# four /admin buttons can each show their own outcome, not one shared line.
_BACKFILL_RESULTS: dict[str, dict[str, Any]] = {}

# Per-quarter extraction report from the last fundamentals load: how many rows
# the consolidated filter dropped and why, plus every validation rejection. This
# is the evidence that the filter is doing its job, so it is surfaced on /admin
# rather than only living in the logs.
_LAST_EXTRACTION_REPORTS: dict[str, dict[str, Any]] = {}


# What the last frames sweep did, per quarter. A list rather than a dict: the
# sweep is ordered and small, and /admin shows it as it ran.
_FRAMES_REPORTS: list[dict[str, Any]] = []


def get_extraction_reports() -> dict[str, dict[str, Any]]:
    """Per-quarter XBRL extraction diagnostics from the last fundamentals load."""
    return {k: dict(v) for k, v in _LAST_EXTRACTION_REPORTS.items()}


# A full reload (wipe + reload + verify) driven from /admin. Tracked separately
# from _BACKFILL_STATE so the page can say "wiping", "loading", "verifying"
# rather than only "running", and so the wipe count survives to the report.
_RELOAD_STATE: dict[str, Any] = {
    # fetching -> replacing -> verifying. There is no "wiping" phase any more:
    # nothing is deleted until every quarter is on disk, and the delete happens
    # in the same transaction as the reload.
    "phase": "idle",          # idle | fetching | replacing | verifying | done | error
    "started_at": None,
    "finished_at": None,
    "rows_deleted": None,
    "rows_written": None,
    "quarters_requested": None,
    "quarters": None,         # the quarters this reload asked for
    "staged": None,           # per-quarter download status, filled during fetch
    "quarters_available": None,  # how many SEC has actually published
    "unpublished": None,      # 404s -- expected for the newest quarter
    "quarters_loaded": None,
    "data_intact": None,      # True after an abort that touched nothing
    "last_error": None,
    "verification": None,
}


def get_reload_state() -> dict[str, Any]:
    """Live state of the /admin full-reload job."""
    return dict(_RELOAD_STATE)


# Result of the last raw num.txt dump requested from /admin. A dump downloads a
# ~100MB ZIP, so it runs as a background job and parks its output here rather
# than risking an HTTP timeout mid-download.
_RAW_FACTS_STATE: dict[str, Any] = {
    "phase": "idle",        # idle | downloading | parsing | done | error
    "request": None,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "result": None,
}


def get_raw_facts_state() -> dict[str, Any]:
    """Live state + last result of the /admin raw-facts dump."""
    return dict(_RAW_FACTS_STATE)


async def run_raw_facts_dump(
    ticker: str,
    year: int,
    quarter: int,
    tags: tuple[str, ...],
    ddate: str | None = None,
    cik: str | None = None,
) -> dict[str, Any]:
    """Download one quarterly ZIP and dump a company's rows for `tags`."""
    from src.ingest import raw_facts

    req = {
        "ticker": ticker.upper(), "dataset": f"{year}q{quarter}",
        "tags": list(tags), "ddate": ddate, "cik": cik,
    }
    _RAW_FACTS_STATE.update(
        phase="downloading", request=req, result=None, last_error=None,
        started_at=dt.datetime.now(dt.UTC).isoformat(), finished_at=None,
    )
    try:
        zbytes = await download_dataset_for_dump(year, quarter)
        _RAW_FACTS_STATE["phase"] = "parsing"
        result = await asyncio.to_thread(
            raw_facts.dump_company_facts, zbytes, ticker, tags, ddate, cik
        )
        result["dataset"] = f"{year}q{quarter}"
        _RAW_FACTS_STATE.update(
            phase="done", result=result,
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        return result
    except Exception as exc:  # noqa: BLE001 - surfaced on /admin
        _RAW_FACTS_STATE.update(
            phase="error", last_error=str(exc)[:500],
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        log.exception("raw_facts_dump_failed", error=str(exc)[:300])
        raise


async def download_dataset_for_dump(year: int, quarter: int) -> bytes:
    """The dump uses the SAME cached path as the backfill.

    Two independent downloaders of the same 100MB file, with no shared cache, is
    what earned the 429s -- every tap of the dump button re-fetched a file
    already on disk.
    """
    return await sec_datasets.download_dataset(year, quarter)


def _update_reload(**kw: Any) -> None:
    _RELOAD_STATE.update(kw)


def wipe_fundamentals() -> int:
    """Delete every fundamentals row. Returns how many were removed.

    The old parser's rows cannot be repaired in place: once the tag and its
    dimensions are discarded, a wrong number is indistinguishable from a right
    one. So a reload starts from empty rather than upserting on top.

    NOT part of the reload path any more -- `_replace_fundamentals` does the
    delete inside the same transaction as the reload, so a failure half way
    through leaves the old rows in place. This stays for the rare case of
    deliberately emptying the table, and is deliberately the only thing in this
    module that deletes without a replacement in hand.
    """
    from sqlalchemy import delete, func, select

    from src.storage.models import Fundamental

    with session_scope() as session:
        before = session.execute(
            select(func.count()).select_from(Fundamental)
        ).scalar_one()
        session.execute(delete(Fundamental))
    log.warning("fundamentals_wiped", rows_deleted=before)
    return before


def _replace_fundamentals(
    qs: list[tuple[int, int]], cik_map: Mapping[str, str]
) -> tuple[int, int]:
    """Delete the old rows and write the new ones in ONE transaction.

    Every quarter must already be in the disk cache -- this does no network I/O,
    so the only way it can fail is a parse, a validation abort, or the write
    itself, and all three roll the delete back with it. The table is never
    observably empty and never half-loaded.

    Quarters are parsed one at a time and the frame is released before the next,
    so peak memory is one quarter's num.txt, not seven.
    """
    from sqlalchemy import delete, func, select

    from src.storage.models import Fundamental

    written = 0
    loaded = 0
    with session_scope() as session:
        before = session.execute(
            select(func.count()).select_from(Fundamental)
        ).scalar_one()
        session.execute(delete(Fundamental))
        log.warning("fundamentals_delete_staged", rows=before,
                    note="rolls back unless every quarter loads")

        for year, q in qs:
            zbytes = sec_cache.cached_bytes(year, q)
            if zbytes is None:
                # Prefetch said it was there. Something removed or truncated it
                # between then and now -- abort and keep the old rows.
                raise RuntimeError(
                    f"{year}q{q} left the cache mid-reload; nothing was deleted"
                )
            sub, num = sec_datasets.parse_dataset(zbytes)
            del zbytes
            rows, report = xbrl.extract_and_validate(sub, num, cik_map)
            del sub, num
            _LAST_EXTRACTION_REPORTS[f"{year}q{q}"] = report.as_dict()
            raw = len(rows)
            rows = _collapse_earliest_filing(rows)
            log.info(
                "sec_quarter_deduped", year=year, quarter=q, raw=raw,
                kept=len(rows), dropped=raw - len(rows),
            )
            for j in range(0, len(rows), 5000):
                written += repository.save_fundamentals(session, rows[j : j + 5000])
            loaded += 1
            _update_reload(rows_written=written, quarters_loaded=loaded)
            log.info("fundamentals_reload_progress", year=year, quarter=q,
                     rows=written, quarters_loaded=loaded)

        if written == 0:
            # Committing here would swap 1.2M rows for nothing at all.
            raise RuntimeError(
                f"reload extracted 0 rows from {len(qs)} quarters; "
                f"the existing table was left untouched"
            )
    # Which companies are drawable has just changed wholesale, and the search
    # caches that list.
    from src.company.lookup import reset_cache

    reset_cache()
    log.warning("fundamentals_replaced", rows_deleted=before, rows_written=written)
    return before, written


async def reload_fundamentals(quarters: int = 7) -> dict[str, Any]:
    """Replace the fundamentals table, then verify the five companies.

    Ordering is the whole point. A previous version wiped first and discovered
    afterwards that SEC was returning 429 on every quarter, which deleted
    1,231,927 rows and wrote none. So:

      1. Download every quarter to the disk cache. A 404 means SEC has not
         published that quarter yet -- skipped, not fatal. Any other failure
         aborts here, before a single row has been touched.
      2. Delete and reload inside one transaction, parsing from disk only.
      3. Verify.

    Success is measured against the quarters that EXIST, not the quarters that
    were asked for. `recent_quarters` walks back from today, so the newest one
    is routinely a few weeks from being published; loading six of seven for
    that reason is a complete reload, not a partial one.

    Returns the full report: rows deleted/written, the per-quarter extraction
    diagnostics, and the verification result.
    """
    from src.company.verify import concept_coverage, verify_companies
    from src.ingest.sec_cache import available_quarters, prefetch_quarters

    qs = sec_datasets.recent_quarters(dt.date.today(), quarters)
    _RELOAD_STATE.update(
        phase="fetching", started_at=dt.datetime.now(dt.UTC).isoformat(),
        finished_at=None, rows_deleted=None, rows_written=None,
        quarters_requested=quarters, quarters=[f"{y}q{q}" for y, q in qs],
        staged=[], quarters_loaded=0, quarters_available=None,
        unpublished=None, data_intact=True, last_error=None, verification=None,
    )
    _LAST_EXTRACTION_REPORTS.clear()

    # --- 1. everything obtainable? ------------------------------------------
    try:
        staged = await prefetch_quarters(
            qs, on_progress=lambda s: _update_reload(staged=s)
        )
        _update_reload(staged=staged)
    except Exception as exc:  # noqa: BLE001 - reported on /admin
        _update_reload(
            phase="error", data_intact=True, rows_deleted=0,
            last_error=f"download failed, nothing was deleted: {str(exc)[:400]}",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        log.error("fundamentals_reload_aborted_before_delete", error=str(exc)[:300])
        raise

    have = available_quarters(staged)
    unpublished = [s["quarter"] for s in staged if s["source"] == "unpublished"]
    _update_reload(quarters_available=len(have), unpublished=unpublished)
    if not have:
        # Every quarter unpublished is not "nothing to do" -- it means the
        # quarter arithmetic is wrong or the URL moved. Either way, do not wipe.
        _update_reload(
            phase="error", data_intact=True, rows_deleted=0,
            last_error=f"none of the {len(qs)} requested quarters are published; "
                       f"nothing was deleted",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        raise RuntimeError(
            f"no published quarter among {[f'{y}q{q}' for y, q in qs]}"
        )
    if unpublished:
        log.info("fundamentals_reload_skipping_unpublished", quarters=unpublished,
                 loading=len(have))

    # --- 2. swap, transactionally -------------------------------------------
    _update_reload(phase="replacing", data_intact=True)
    cik_map = await _cik_to_ticker()
    try:
        deleted, written = await asyncio.to_thread(
            _replace_fundamentals, have, cik_map
        )
    except Exception as exc:  # noqa: BLE001 - the transaction rolled back
        _update_reload(
            phase="error", data_intact=True, rows_deleted=0,
            last_error=f"reload rolled back, existing rows kept: {str(exc)[:400]}",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        log.exception("fundamentals_reload_rolled_back", error=str(exc)[:300])
        raise

    # --- 3. verify -----------------------------------------------------------
    _update_reload(phase="verifying", rows_deleted=deleted, rows_written=written,
                   data_intact=False)
    try:
        verification = await asyncio.to_thread(verify_companies)
        coverage = await asyncio.to_thread(concept_coverage)
        verification["coverage"] = coverage
    except Exception as exc:  # noqa: BLE001 - the data is loaded; only the check failed
        _update_reload(
            phase="error", last_error=f"loaded, but verification failed: {str(exc)[:400]}",
            finished_at=dt.datetime.now(dt.UTC).isoformat(),
        )
        log.exception("fundamentals_verify_failed", error=str(exc)[:300])
        raise

    _update_reload(
        phase="done", verification=verification,
        finished_at=dt.datetime.now(dt.UTC).isoformat(),
    )
    log.info(
        "fundamentals_reload_complete", rows_deleted=deleted,
        rows_written=written, quarters_loaded=len(have),
        unpublished=unpublished, verification_passed=verification["passed"],
    )
    return {
        "rows_deleted": deleted,
        "rows_written": written,
        "quarters_loaded": len(have),
        "unpublished": unpublished,
        "staged": staged,
        "extraction": get_extraction_reports(),
        "verification": verification,
    }


def get_backfill_state() -> dict[str, Any]:
    """A copy of the live backfill diagnostics + per-kind last results, for /status."""
    out = dict(_BACKFILL_STATE)
    out["results"] = {k: dict(v) for k, v in _BACKFILL_RESULTS.items()}
    return out


def record_backfill_result(kind: str, rows: int, error: str | None = None) -> None:
    """Record the outcome of a single backfill kind, for its /diagnostics button."""
    _BACKFILL_RESULTS[kind] = {
        "rows": int(rows or 0),
        "error": error[:300] if error else None,
        "at": dt.datetime.now(dt.UTC).isoformat(),
    }


def _update_state(**kw: Any) -> None:
    _BACKFILL_STATE.update(kw)
    _BACKFILL_STATE["last_progress_at"] = dt.datetime.now(dt.UTC).isoformat()


def record_backfill_error(msg: str) -> None:
    """Mark the backfill failed (called by the API's background wrapper too)."""
    _update_state(phase="error", last_error=msg[:300])


def _wide_end_backfill_chain(s) -> list[tuple[str, Any]]:
    """The ordered wide-end bar sources for a *multi-day* backfill.

    Chosen by CAPABILITY, not key presence. Keyless sources come first, so the
    service loads prices with no keys configured at all: Yahoo's batched
    download (200 symbols per call) is primary, with Stooq's bulk archive
    behind it for the day its browser challenge lifts. FMP batch EOD (whole
    market per day, paid FMP endpoint) follows. Polygon grouped-daily is one call PER SESSION, so a multi-day
    backfill is `days` calls -- fine on a paid plan, but on the rate-limited free
    tier it 429s for hours, so it is included ONLY when POLYGON_TIER=paid, and
    even then it sits last behind the bulk sources.
    """
    # Yahoo first, because it is the keyless source that currently WORKS.
    # Stooq's bulk archive was primary here and is no longer reachable: the
    # whole site now answers with a JavaScript proof-of-work browser challenge,
    # and /db/h/d_us_txt.zip returns a genuine "page does not exist". Solving a
    # bot challenge to take the file is not something this service should do.
    # It stays in the chain BELOW Yahoo rather than being deleted: it is one
    # request for the entire market and all of its history when it works, so if
    # Stooq restores it, the fallback picks it up again with no code change.
    chain: list[tuple[str, Any]] = [
        ("yahoo_batch", _backfill_bars_yahoo),
        ("stooq", _backfill_bars_stooq),
    ]
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


async def _bar_universe(cap: int | None) -> list[str]:
    """The symbols to ask a per-ticker source for.

    Tickers already carrying bars come first and are used alone when present:
    a daily top-up should refresh what the site actually shows, not re-crawl
    every registrant. On a cold database it falls back to SEC's company list,
    which is the same free seed the universe builder uses.
    """
    with session_scope() as session:
        known = session.execute(
            select(DailyBar.ticker).distinct()
        ).scalars().all()
    tickers = sorted({t for t in known if t})
    if not tickers:
        cik_map = await _cik_to_ticker()
        tickers = sorted({t for t in cik_map.values() if t})
    if cap:
        tickers = tickers[:cap]
    return tickers


async def _backfill_bars_yahoo(days: int, end: dt.date) -> int:
    """Keyless history for the whole universe, batched through Yahoo.

    yfinance takes 200 symbols per call, so ~8,000 names cost ~40 requests
    rather than 8,000. That is a different shape from a bulk archive -- more
    requests, and the universe has to be enumerated first -- but it needs no
    key and no browser challenge, which is what makes it the working keyless
    primary now that Stooq's archive is gone.

    Raises on a hard failure so the orchestrator records the reason and tries
    the next source, rather than reporting a silent zero.
    """
    cap = get_settings().free_universe_max or None
    tickers = await _bar_universe(cap)
    if not tickers:
        raise RuntimeError(
            "no tickers to load: the bars table is empty and SEC's company "
            "list could not be read"
        )
    # The same 1.5x cushion the bulk path uses: `days` counts trading sessions,
    # and a calendar window has to cover weekends and holidays to contain them.
    start = end - dt.timedelta(days=int(days * 1.5) + 10)
    _update_state(
        unit="tickers", units_total=len(tickers), units_done=0, rows=0,
    )
    log.info(
        "backfill_yahoo_start", tickers=len(tickers), start=str(start),
        end=str(end),
    )

    rows = await yahoo.fetch_daily_bars_batch(tickers, start, end)
    if not rows:
        raise RuntimeError(
            "Yahoo returned no rows: " + (yahoo.last_error() or "empty response")
        )

    total = 0
    for i in range(0, len(rows), 5000):
        with session_scope() as session:
            total += repository.save_bars(session, rows[i : i + 5000])
        _update_state(rows=total)
    _update_state(units_done=len(tickers), rows=total)
    log.info("backfill_yahoo_complete", rows=total, tickers=len(tickers))
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


async def backfill_company_names() -> int:
    """Persist the company names SEC already hands us on every load.

    NOT a new data source. `sec_edgar.fetch_company_tickers()` returns
    {ticker, name, cik} and the fundamentals and earnings loads both already
    call it -- they keep the CIK map and drop the name. This writes the name
    into `universe`, which is where the company page and the name search
    already look for it.

    Additive and idempotent: one row per ticker for today, upserted. It writes
    only the identity columns, so it cannot disturb a universe snapshot built
    with prices and liquidity in it.
    """
    reference = await sec_edgar.fetch_company_tickers()
    today = dt.date.today()
    rows = [
        {
            "ticker": str(r["ticker"]).upper(),
            "name": r.get("name"),
            "cik": str(r.get("cik")) if r.get("cik") is not None else None,
            "security_type": r.get("security_type"),
        }
        for r in reference
        if r.get("ticker") and r.get("name")
    ]
    if not rows:
        _update_state(phase="error", last_error="SEC returned no company names")
        return 0
    written = 0
    for i in range(0, len(rows), 5000):
        with session_scope() as session:
            written += repository.save_universe(session, today, rows[i : i + 5000])

    # Search caches the name list for half an hour. Without this the names are
    # searchable the moment they land but MISSPELLINGS are not, for thirty
    # minutes -- which is exactly the window in which somebody tests it.
    from src.company.lookup import reset_cache

    reset_cache()
    log.info("company_names_loaded", rows=written, as_of=today.isoformat())
    return written


# Companies `company_tickers.json` does not carry. See
# `backfill_missing_company_names` for why that file is not enough on its own.
_NAME_FALLBACK_RPS = 5.0

# Companies whose legal name genuinely IS their ticker, so "name equals
# ticker" is the right answer rather than a missing one. They match the
# broken-name query and must not be counted as failures or rewritten.
#
# A hardcoded list is the honest shape here. The alternative is a rule, and
# there is no rule: nothing in the data distinguishes "the name is RH" from
# "the name was never loaded" except knowing that RH is a company called RH.
# Three names, checked by hand against their filings.
# How many former names to hold before writing them. Small enough that an
# interrupted run has left most of its work behind, large enough that the
# write is not one statement per company.
_FORMER_NAME_FLUSH = 50

_NAME_IS_THE_TICKER: frozenset[str] = frozenset({
    "RH",     # RH, formerly Restoration Hardware
    "CTW",    # CTW Cayman
    "VTEX",   # VTEX, the Brazilian commerce platform
})


def _drawable_without_a_name(session) -> list[str]:
    """Tickers with a balance sheet to draw and no company name to put on it.

    The rule is the one `company/balancesheet.py` uses to READ the name -- the
    most recent universe row at or before today -- rather than "has any row
    with a name". Matching the reader matters: a ticker whose only named row
    is older than an unnamed one renders as its own ticker, and a looser query
    here would call that fixed.

    Written in two plain queries rather than one DISTINCT ON, because
    DISTINCT ON is Postgres-only and the tests run on SQLite.
    """
    from src.storage.models import Fundamental, UniverseSnapshot

    drawable = {
        t for (t,) in session.execute(
            select(Fundamental.ticker)
            .where(Fundamental.metric == "total_assets", Fundamental.value > 0)
            .distinct()
        )
    }
    if not drawable:
        return []

    latest: dict[str, tuple[dt.date, str | None]] = {}
    for ticker, as_of, name in session.execute(
        select(
            UniverseSnapshot.ticker, UniverseSnapshot.as_of_date,
            UniverseSnapshot.name,
        )
    ):
        seen = latest.get(ticker)
        if seen is None or as_of > seen[0]:
            latest[ticker] = (as_of, name)

    broken = []
    for ticker in sorted(drawable):
        row = latest.get(ticker)
        if row is None:                      # not in the universe at all
            broken.append(ticker)
            continue
        name = (row[1] or "").strip()
        if not name or name == ticker:
            broken.append(ticker)
    return broken


def _cik_for(session, ticker: str) -> str | None:
    """Any CIK this database holds for a ticker, newest first.

    Three tables carry one and they disagree in coverage: `universe` has it for
    companies the reference load reached, `sector_map` for anything the SIC
    pull touched, and `filing_events` for anything that has ever filed. A
    ticker missing from the first is exactly the case this function exists for,
    so all three are tried before giving up.
    """
    from src.storage.models import FilingEvent, SectorMap, UniverseSnapshot

    found = session.execute(
        select(UniverseSnapshot.cik)
        .where(UniverseSnapshot.ticker == ticker, UniverseSnapshot.cik.isnot(None))
        .order_by(UniverseSnapshot.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    if found:
        return str(found).strip() or None

    found = session.execute(
        select(SectorMap.cik).where(
            SectorMap.ticker == ticker, SectorMap.cik.isnot(None)
        ).limit(1)
    ).scalar_one_or_none()
    if found:
        return str(found).strip() or None

    found = session.execute(
        select(FilingEvent.cik)
        .where(FilingEvent.ticker == ticker, FilingEvent.cik.isnot(None))
        .order_by(FilingEvent.filing_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (str(found).strip() or None) if found else None


async def backfill_missing_company_names(
    *, limit: int | None = None, rps: float = _NAME_FALLBACK_RPS,
    skip: frozenset[str] = _NAME_IS_THE_TICKER,
) -> dict[str, Any]:
    """Name the drawable companies that `company_tickers.json` leaves out.

    `backfill_company_names` reads that file, which lists companies with a
    CURRENT exchange listing. A company page exists for anything with filed
    fundamentals, and those two sets are not the same: a delisted filer, a
    completed SPAC, a ticker that moved -- AVB among them -- has balance sheets
    to draw and no entry in the reference file. Its page rendered as
    "AVB (AVB) Balance Sheet", the company name field silently empty.

    The submissions API answers per CIK rather than per listing, so it has the
    name for anything that has ever filed. One request each, which is why this
    is a repair pass over a short list and not how names are loaded normally.

    Paced at 5 requests/second against SEC's limit of 10. The shared token
    bucket would allow 9; this is slower on purpose, because the whole job is
    a few dozen requests and there is nothing to gain by crowding a limit whose
    penalty is a ten-minute block.

    Never invents a CIK. The database is asked first and SEC's own ticker
    file second; a ticker neither of them knows is reported and skipped.
    Guessing one would write another company's name onto this page, which is
    worse than the empty field it replaces.

    `_NAME_IS_THE_TICKER` is left alone: those companies match the query and
    are not broken, and the count of remaining matches after a successful run
    is exactly that set.
    """
    with session_scope() as session:
        matched = _drawable_without_a_name(session)
        targets = [t for t in matched if t not in skip]
        if limit is not None:
            targets = targets[:limit]
        ciks = {t: _cik_for(session, t) for t in targets}
    by_design = sorted(t for t in matched if t in skip)

    # A ticker with no CIK in any of our tables may still have one in SEC's
    # own reference file -- which is the case for tickers that arrived in that
    # file after the last universe load. Consulting it is not inventing a CIK;
    # it is asking the same authority the rest of this module asks, and it is
    # the difference between "cannot be named" and "we had not looked".
    #
    # Fetched once, and only when something actually needs it.
    if any(not c for c in ciks.values()):
        try:
            reference = await sec_edgar.fetch_company_tickers()
        except Exception as exc:  # noqa: BLE001 - the DB CIKs still stand
            log.warning("company_name_reference_failed", error=str(exc)[:200])
            reference = []
        by_ticker = {
            str(r.get("ticker", "")).upper(): r.get("cik")
            for r in reference if r.get("ticker") and r.get("cik") is not None
        }
        for ticker, cik in list(ciks.items()):
            if cik:
                continue
            found = by_ticker.get(ticker.upper())
            if found is not None:
                ciks[ticker] = str(found).strip()
                log.info("company_name_cik_from_reference",
                         ticker=ticker, cik=ciks[ticker])

    missing_cik = sorted(t for t, c in ciks.items() if not c)
    resolvable = [(t, c) for t, c in sorted(ciks.items()) if c]

    log.info(
        "company_name_repair_start",
        matched=len(matched), candidates=len(targets),
        resolvable=len(resolvable), no_cik=len(missing_cik),
        name_is_the_ticker=len(by_design),
    )
    if not resolvable:
        return {"matched": len(matched), "checked": len(targets), "named": 0,
                "written": 0, "no_cik": missing_cik, "unnamed": [],
                "name_is_the_ticker": by_design}

    delay = 1.0 / rps if rps > 0 else 0.0
    rows: list[dict[str, Any]] = []
    unnamed: list[str] = []
    async with sec_edgar.make_client(concurrency=1) as client:
        for ticker, cik in resolvable:
            try:
                payload = await sec_edgar.fetch_submissions(client, cik)
            except Exception as exc:  # noqa: BLE001 - one bad CIK is survivable
                log.warning("company_name_fetch_failed", ticker=ticker,
                            cik=cik, error=str(exc)[:200])
                unnamed.append(ticker)
                await asyncio.sleep(delay)
                continue
            name = str((payload or {}).get("name") or "").strip()
            if not name:
                # A 200 with no name is a real answer and not an error: some
                # CIKs are filing agents or trusts with no entity name. Left
                # empty rather than filled with the ticker.
                log.info("company_name_absent", ticker=ticker, cik=cik)
                unnamed.append(ticker)
            else:
                rows.append({
                    "ticker": ticker, "name": name[:256],
                    "cik": str(cik).strip()[:16],
                })
            await asyncio.sleep(delay)

    written = 0
    if rows:
        # Today's date, the same as `backfill_company_names`. NOT backdated
        # into the last snapshot: that snapshot is a record of what was known
        # then, and editing it to say a name was there would be a small lie in
        # the one table the point-in-time reads depend on.
        today = dt.date.today()
        for i in range(0, len(rows), 2000):
            with session_scope() as session:
                written += repository.save_universe(
                    session, today, rows[i : i + 2000]
                )

        from src.company.lookup import reset_cache

        reset_cache()

    log.info("company_name_repair_done", named=len(rows), written=written,
             no_cik=len(missing_cik), unnamed=len(unnamed))
    return {
        "matched": len(matched), "checked": len(targets), "named": len(rows),
        "written": written, "no_cik": missing_cik, "unnamed": unnamed,
        "name_is_the_ticker": by_design,
    }


async def backfill_former_names(
    *, limit: int | None = None, rps: float = _NAME_FALLBACK_RPS,
    only_missing: bool = True,
) -> dict[str, Any]:
    """Record the name each drawable company filed under before its current one.

    `universe.name` is SEC's CURRENT name for a CIK. The filings on a company
    page were filed earlier, sometimes under a different name, and a rename is
    invisible in the data: Equity Residential's balance sheets now render under
    "VIVMARK RESIDENTIAL" with nothing on the page connecting the two. SEC has
    the same problem and does not solve it -- `companyfacts` labels those
    identical facts with the new name as well -- so the former name has to be
    fetched and stored, or the page cannot show it.

    One submissions request per company, which is why this is a periodic job
    and not something a page render does. Paced at 5 requests/second against
    SEC's limit of 10.

    `only_missing` skips companies that already have a former name recorded, so
    a re-run costs requests only for the ones it has never asked about. Pass
    False after a long gap, when a company may have renamed since.

    Writes NOTHING when SEC reports no former name. The absence is the common
    case -- most companies have never renamed -- and a blank is the honest
    representation of it.
    """
    from sqlalchemy import select

    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as session:
        drawable = {
            t for (t,) in session.execute(
                select(Fundamental.ticker)
                .where(Fundamental.metric == "total_assets", Fundamental.value > 0)
                .distinct()
            )
        }
        latest: dict[str, tuple[dt.date, str | None, str | None, str | None]] = {}
        for ticker, as_of, cik, former, name in session.execute(
            select(
                UniverseSnapshot.ticker, UniverseSnapshot.as_of_date,
                UniverseSnapshot.cik, UniverseSnapshot.former_name,
                UniverseSnapshot.name,
            )
        ):
            seen = latest.get(ticker)
            if seen is None or as_of > seen[0]:
                latest[ticker] = (as_of, cik, former, name)

    targets: list[tuple[str, str, dt.date, str]] = []
    for ticker in sorted(drawable):
        row = latest.get(ticker)
        if row is None or not row[1]:
            continue
        if only_missing and (row[2] or "").strip():
            continue
        targets.append((ticker, str(row[1]).strip(), row[0], row[3] or ""))
    if limit is not None:
        targets = targets[:limit]

    log.info("former_names_start", candidates=len(targets))
    if not targets:
        return {"checked": 0, "named": 0, "written": 0}

    delay = 1.0 / rps if rps > 0 else 0.0
    # Keyed by the snapshot date each ticker's newest row already sits on.
    # NOT today's: `save_universe` upserts on (as_of_date, ticker), so writing
    # a former name at a date a ticker has no row for would INSERT one whose
    # `name` and `cik` are NULL -- and that row, being the newest, is the one
    # the company page reads. The page would lose the company's name to a
    # change whose entire purpose is showing more of it.
    by_date: dict[dt.date, list[dict[str, Any]]] = {}
    named = 0
    written = 0

    def _flush() -> int:
        """Persist what has been fetched so far and forget it.

        Called periodically rather than once at the end. This job is ~6,000
        sequential requests at 5/second, so it runs for the better part of an
        hour; holding every result until the last one means a disconnect an
        hour in writes nothing at all, and the re-run starts from zero because
        `only_missing` has nothing to skip.
        """
        done = 0
        for as_of, rows in by_date.items():
            for i in range(0, len(rows), 2000):
                with session_scope() as session:
                    done += repository.save_universe(
                        session, as_of, rows[i : i + 2000]
                    )
        by_date.clear()
        return done

    async with sec_edgar.make_client(concurrency=1) as client:
        for ticker, cik, as_of, current in targets:
            try:
                payload = await sec_edgar.fetch_submissions(client, cik)
            except Exception as exc:  # noqa: BLE001 - one bad CIK is survivable
                log.warning("former_name_fetch_failed", ticker=ticker,
                            cik=cik, error=str(exc)[:200])
                await asyncio.sleep(delay)
                continue
            former, until = _latest_former_name(payload)
            # SEC re-records the SAME name as a formerNames entry surprisingly
            # often -- ADM, CSCO and Citigroup all carry one dated yesterday
            # whose name is what they are still called. Storing those would
            # fill the column with "formerly <the current name>" for hundreds
            # of companies that have never renamed.
            if former and former.strip().upper() == current.strip().upper():
                former = None
            if former:
                by_date.setdefault(as_of, []).append({
                    "ticker": ticker, "former_name": former[:256],
                    "former_name_until": until,
                })
                named += 1
                if sum(len(v) for v in by_date.values()) >= _FORMER_NAME_FLUSH:
                    written += _flush()
                    log.info("former_names_progress", named=named,
                             written=written)
            await asyncio.sleep(delay)

    written += _flush()
    log.info("former_names_done", named=named, written=written)
    return {"checked": len(targets), "named": named, "written": written}


def _latest_former_name(
    payload: Mapping[str, Any],
) -> tuple[str | None, dt.date | None]:
    """The most recent entry in SEC's `formerNames`, and when it lapsed.

    The most recent one, not all of them: the page has room for the name a
    reader might recognise, and for Helix that is "HELIX ENERGY SOLUTIONS
    GROUP INC" rather than also "CAL DIVE INTERNATIONAL INC" from 2006. The
    full history stays one click away on EDGAR.

    Pure, so it can be tested without a network.
    """
    best_name: str | None = None
    best_until: dt.date | None = None
    for entry in ((payload or {}).get("formerNames") or []):
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "").strip()
        raw = str(entry.get("to") or "")[:10]
        if not name or not raw:
            continue
        try:
            until = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        if best_until is None or until > best_until:
            best_name, best_until = name, until
    return best_name, best_until


def canonical_of(tickers: Iterable[str]) -> str:
    """The one ticker a CIK's rows are stored under, given several candidates.

    Shortest first, and a symbol carrying `.` or `-` loses to one that does
    not: those punctuate a share class (`RDI.B`, `BRK-A`), so the bare symbol
    is the common stock and the natural home for the filer's balance sheet.
    Alphabetical breaks the remaining ties so the choice is stable across runs
    rather than dependent on dict ordering -- a canonical ticker that moves
    between ingests would scatter one company's history across two symbols.
    """
    return min(tickers, key=lambda t: (("." in t or "-" in t), len(t), t))


async def _cik_to_ticker() -> dict[str, str]:
    """{cik (leading zeros stripped) -> the ONE ticker its rows are stored under}.

    Seeded from OUR OWN `sector_map`, with SEC's `company_tickers.json` filling
    only the CIKs that table does not cover. That order is the fix for two
    separate failures, both of which silently discarded real companies:

    1. **SEC's file is not a complete list of filers.** Fetched 11 September
       2026 it held 10,407 entries and did not contain AVB (AvalonBay, an S&P
       500 REIT that files 10-Qs on schedule), WBS, SE, RMAX or LBRDK. A CIK it
       does not list could not be resolved, so every fact for those companies
       was dropped at ingest. `sector_map` has all of them, with CIKs --
       the database already held the answer this function was throwing away.

    2. **`setdefault` collapsed each CIK to whichever ticker SEC listed first**,
       and that winner was frequently a symbol we do not even cover: HLX lost to
       HOS, AREN to PAAI, BTOG to SGRX, NRDE to SNFI, FCCI to EAIQ. So the
       ingest wrote rows for tickers no page reads while the covered ticker
       showed an empty page. Seeding from `sector_map` makes OUR ticker win by
       construction, because it is the only candidate considered.

    Where `sector_map` itself carries several tickers for one CIK -- 1,476 of
    its 8,056 CIKs do, every dual class, preferred and SPAC unit -- one is
    chosen by `canonical_of` and the others are resolved to it AT READ TIME by
    `company.lookup.canonical_ticker`. Rows are deliberately NOT written under
    every sibling: `ticker` is what the whole codebase counts by, so a company
    with two share classes would be counted twice by `identity_failures`,
    `universe_check`, the stat bar and the peer list, and a pass rate would
    quietly become share-class-weighted.
    """
    out: dict[str, str] = {}

    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import SectorMap

    by_cik: dict[str, list[str]] = {}
    try:
        with session_scope() as session:
            for ticker, cik in session.execute(
                select(SectorMap.ticker, SectorMap.cik).where(SectorMap.cik.isnot(None))
            ).all():
                key = str(cik or "").lstrip("0")
                if key and ticker:
                    by_cik.setdefault(key, []).append(str(ticker).upper())
    except Exception as exc:  # noqa: BLE001 - fall back to SEC rather than fail
        log.warning("cik_map_sector_map_unavailable", error=str(exc)[:200])

    for cik, tickers in by_cik.items():
        out[cik] = canonical_of(tickers)

    reference = await sec_edgar.fetch_company_tickers()
    added = 0
    for r in reference:
        cik = str(r.get("cik") or "").lstrip("0")
        tkr = str(r.get("ticker") or "").upper()
        if cik and tkr and cik not in out:
            out[cik] = tkr
            added += 1

    log.info(
        "cik_map_built",
        from_sector_map=len(by_cik),
        from_sec_file=added,
        total=len(out),
    )
    return out


def _collapse_earliest_filing(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One fundamental row per (ticker, metric, period_end, source), earliest
    filing_date winning -- as first reported, never a later restatement."""
    ordered = sorted(
        rows,
        key=lambda r: (r.get("filing_date") is None, r.get("filing_date") or dt.date.max),
    )
    kept, _ = repository.dedupe_on(
        ordered, ["ticker", "metric", "period_end", "source"]
    )
    return kept


def _collapse_earnings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One event per (ticker, report_date). report_date IS the filing date, so the
    tie is between same-day filings: prefer the one with a real EPS, then the
    latest fiscal period (the actual event, not an amendment of an older one)."""
    ordered = sorted(
        rows,
        key=lambda r: (
            r.get("actual_eps") is None,
            -(r.get("period_end") or dt.date.min).toordinal(),
        ),
    )
    kept, _ = repository.dedupe_on(ordered, ["ticker", "report_date"])
    return kept


async def backfill_fundamentals(
    as_of: dt.date | None = None, quarters: int | None = None
) -> int:
    """As-reported fundamentals via the SEC bulk Financial Statement Data Sets.

    ONE ZIP per quarter (num.txt + sub.txt) instead of ~17 XBRL concept calls per
    company (~85k requests). Loads the last `quarters` quarters, joins facts to
    filings for the honest (period_end, filing_date) pair, and saves them. Returns
    the number of fundamental rows written.
    """
    s = get_settings()
    as_of = as_of or dt.date.today()
    quarters = quarters or s.sec_dataset_quarters
    cik_map = await _cik_to_ticker()
    qs = sec_datasets.recent_quarters(as_of, quarters)
    _update_state(
        phase="running", source="sec_datasets", unit="quarters",
        units_total=len(qs), units_done=0, rows=0, last_error=None,
    )
    log.info("backfill_fundamentals_start", quarters=qs, companies=len(cik_map))

    total = 0
    loaded_quarters = 0
    # Which quarters' ZIPs actually parsed. The scheduler reads this to know
    # whether the authoritative load for the target quarter has happened --
    # the fundamentals table alone can no longer answer that, since the frames
    # sweep writes the same rows from a different endpoint.
    quarters_loaded: list[str] = []
    unpublished: list[str] = []
    for i, (year, q) in enumerate(qs):
        try:
            zbytes = await sec_datasets.download_dataset(year, q)
            sub, num = sec_datasets.parse_dataset(zbytes)
            rows, report = xbrl.extract_and_validate(sub, num, cik_map)
        except xbrl.ExtractionError as exc:
            # The parser is wrong, not the quarter. Writing the surviving rows
            # would leave the table half-right, which is worse than empty.
            log.error("sec_dataset_extraction_invalid", year=year, quarter=q, error=str(exc))
            _update_state(phase="error", last_error=f"{year}q{q}: {str(exc)[:300]}")
            raise
        except sec_cache.SECNotPublished:
            # The newest requested quarter is routinely a few weeks out. Not an
            # error, and not something `last_error` should be left holding.
            log.info("sec_dataset_not_published", year=year, quarter=q)
            unpublished.append(f"{year}q{q}")
            continue
        except sec_cache.SECThrottled as exc:
            # Every remaining quarter would hit the same throttle and spend four
            # more retries doing it. Stop; what loaded already is upserted, and
            # cached quarters make the re-run cheap.
            log.error("sec_dataset_throttled_stop", year=year, quarter=q)
            _update_state(phase="error", last_error=str(exc)[:300])
            break
        except Exception as exc:  # noqa: BLE001 - one bad quarter is survivable
            log.warning("sec_dataset_quarter_failed", year=year, quarter=q, error=str(exc)[:200])
            _update_state(last_error=f"{year}q{q}: {str(exc)[:200]}")
            continue
        loaded_quarters += 1
        quarters_loaded.append(f"{year}q{q}")
        _LAST_EXTRACTION_REPORTS[f"{year}q{q}"] = report.as_dict()
        # Collapse across the WHOLE quarter BEFORE batching. save_fundamentals
        # dedupes too, but only within the batch it is handed -- duplicates split
        # across a 5000-row boundary would survive as two rows, and the later
        # batch (carrying the restatement) would win the upsert. Earliest
        # filing_date wins: what was actually known at the time.
        raw = len(rows)
        rows = _collapse_earliest_filing(rows)
        log.info(
            "sec_quarter_deduped", year=year, quarter=q, raw=raw, kept=len(rows),
            dropped=raw - len(rows),
            dropped_pct=round(100 * (raw - len(rows)) / max(1, raw), 1),
        )
        for j in range(0, len(rows), 5000):
            with session_scope() as session:
                total += repository.save_fundamentals(session, rows[j : j + 5000])
        _update_state(units_done=i + 1, rows=total)
        log.info("backfill_fundamentals_progress", year=year, quarter=q, rows=total)

    if loaded_quarters == 0:
        _update_state(phase="error", quarters_loaded=[],
                      last_error=_BACKFILL_STATE.get("last_error")
                      or "no SEC dataset quarter could be downloaded")
    else:
        _update_state(phase="done", quarters_loaded=quarters_loaded)
    log.info("backfill_fundamentals_complete", rows=total,
             quarters_loaded=loaded_quarters, unpublished=unpublished)
    return total


async def backfill_earnings(
    as_of: dt.date | None = None, quarters: int | None = None
) -> int:
    """Populate EarningsEvent from the SEC bulk datasets: one event per periodic
    filing (10-Q/10-K), report_date = filing date, period_end = period, actual_eps
    from the diluted-EPS fact. This is what fills the previously-empty table and
    makes pead_window computable. consensus/surprise stay None (paid feed only);
    earnings_gap (price gap) is a follow-up bars-join. Returns rows written."""
    s = get_settings()
    as_of = as_of or dt.date.today()
    quarters = quarters or s.sec_dataset_quarters
    cik_map = await _cik_to_ticker()
    qs = sec_datasets.recent_quarters(as_of, quarters)
    _update_state(
        phase="running", source="sec_earnings", unit="quarters",
        units_total=len(qs), units_done=0, rows=0, last_error=None,
    )
    log.info("backfill_earnings_start", quarters=qs)

    events: list[dict[str, Any]] = []
    loaded_quarters = 0
    quarters_loaded: list[str] = []
    for i, (year, q) in enumerate(qs):
        try:
            zbytes = await sec_datasets.download_dataset(year, q)
            sub, num = sec_datasets.parse_dataset(zbytes)
            events.extend(sec_datasets.extract_earnings(sub, num, cik_map))
        except Exception as exc:  # noqa: BLE001
            log.warning("sec_dataset_quarter_failed", year=year, quarter=q, error=str(exc)[:200])
            _update_state(last_error=f"{year}q{q}: {str(exc)[:200]}")
            continue
        loaded_quarters += 1
        quarters_loaded.append(f"{year}q{q}")
        _update_state(units_done=i + 1)

    # Same cross-batch hazard as fundamentals, plus one more: a filing can appear
    # in more than one quarter's dataset, so collapse the accumulated events over
    # ALL quarters before batching.
    raw = len(events)
    events = _collapse_earnings(events)
    log.info(
        "sec_earnings_deduped", raw=raw, kept=len(events), dropped=raw - len(events),
        dropped_pct=round(100 * (raw - len(events)) / max(1, raw), 1),
    )
    total = 0
    for j in range(0, len(events), 5000):
        with session_scope() as session:
            total += repository.save_earnings(session, events[j : j + 5000])
    if loaded_quarters == 0:
        _update_state(phase="error", quarters_loaded=[],
                      last_error=_BACKFILL_STATE.get("last_error")
                      or "no SEC dataset quarter could be downloaded")
    else:
        _update_state(phase="done", rows=total, quarters_loaded=quarters_loaded)
    log.info("backfill_earnings_complete", rows=total, quarters_loaded=loaded_quarters)
    return total


async def backfill_filings(
    as_of: dt.date | None = None, quarters: int | None = None
) -> int:
    """Newly-filed numbers, from SEC's XBRL frames -- the CURRENT quarter's
    filings, months before the bulk dataset for them exists.

    The bulk datasets (`backfill_fundamentals`) publish once, weeks after a
    quarter closes, so a 10-Q filed in August is invisible here until November.
    This path asks a different SEC endpoint the same question: one request per
    concept returns that concept for every filer in a period, and the accession
    number on each fact is joined to EDGAR's form index for the filing date.
    See src/ingest/sec_frames.py for why the join, not a guess, supplies the
    date.

    Writes the same metrics under the same names and the same `source="sec"` as
    the bulk path, so a fact loaded by either route lands on one natural key.
    Returns the number of fundamental rows written.
    """
    s = get_settings()
    as_of = as_of or dt.date.today()
    quarters = quarters or s.sec_frames_quarters
    qs = sec_frames.frame_quarters(as_of, quarters)
    _update_state(
        phase="running", source="sec_frames", unit="quarters",
        units_total=len(qs), units_done=0, rows=0, last_error=None,
    )
    log.info("backfill_filings_start", quarters=qs)

    cik_map = await _cik_to_ticker()

    # Filing dates for a period land in the FOLLOWING calendar quarter, so the
    # index has to cover the quarter in progress as well as the one before it.
    # Anything older than that is what the bulk dataset is for.
    index: dict[str, dict[str, Any]] = {}
    current_q = (as_of.month - 1) // 3 + 1
    index_quarters = [(as_of.year, current_q)]
    prev_q, prev_y = current_q - 1, as_of.year
    if prev_q == 0:
        prev_q, prev_y = 4, as_of.year - 1
    index_quarters.append((prev_y, prev_q))
    for year, q in index_quarters:
        try:
            index.update(await sec_frames.fetch_form_index(year, q))
        except sec_frames.SECUnavailable as exc:
            # Without any index there are no honest filing dates, so this is
            # fatal for the run rather than something to work around.
            _update_state(phase="error", last_error=str(exc)[:300])
            log.error("form_index_failed", year=year, quarter=q, error=str(exc)[:200])
            return 0
    log.info("form_index_loaded", filings=len(index), quarters=index_quarters)
    if not index:
        _update_state(phase="error", last_error="EDGAR form index came back empty")
        return 0

    total = 0
    reports: list[dict[str, Any]] = []
    for i, (year, q) in enumerate(qs):
        try:
            rows, report = await sec_frames.sweep_quarter(year, q, index, cik_map)
        except sec_frames.SECUnavailable as exc:
            _update_state(phase="error", last_error=str(exc)[:300])
            log.error("frames_sweep_failed", year=year, quarter=q, error=str(exc)[:200])
            return total
        reports.append(report)
        log.info("frames_quarter_swept", **report)
        for j in range(0, len(rows), 5000):
            with session_scope() as session:
                total += repository.save_fundamentals(session, rows[j : j + 5000])
        events = sec_frames.earnings_from_rows(rows)
        for j in range(0, len(events), 5000):
            with session_scope() as session:
                repository.save_earnings(session, events[j : j + 5000])
        _update_state(units_done=i + 1, rows=total)

    _FRAMES_REPORTS.clear()
    _FRAMES_REPORTS.extend(reports)
    if total == 0 and all(r["rows_kept"] == 0 for r in reports):
        _update_state(
            phase="error",
            last_error="frames returned no datable rows for any quarter",
        )
    else:
        _update_state(phase="done", rows=total)
    log.info("backfill_filings_complete", rows=total, quarters=len(reports))
    return total


def get_frames_reports() -> list[dict[str, Any]]:
    """What the last filing sweep saw, kept and dropped -- for /admin."""
    return [dict(r) for r in _FRAMES_REPORTS]



def main() -> None:
    parser = argparse.ArgumentParser(description="Historical backfill")
    parser.add_argument(
        "--days", type=int, default=600,
        help="trading sessions of price history to fetch (default 600)",
    )
    parser.add_argument(
        "--earnings", action="store_true",
        help="populate EarningsEvent from the SEC bulk datasets",
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
    parser.add_argument(
        "--former-names", action="store_true",
        help="record each drawable company's previous SEC name, so a page "
             "renamed out from under its filings still shows the old one",
    )
    parser.add_argument(
        "--repair-names", action="store_true",
        help="name drawable companies company_tickers.json does not list "
             "(SEC submissions API, one request per company)",
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
            await backfill_fundamentals()
        if args.earnings:
            await backfill_earnings()
        if args.repair_names:
            report = await backfill_missing_company_names(limit=args.limit)
            log.info("company_names_repaired", **{
                k: v for k, v in report.items() if k not in ("no_cik", "unnamed")
            })
        if args.former_names:
            log.info("former_names_recorded",
                     **await backfill_former_names(limit=args.limit))

    asyncio.run(run())


if __name__ == "__main__":
    main()
