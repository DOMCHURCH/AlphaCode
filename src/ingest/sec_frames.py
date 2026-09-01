"""Filings as they are filed, rather than a quarter after the fact.

The bulk Financial Statement Data Sets (src/ingest/sec_datasets.py) are the
authoritative load, but they publish ONCE, weeks after a quarter closes. A
company that filed its June-quarter 10-Q in August does not appear in any
dataset until the following November. For a site whose whole subject is filed
balance sheets, months of invisible filings is the wrong answer.

SEC's XBRL **frames** API closes that gap, and closes it cheaply. One request:

    /api/xbrl/frames/us-gaap/Assets/USD/CY2026Q2I.json

returns that concept for EVERY filer that reported it in that period -- five
thousand companies, one call. So the entire market's newest quarter costs one
request per concept, not one per company. The per-company alternative
(companyfacts, ~3MB each) would be gigabytes a day to say the same thing.

What frames do not carry is a filing date, and this project cannot store a
fundamental without one: `filing_date` is what the point-in-time layer reads,
and inventing it would manufacture exactly the lookahead bias that layer
exists to prevent. Frames do carry `accn`, the accession number -- so the
filing date comes from EDGAR's quarterly form index, which lists every filing
with the date it was filed. Joining on `accn` gives the honest pair. A row
whose accession is in no index we hold is DROPPED, never dated by guesswork.

Two known limits, stated rather than hidden:

* A frame holds one fact per company per period, chosen by SEC. Filers whose
  fiscal quarter is too far off the calendar quarter are not in the calendar
  frame at all. They are not lost -- the quarterly dataset picks them up on its
  own schedule -- they are just later here.
* Only the concepts in `xbrl.CONCEPTS` are fetched, one request per tag, so
  this path stores the same metrics under the same names as the bulk path.
  Both write `source="sec"`: they are the same facts from the same filings, so
  a row loaded by either route collapses onto one natural key rather than
  double-counting.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from src.config.settings import get_settings
from src.ingest.sec_cache import cache_dir
from src.ingest.xbrl import CONCEPTS, DURATION, INSTANT, coerce_float

log = structlog.get_logger(__name__)

FRAMES_URL = "https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{uom}/{ccp}.json"
FORM_INDEX_URL = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/form.idx"

# The periodic reports that carry a balance sheet worth drawing.
PERIODIC_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})

# SEC asks for no more than 10 requests/second. Frames are small but there are
# a few dozen of them, so they go out a handful at a time rather than all at
# once -- politeness that costs nothing, since the whole sweep is seconds.
_CONCURRENCY = 4

# The form index for a quarter changes every business day while that quarter is
# open, and never again once it closes. Six hours keeps a running service
# current without re-downloading 30MB on every tick.
_INDEX_TTL_SECONDS = 6 * 3600


class SECUnavailable(RuntimeError):
    """SEC could not be read at all -- network, 403, or a throttle."""


def _user_agent() -> str:
    ua = get_settings().sec_user_agent
    if not ua or "@" not in ua:
        raise SECUnavailable(
            "SEC_USER_AGENT must be set to 'Name your@email.com' or SEC returns 403"
        )
    return ua


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
def instant_ccp(year: int, quarter: int) -> str:
    """Frame id for a point-in-time concept: CY2026Q2I."""
    return f"CY{year}Q{quarter}I"


def duration_ccp(year: int, quarter: int) -> str:
    """Frame id for a flow concept measured over the quarter: CY2026Q2."""
    return f"CY{year}Q{quarter}"


def uom_path(uom: str) -> str:
    """`USD/shares` is spelled `USD-per-shares` in a frame URL."""
    return uom.replace("/", "-per-")


def frame_quarters(today: dt.date, back: int = 2) -> list[tuple[int, int]]:
    """The calendar quarters worth sweeping, newest first.

    Starts at the quarter that has ENDED, not the one in progress: a frame for
    a period nobody has finished reporting is empty. More than one because a
    filing lands weeks after its period closes, and amendments later still --
    so the previous quarter is still gaining rows while the newest fills up.
    """
    q = (today.month - 1) // 3 + 1
    year = today.year
    q -= 1
    if q == 0:
        q, year = 4, year - 1
    out = []
    for _ in range(back):
        out.append((year, q))
        q -= 1
        if q == 0:
            q, year = 4, year - 1
    return out


# ---------------------------------------------------------------------------
# The form index: accession -> (form, cik, filed)
# ---------------------------------------------------------------------------
def parse_form_index(text: str) -> dict[str, dict[str, Any]]:
    """EDGAR's `form.idx` -> {accession: {form, cik, filed, company}}.

    The file is fixed-width with a header, but the columns are not reliably
    aligned across years, so it is parsed from the right: the last field is the
    path (which ends in the accession), the one before it the filing date, and
    the one before that the CIK. Everything left of those is the form type and
    company name, split at the first run of two or more spaces. Only periodic
    reports are kept -- an 8-K has no balance sheet to date.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith(("-", " ")) and "edgar/data" not in line:
            continue
        if "edgar/data/" not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        path = parts[-1]
        filed_raw = parts[-2]
        cik_raw = parts[-3]
        head = line[: line.index(cik_raw + " ")] if f"{cik_raw} " in line else ""
        form = head.split("  ", 1)[0].strip() or parts[0]
        if form not in PERIODIC_FORMS:
            continue
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        filed = _parse_idx_date(filed_raw)
        if filed is None or not cik_raw.isdigit():
            continue
        out[accession] = {
            "form": form,
            "cik": int(cik_raw),
            "filed": filed,
        }
    return out


