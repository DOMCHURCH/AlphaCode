"""One place that fetches SEC quarterly ZIPs, with a disk cache and a throttle.

Before this, the backfill and the raw-facts dump each downloaded the same ~100MB
file independently, with no shared cache and no rate limiting between them. Every
tap of the dump button re-fetched a file already on the machine. That is what
earned the 429s.

So:

  * ONE download path. Both callers come through here.
  * Disk cache keyed by quarter. A file already fetched is never fetched again,
    so a re-run within the same day costs nothing.
  * A floor between network fetches. SEC allows 10 requests/second, but these
    are ~100MB files and hammering them is what gets an IP throttled; the limit
    that matters here is politeness, not the documented ceiling.
  * 429 handled properly: `Retry-After` is respected when sent, otherwise
    exponential backoff, several attempts per quarter before giving up.

The cache directory is ephemeral on a container restart, which is fine -- it is
an optimisation, not storage.
"""

from __future__ import annotations

import asyncio
import io
import os
import time
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path

import httpx
import structlog

from src.config.settings import get_settings

log = structlog.get_logger(__name__)

DATASET_URL = (
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip"
)

# Minimum wall-clock gap between two network fetches, across every caller in the
# process. These are ~100MB files; back-to-back requests are what draw a 429.
MIN_SECONDS_BETWEEN_FETCHES = 5.0

MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 10.0
# A server-sent Retry-After longer than this is honoured but reported, rather
# than silently parking a background job for an hour.
MAX_HONOURED_RETRY_AFTER = 300.0

_fetch_lock = asyncio.Lock()
_last_fetch_at: float = 0.0


class SECThrottled(RuntimeError):
    """SEC refused with 429 after every retry. The caller must not proceed."""


class SECNotPublished(RuntimeError):
    """404: that quarter's dataset does not exist yet.

    Not a failure. A dataset publishes some weeks after quarter end, so the
    newest quarter we ask for is routinely absent -- asking for seven quarters
    and getting six is the normal state of the world, not a degraded one. This
    is a separate type from SECThrottled precisely so a reload can tell "SEC
    hasn't written this yet" from "SEC is refusing to talk to us", which are
    opposite instructions: skip the first, stop dead on the second.
    """


def cache_dir() -> Path:
    d = Path(os.environ.get("SEC_CACHE_DIR", ".sec-cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_path(year: int, quarter: int) -> Path:
    return cache_dir() / f"{year}q{quarter}.zip"


def cached_bytes(year: int, quarter: int) -> bytes | None:
    """The cached ZIP for a quarter, or None. Never returns a corrupt file."""
    p = cache_path(year, quarter)
    try:
        if not p.exists() or p.stat().st_size < 1024:
            return None
        data = p.read_bytes()
    except OSError:
        return None
    if not zipfile.is_zipfile(io.BytesIO(data)):
        # A truncated download is worse than none: it would parse to a partial
        # quarter and look like real data.
        log.warning("sec_cache_corrupt_discarded", year=year, quarter=quarter)
        p.unlink(missing_ok=True)
        return None
    return data


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None  # HTTP-date form; fall back to our own backoff


async def _respect_floor() -> None:
    """Hold the shared floor between network fetches."""
    global _last_fetch_at
    gap = time.monotonic() - _last_fetch_at
    if gap < MIN_SECONDS_BETWEEN_FETCHES:
        wait = MIN_SECONDS_BETWEEN_FETCHES - gap
        log.debug("sec_fetch_floor_wait", seconds=round(wait, 1))
        await asyncio.sleep(wait)
    _last_fetch_at = time.monotonic()


async def fetch_dataset(
    year: int, quarter: int, *, timeout: float = 300.0, use_cache: bool = True
) -> bytes:
    """One quarter's ZIP, from disk if we already have it.

    Raises SECThrottled on a 429 that survives every retry, and RuntimeError on
    anything else. Never returns a partial or non-ZIP body.
    """
    if use_cache:
        cached = cached_bytes(year, quarter)
        if cached is not None:
            log.info("sec_dataset_cache_hit", year=year, quarter=quarter,
                     bytes=len(cached))
            return cached

    s = get_settings()
    url = (s.sec_dataset_url or DATASET_URL).format(year=year, q=quarter)
    headers = {"User-Agent": s.sec_user_agent, "Accept-Encoding": "gzip, deflate"}

    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Serialise fetches process-wide so the dump and the backfill cannot
        # race each other into a burst.
        async with _fetch_lock:
            await _respect_floor()
            log.info("sec_dataset_download_start", url=url, attempt=attempt)
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=timeout
                ) as c:
                    resp = await c.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                resp = None

        if resp is None:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning("sec_dataset_network_error", year=year, quarter=quarter,
                        attempt=attempt, error=last_error[:200], retry_in=wait)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(
                    f"SEC dataset {year}q{quarter}: {last_error[:200]}"
                )
            await asyncio.sleep(wait)
            continue

        if resp.status_code == 429:
            server_wait = _parse_retry_after(resp.headers.get("Retry-After"))
            wait = server_wait if server_wait is not None else (
                BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            )
            honoured = min(wait, MAX_HONOURED_RETRY_AFTER)
            log.warning(
                "sec_dataset_throttled", year=year, quarter=quarter,
                attempt=attempt, retry_after_header=resp.headers.get("Retry-After"),
                waiting=honoured,
            )
            if attempt == MAX_ATTEMPTS:
                raise SECThrottled(
                    f"SEC returned 429 for {year}q{quarter} after {MAX_ATTEMPTS} "
                    f"attempts. Wait and retry; the cache means already-fetched "
                    f"quarters will not be re-downloaded."
                )
            await asyncio.sleep(honoured)
            continue

        if resp.status_code == 404:
            # Deterministic -- retrying cannot make an unpublished quarter exist.
            log.info("sec_dataset_not_published", year=year, quarter=quarter, url=url)
            raise SECNotPublished(
                f"{year}q{quarter} is not published yet (404). SEC posts a "
                f"quarter's dataset some weeks after quarter end."
            )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"SEC dataset {year}q{quarter}: HTTP {resp.status_code} for {url}"
            )

        data = resp.content
        if len(data) < 1024 or not zipfile.is_zipfile(io.BytesIO(data)):
            raise RuntimeError(
                f"SEC dataset {year}q{quarter}: not a valid ZIP ({len(data)} "
                f"bytes) -- moved URL, throttle, or an HTML error page."
            )

        try:
            cache_path(year, quarter).write_bytes(data)
        except OSError as exc:  # a full disk must not fail the load
            log.warning("sec_cache_write_failed", error=str(exc)[:200])
        log.info("sec_dataset_download_done", bytes=len(data), year=year,
                 quarter=quarter)
        return data

    raise RuntimeError(f"SEC dataset {year}q{quarter}: exhausted retries")


