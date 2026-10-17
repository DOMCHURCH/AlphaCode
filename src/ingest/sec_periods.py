"""The real period end of a filing, looked up by accession number.

SEC's bulk datasets round `ddate` to the month end (see src/company/periods.py).
The XBRL frames API carries the real date next to the accession number (`accn`),
so one Assets frame per calendar quarter is a complete accession -> real date
table for every filer in it, in a handful of requests. The bulk loader asks this
table first and falls back to the rounded date ONLY when an accession is absent,
which is what stops the next bulk load from re-creating the duplicates.

A looked-up date is accepted only if it is within WINDOW_DAYS of the rounded one.
Anything further away is a different fact, not the same quarter's real date, and
is ignored rather than trusted.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

import structlog

from src.company.periods import WINDOW_DAYS

log = structlog.get_logger(__name__)

Fetch = Callable[[str, str, str], Awaitable[list[dict[str, Any]] | None]]


def pick_real_period(rounded: dt.date, real: dt.date | None) -> dt.date:
    """The real date when it is a believable un-rounding of `rounded`, else `rounded`."""
    if real is not None and abs((real - rounded).days) <= WINDOW_DAYS:
        return real
    return rounded


def real_periods_from_frame(data: Iterable[Mapping[str, Any]]) -> dict[str, dt.date]:
    """{accession: real period end} from one frame payload."""
    out: dict[str, dt.date] = {}
    for r in data:
        accn, end = r.get("accn"), r.get("end")
        if not accn or not end:
            continue
        try:
            out[str(accn)] = dt.date.fromisoformat(str(end)[:10])
        except ValueError:
            continue
    return out


def quarters_for(year: int, quarter: int) -> list[tuple[int, int]]:
    """The calendar quarters whose instant frame can hold a filing of dataset (year, quarter):
    the quarter itself and the one before it (a 10-Q is filed after its period closes)."""
    prev = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
    return [(year, quarter), prev]


async def real_periods_for(year: int, quarter: int, fetch: Fetch | None = None) -> dict[str, dt.date]:
    """Accession -> real period end for dataset (year, quarter). Never raises: on any
    failure the loader simply keeps the rounded dates (and says so in the log)."""
    if fetch is None:
        from src.ingest.sec_frames import fetch_frame

        fetch = fetch_frame
    out: dict[str, dt.date] = {}
    for y, q in quarters_for(year, quarter):
        try:
            data = await fetch("Assets", "USD", f"CY{y}Q{q}I")
        except Exception as exc:  # network, rate limit, bad JSON: not fatal
            log.warning("real_periods_unavailable", year=y, quarter=q, error=str(exc)[:120])
            continue
        if data:
            out.update(real_periods_from_frame(data))
    log.info("real_periods_loaded", year=year, quarter=quarter, accessions=len(out))
    return out


def real_periods_for_sync(year: int, quarter: int, fetch: Fetch | None = None) -> dict[str, dt.date]:
    """Same, for the synchronous reload path (works whether or not a loop is already running)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(real_periods_for(year, quarter, fetch))
    box: dict[str, dict[str, dt.date]] = {}
    t = threading.Thread(target=lambda: box.update(r=asyncio.run(real_periods_for(year, quarter, fetch))))
    t.start()
    t.join()
    return box.get("r", {})