def _parse_idx_date(raw: str) -> dt.date | None:
    raw = raw.strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def _index_cache_path(year: int, quarter: int) -> Path:
    return cache_dir() / f"form-{year}q{quarter}.idx"


async def fetch_form_index(
    year: int, quarter: int, *, timeout: float = 180.0
) -> dict[str, dict[str, Any]]:
    """Every periodic filing in a quarter, by accession, from disk when fresh.

    A quarter that has closed never changes, but the open one gains filings
    daily, so the cache carries a TTL rather than being permanent.
    """
    path = _index_cache_path(year, quarter)
    try:
        if path.exists() and path.stat().st_size > 1024:
            age = time.time() - path.stat().st_mtime
            if age < _INDEX_TTL_SECONDS:
                return parse_form_index(path.read_text(encoding="latin-1"))
    except OSError:
        pass

    url = FORM_INDEX_URL.format(year=year, q=quarter)
    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise SECUnavailable(f"form index {year}q{quarter}: {exc}") from exc
    if r.status_code == 404:
        # A quarter that has not started. Not an error.
        return {}
    if r.status_code != 200:
        raise SECUnavailable(
            f"form index {year}q{quarter}: HTTP {r.status_code}"
        )
    text = r.text
    try:
        path.write_text(text, encoding="latin-1")
    except OSError as exc:  # a read-only disk must not fail the load
        log.warning("form_index_cache_write_failed", error=str(exc)[:200])
    return parse_form_index(text)


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------
async def fetch_frame(
    tag: str, uom: str, ccp: str, *, timeout: float = 60.0
) -> list[dict[str, Any]] | None:
    """One concept, one period, every filer. None when SEC has no such frame.

    A 404 is ordinary: not every tag is reported in every period, and the
    newest frame does not exist until somebody has filed into it.
    """
    url = FRAMES_URL.format(
        taxonomy="us-gaap", tag=tag, uom=uom_path(uom), ccp=ccp
    )
    headers = {"User-Agent": _user_agent(), "Accept-Encoding": "gzip, deflate"}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise SECUnavailable(f"frame {tag}/{ccp}: {exc}") from exc
    if r.status_code in (403, 429):
        raise SECUnavailable(f"frame {tag}/{ccp}: HTTP {r.status_code}")
    if r.status_code != 200:
        return None
    try:
        payload = r.json()
    except ValueError as exc:
        raise SECUnavailable(f"frame {tag}/{ccp}: not JSON") from exc
    data = payload.get("data")
    return data if isinstance(data, list) else None


def rows_from_frame(
    data: list[dict[str, Any]], metric: str, tag_rank: int = 0
) -> list[dict[str, Any]]:
    """A frame payload -> raw (cik, accn, period_end, value) rows for one metric.

    `tag_rank` is the alias's position in its concept's tag tuple. It rides
    along on every row so that a filer reporting two aliases of one concept
    can be collapsed to the preferred tag later, no matter what order the
    concurrent fetches happened to finish in.
    """
    out: list[dict[str, Any]] = []
    for r in data:
        cik = r.get("cik")
        accn = r.get("accn")
        end = r.get("end")
        value = coerce_float(r.get("val"))
        if cik is None or not accn or not end or value is None:
            continue
        period_end = _parse_idx_date(str(end))
        if period_end is None:
            continue
        out.append({
            "cik": int(cik),
            "accn": str(accn),
            "period_end": period_end,
            "metric": metric,
            "value": value,
            "tag_rank": tag_rank,
        })
    return out


