"""The full dataset, as a CSV stream.

This is the paid artefact: every as-reported fact in the `fundamentals` table,
which is what "1.24M cleaned facts" refers to. There is no separate "cleaned"
table -- the consolidated-row filtering that fixed the duplicate-tag problem
(one company reporting Total Assets twenty-three times in a single filing, once
per segment and subsidiary) happens at ingest, in `src.ingest.xbrl`. What
reaches this table is already the resolved figure, one row per company / metric
/ period.

The whole file is generated row by row and never assembled in memory. At a
million-plus rows a `.all()` here would materialise several hundred megabytes of
ORM objects and take the container's memory with it, so the query is a Core
select over columns with `yield_per`, which is a real server-side cursor on
Postgres and a chunked fetch on SQLite.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import time
from collections.abc import Iterator
from typing import NamedTuple

import structlog
from sqlalchemy import select

from src.storage.db import session_scope
from src.storage.models import Fundamental

log = structlog.get_logger(__name__)

# Rows fetched per round trip. Large enough that the per-batch overhead
# disappears, small enough that one batch is a few megabytes, not a few hundred.
_CHUNK = 5_000

COLUMNS = (
    "ticker",
    "metric",
    "value",
    "period_end",
    "fiscal_period",
    "filing_date",
    "source",
    "restated",
    "ingested_at",
)


def filename(today: dt.date | None = None) -> str:
    return f"to-scale-facts-{(today or dt.date.today()).isoformat()}.csv"


def _fmt(v: object) -> object:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return "" if v is None else v


def iter_csv() -> Iterator[str]:
    """Yield the dataset as CSV text, header first.

    The generator opens its OWN session and holds it for the life of the
    stream. It has to: this runs after the endpoint has returned, while
    Starlette pulls chunks, so a session opened by the route would already be
    closed by the time the first row was needed.

    Ordering is (ticker, metric, period_end) so the file is diffable between
    downloads -- an unordered dump of the same data reshuffles every time and
    cannot be compared against last month's copy.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    def drain() -> str:
        out = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return out

    writer.writerow(COLUMNS)
    yield drain()

    stmt = (
        select(
            Fundamental.ticker,
            Fundamental.metric,
            Fundamental.value,
            Fundamental.period_end,
            Fundamental.fiscal_period,
            Fundamental.filing_date,
            Fundamental.source,
            Fundamental.restated,
            Fundamental.ingested_at,
        )
        .order_by(Fundamental.ticker, Fundamental.metric, Fundamental.period_end)
        .execution_options(stream_results=True, yield_per=_CHUNK)
    )

    rows = 0
    with session_scope() as session:
        for chunk in session.execute(stmt).partitions(_CHUNK):
            for row in chunk:
                writer.writerow([_fmt(v) for v in row])
            rows += len(chunk)
            yield drain()
    log.info("dataset_streamed", rows=rows)


_COUNT_TTL_S = 900.0
_SHAPE_TTL_S = 900.0
_count_cache: tuple[float, int] | None = None


def row_count() -> int:
    """How many facts the download contains. Shown on the dashboard so a buyer
    knows what they are paying for before they pay for it.

    Memoised for fifteen minutes. `count(*)` over a million-plus rows is a
    sequential scan on Postgres, and this figure is decoration on a page that
    should feel instant -- it changes four times a year, when a quarter loads.
    A stale count for a quarter of an hour is invisible; a page that waits half
    a second for it on every view is not.
    """
    global _count_cache
    from sqlalchemy import func

    now = time.monotonic()
    if _count_cache is not None and now - _count_cache[0] < _COUNT_TTL_S:
        return _count_cache[1]

    with session_scope() as session:
        n = int(
            session.execute(
                select(func.count()).select_from(Fundamental)
            ).scalar_one()
        )
    _count_cache = (now, n)
    return n


def reset_count_cache() -> None:
    """Test helper: forget the memoised count so a new database is counted."""
    global _count_cache, _shape_cache
    _count_cache = None
    _shape_cache = None


class Shape(NamedTuple):
    """What the download contains, for the page that sells it.

    Three numbers a buyer asks before paying and could not previously get:
    how many rows, roughly how big the file is, and -- the one that matters
    most for a STATIC product -- how current the data in it is.
    """

    rows: int
    bytes_estimate: int
    generated: dt.datetime | None
    newest_filing: dt.date | None

    @property
    def size_label(self) -> str:
        """"~184 MB". Prefixed with a tilde everywhere it is shown, because it
        is extrapolated and must not read as a byte count."""
        n = float(self.bytes_estimate)
        for unit in ("bytes", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"


_shape_cache: tuple[float, Shape] | None = None


def _sample_bytes_per_row(session: object, sample: int = 500) -> float:
    """Mean encoded CSV bytes per row, measured on a real sample.

    The file is never assembled -- `iter_csv` streams it and nothing on disk
    has a size to stat -- so an exact figure does not exist to be read. It is
    written through the same `csv.writer` with the same `_fmt` the download
    uses, so the estimate is the real encoding rather than a guess at one, and
    only the extrapolation is approximate.
    """
    stmt = (
        select(
            Fundamental.ticker,
            Fundamental.metric,
            Fundamental.value,
            Fundamental.period_end,
            Fundamental.fiscal_period,
            Fundamental.filing_date,
            Fundamental.source,
            Fundamental.restated,
            Fundamental.ingested_at,
        )
        .order_by(Fundamental.ticker, Fundamental.metric, Fundamental.period_end)
        .limit(sample)
    )
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    seen = 0
    for row in session.execute(stmt):  # type: ignore[attr-defined]
        writer.writerow([_fmt(v) for v in row])
        seen += 1
    if not seen:
        return 0.0
    return len(buf.getvalue().encode("utf-8")) / seen


def shape() -> Shape:
    """Rows, size and freshness in one query pass, memoised like `row_count`.

    Same fifteen minutes and the same reasoning: these change when a quarter
    loads, four times a year, and they decorate a page that should feel
    instant.
    """
    global _shape_cache
    from sqlalchemy import func

    now = time.monotonic()
    if _shape_cache is not None and now - _shape_cache[0] < _SHAPE_TTL_S:
        return _shape_cache[1]

    with session_scope() as session:
        rows = int(
            session.execute(
                select(func.count()).select_from(Fundamental)
            ).scalar_one()
        )
        generated = session.execute(
            select(func.max(Fundamental.ingested_at))
        ).scalar_one()
        newest = session.execute(
            select(func.max(Fundamental.filing_date))
        ).scalar_one()
        per_row = _sample_bytes_per_row(session) if rows else 0.0

    header = len(",".join(COLUMNS).encode("utf-8")) + 1
    built = Shape(
        rows=rows,
        bytes_estimate=int(header + per_row * rows),
        generated=generated,
        newest_filing=newest,
    )
    _shape_cache = (now, built)
    return built