async def prefetch_quarters(
    quarters: Sequence[tuple[int, int]],
    *,
    timeout: float = 300.0,
    on_progress: Callable[[list[dict[str, object]]], None] | None = None,
) -> list[dict[str, object]]:
    """Put every PUBLISHED quarter's ZIP on disk.

    This exists so a caller that is about to destroy data can find out FIRST
    whether the replacement is obtainable. A reload that wipes and then
    discovers SEC is returning 429 has already lost the data it was replacing.

    "Obtainable" is not the same as "exists". A 404 means SEC has not published
    that quarter yet -- routinely true of the newest one we ask for -- so it is
    recorded as `source: "unpublished"` and skipped. Every other failure, 429
    included, raises and nothing downstream runs.

    Returns one entry per requested quarter, in order, each carrying `year`/`q`
    so the caller can load exactly the quarters that landed.
    """
    staged: list[dict[str, object]] = []
    for year, q in quarters:
        entry: dict[str, object] = {"quarter": f"{year}q{q}", "year": year, "q": q}
        # cached_bytes validates the ZIP, so a truncated file on disk does not
        # count as obtainable -- it is discarded and re-fetched here, not at
        # parse time when the table is already empty.
        cached = cached_bytes(year, q)
        if cached is not None:
            staged.append({**entry, "bytes": len(cached), "source": "cache"})
        else:
            try:
                data = await fetch_dataset(year, q, timeout=timeout, use_cache=False)
            except SECNotPublished:
                staged.append({**entry, "bytes": 0, "source": "unpublished"})
                if on_progress is not None:
                    on_progress(list(staged))
                continue
            staged.append({**entry, "bytes": len(data), "source": "network"})
        if on_progress is not None:
            on_progress(list(staged))
    log.info(
        "sec_prefetch_complete", requested=len(staged),
        available=sum(1 for s in staged if s["source"] != "unpublished"),
        unpublished=[s["quarter"] for s in staged if s["source"] == "unpublished"],
    )
    return staged


def available_quarters(staged: Sequence[dict[str, object]]) -> list[tuple[int, int]]:
    """The (year, quarter) pairs that actually landed on disk."""
    return [
        (int(s["year"]), int(s["q"]))
        for s in staged
        if s["source"] != "unpublished"
    ]


def cache_status() -> list[dict[str, object]]:
    """What is on disk, for /admin."""
    out = []
    try:
        for f in sorted(cache_dir().glob("*.zip")):
            st = f.stat()
            out.append({
                "quarter": f.stem,
                "bytes": st.st_size,
                "age_hours": round((time.time() - st.st_mtime) / 3600, 1),
            })
    except OSError:
        pass
    return out