def join_filing_dates(
    rows: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    cik_to_ticker: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Attach ticker + filing date, dropping anything we cannot honestly date.

    Returns the rows and a census of what was dropped and why, because "we
    stored 40,000 facts" means nothing without "and skipped 900 because their
    accession was in no index we hold".
    """
    kept: list[dict[str, Any]] = []
    dropped = {"no_accession_in_index": 0, "no_ticker": 0}
    for r in rows:
        entry = index.get(r["accn"])
        if entry is None:
            dropped["no_accession_in_index"] += 1
            continue
        ticker = cik_to_ticker.get(str(entry["cik"]))
        if not ticker:
            dropped["no_ticker"] += 1
            continue
        kept.append({
            "ticker": ticker,
            "metric": r["metric"],
            "value": r["value"],
            "period_end": r["period_end"],
            "filing_date": entry["filed"],
            "source": "sec",
            "restated": False,
            "tag_rank": r.get("tag_rank", 0),
        })
    return kept, dropped


def earnings_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One earnings event per (ticker, filing date), carrying diluted EPS.

    Built from the same joined rows rather than a second pass over EDGAR: a
    row already knows the ticker, the period it covers and the day it was
    filed, which is the whole of an earnings event here. consensus and surprise
    stay absent -- there is no free feed for them, and a fabricated zero would
    read as a company that met expectations exactly.
    """
    events: dict[tuple[str, dt.date], dict[str, Any]] = {}
    for r in rows:
        key = (r["ticker"], r["filing_date"])
        ev = events.get(key)
        if ev is None or r["period_end"] > ev["period_end"]:
            ev = {
                "ticker": r["ticker"],
                "report_date": r["filing_date"],
                "period_end": r["period_end"],
                "actual_eps": None,
            }
            events[key] = ev
        if r["metric"] == "eps_diluted" and r["period_end"] == ev["period_end"]:
            ev["actual_eps"] = r["value"]
    return list(events.values())


def collapse_alias_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per (ticker, metric, period_end, filing_date).

    A concept can be supplied by more than one XBRL tag, and a filer reporting
    two aliases would otherwise yield two rows on one natural key -- which
    aborts a Postgres upsert batch. Ties break on `tag_rank`, the alias's
    position in its concept's preference order, so the winner does not depend
    on which concurrent fetch returned first. The key is dropped from the rows
    that survive: it is a fetch detail, not a column.
    """
    seen: dict[tuple[str, str, dt.date, dt.date], dict[str, Any]] = {}
    for r in rows:
        key = (r["ticker"], r["metric"], r["period_end"], r["filing_date"])
        best = seen.get(key)
        if best is None or r.get("tag_rank", 0) < best.get("tag_rank", 0):
            seen[key] = r
    return [{k: v for k, v in r.items() if k != "tag_rank"} for r in seen.values()]


def concept_requests(
    year: int, quarter: int
) -> list[tuple[str, str, str, str, int]]:
    """(metric, tag, uom, ccp, alias rank) for one quarter -- one per XBRL tag.

    Instants take the point-in-time frame (CY2026Q2I), flows the duration one
    (CY2026Q2). The rank is the tag's index in its concept's preference tuple,
    carried through the fetch so the collapse can prefer the modern tag.
    """
    out: list[tuple[str, str, str, str, int]] = []
    for c in CONCEPTS:
        if c.kind not in (INSTANT, DURATION):
            continue
        ccp = (
            instant_ccp(year, quarter)
            if c.kind == INSTANT
            else duration_ccp(year, quarter)
        )
        for rank, tag in enumerate(c.tags):
            out.append((c.metric, tag, c.uom, ccp, rank))
    return out


async def sweep_quarter(
    year: int,
    quarter: int,
    index: dict[str, dict[str, Any]],
    cik_to_ticker: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every concept for one quarter, joined to filing dates.

    Returns (fundamental rows, a report of what happened). A single missing
    frame is not fatal -- concepts are independent, and a tag nobody reported
    this quarter is a fact about the quarter, not a failure.
    """
    requests = concept_requests(year, quarter)
    sem = asyncio.Semaphore(_CONCURRENCY)
    raw: list[dict[str, Any]] = []
    frames_found = 0
    frames_missing = 0
    errors: list[str] = []

    async def one(metric: str, tag: str, uom: str, ccp: str, rank: int) -> None:
        nonlocal frames_found, frames_missing
        async with sem:
            try:
                data = await fetch_frame(tag, uom, ccp)
            except SECUnavailable as exc:
                errors.append(str(exc)[:200])
                return
        if data is None:
            frames_missing += 1
            return
        frames_found += 1
        raw.extend(rows_from_frame(data, metric, rank))

    await asyncio.gather(*(one(*req) for req in requests))

    joined, dropped = join_filing_dates(raw, index, cik_to_ticker)
    rows = collapse_alias_rows(joined)
    report = {
        "quarter": f"{year}q{quarter}",
        "frames_requested": len(requests),
        "frames_found": frames_found,
        "frames_missing": frames_missing,
        "facts_seen": len(raw),
        "rows_kept": len(rows),
        "dropped": dropped,
        "errors": errors[:5],
    }
    return rows, report


def cached_index_status() -> list[dict[str, Any]]:
    """Which form indexes are on disk, for /admin."""
    out: list[dict[str, Any]] = []
    try:
        for p in sorted(cache_dir().glob("form-*.idx")):
            st = p.stat()
            out.append({
                "quarter": p.stem.removeprefix("form-"),
                "bytes": st.st_size,
                "age_hours": round((time.time() - st.st_mtime) / 3600, 1),
            })
    except OSError:
        pass
    return out


__all__ = [
    "SECUnavailable",
    "cached_index_status",
    "collapse_alias_rows",
    "concept_requests",
    "duration_ccp",
    "earnings_from_rows",
    "fetch_form_index",
    "fetch_frame",
    "frame_quarters",
    "instant_ccp",
    "join_filing_dates",
    "parse_form_index",
    "rows_from_frame",
    "sweep_quarter",
    "uom_path",
]