"""Stooq: free bulk end-of-day data. No key, no rate limit.

Stooq publishes the entire US market's daily history as a single ZIP archive.
That is the right keyless *wide-end* source: one HTTP download instead of one
call per day (Polygon grouped-daily) or -- worse -- one call per ticker (the
Yahoo per-ticker loop, which is unreliable from datacenter IPs). Fetch once,
parse locally, load. Polygon grouped-daily stays the daily incremental when a
key is present.

The archive holds one text file per ticker (e.g. `aapl.us.txt`) in the Stooq
bundle format:

    <TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>
    AAPL.US,D,20240102,000000,185.5,186.2,183.9,185.10,50000000,0

The parser below also accepts the plainer `Date,Open,High,Low,Close,Volume`
form Stooq serves elsewhere. Everything is pure/streaming so it is testable
without the network, and no data is ever fabricated: a malformed row is skipped,
never guessed.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import structlog

from src.config.settings import get_settings

log = structlog.get_logger(__name__)

# US daily, plain-text bundle. Configurable via settings.stooq_bulk_url.
DEFAULT_BULK_URL = "https://stooq.com/db/h/d_us_txt.zip"


def ticker_from_filename(name: str) -> str | None:
    """`.../aapl.us.txt` -> `AAPL`; None for anything that isn't a US .txt.

    Class-share dashes are preserved (`brk-b.us.txt` -> `BRK-B`), matching the
    SEC universe seed's convention.
    """
    base = name.rsplit("/", 1)[-1].strip().lower()
    if not base.endswith(".us.txt"):
        return None
    stem = base[: -len(".us.txt")]
    return stem.upper() or None


def _to_date(raw: str) -> dt.date | None:
    raw = raw.strip()
    try:
        if len(raw) == 8 and raw.isdigit():  # YYYYMMDD
            return dt.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        return dt.date.fromisoformat(raw[:10])  # YYYY-MM-DD
    except (ValueError, IndexError):
        return None


def _to_float(raw: str) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # drop NaN


def parse_stooq_txt(
    text: str, ticker: str, since: dt.date | None = None
) -> list[dict[str, Any]]:
    """Parse one Stooq ticker file into DailyBar rows. Pure, offline-testable.

    Maps columns by the header (bundle `<...>` names or `Date,Open,...`). Rows
    without a usable date or close are dropped, not guessed.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    header = [h.strip().strip("<>").lower() for h in lines[0].split(",")]
    idx = {name: i for i, name in enumerate(header)}
    date_i = idx.get("date")
    if date_i is None:
        return []  # no recognizable header -> refuse rather than guess positions

    def col(row: list[str], name: str) -> str | None:
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    out: list[dict[str, Any]] = []
    for ln in lines[1:]:
        row = ln.split(",")
        if len(row) <= date_i:
            continue
        d = _to_date(row[date_i])
        if d is None or (since is not None and d < since):
            continue
        close = _to_float(col(row, "close") or "")
        if close is None:
            continue
        out.append(
            {
                "ticker": ticker,
                "date": d,
                "open": _to_float(col(row, "open") or ""),
                "high": _to_float(col(row, "high") or ""),
                "low": _to_float(col(row, "low") or ""),
                "close": close,
                "volume": _to_float(col(row, "vol") or col(row, "volume") or ""),
                "vwap": None,
                "transactions": None,
            }
        )
    return out


def iter_zip_bars(
    zip_path: str | Path,
    *,
    since: dt.date | None = None,
    max_tickers: int | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """Yield one ticker's rows at a time from a Stooq bundle ZIP on disk.

    Streams entry-by-entry so the whole archive is never held in memory.
    """
    seen = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            ticker = ticker_from_filename(info.filename)
            if ticker is None:
                continue
            if max_tickers is not None and seen >= max_tickers:
                break
            try:
                raw = zf.read(info).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 - one bad entry is survivable
                log.debug("stooq_entry_unreadable", name=info.filename, error=str(exc))
                continue
            rows = parse_stooq_txt(raw, ticker, since=since)
            seen += 1
            if rows:
                yield rows


def load_bulk_from_zip(
    zip_path: str | Path,
    save_batch: Callable[[list[dict[str, Any]]], int],
    *,
    since: dt.date | None = None,
    max_tickers: int | None = None,
    batch_size: int = 20_000,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Parse a Stooq bundle ZIP and persist it via `save_batch`, in row batches.

    Returns the total rows saved. Batching keeps memory flat over a
    whole-market archive. `on_progress(tickers_done, rows_saved)` is called
    periodically so the backfill diagnostics can climb.
    """
    buf: list[dict[str, Any]] = []
    total = 0
    tickers_done = 0
    for rows in iter_zip_bars(zip_path, since=since, max_tickers=max_tickers):
        buf.extend(rows)
        tickers_done += 1
        if len(buf) >= batch_size:
            total += save_batch(buf)
            buf = []
            if on_progress:
                on_progress(tickers_done, total)
    if buf:
        total += save_batch(buf)
    if on_progress:
        on_progress(tickers_done, total)
    log.info("stooq_bulk_loaded", tickers=tickers_done, rows=total)
    return total


async def download_bulk(url: str | None = None, *, timeout: float = 600.0) -> Path:
    """Stream the bulk ZIP to a temp file and return its path. One request."""
    url = url or get_settings().stooq_bulk_url or DEFAULT_BULK_URL
    tmp = Path(tempfile.mkstemp(prefix="stooq_", suffix=".zip")[1])
    log.info("stooq_download_start", url=url)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"Stooq bulk download {resp.status_code} for {url}")
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 20):
                    fh.write(chunk)
    size = tmp.stat().st_size
    log.info("stooq_download_done", bytes=size)
    if size < 1024 or not zipfile.is_zipfile(tmp):
        # Stooq rate-throttles with a tiny HTML page; treat that as a hard fail,
        # never as "no data".
        raise RuntimeError(
            f"Stooq bulk download was not a valid ZIP ({size} bytes) -- "
            f"throttled or the URL changed."
        )
    return tmp
