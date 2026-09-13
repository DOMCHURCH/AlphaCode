"""The full dataset, as a CSV stream.

ONE ROW PER (ticker, metric, period_end). That is what the module has always
claimed and, until the dedup below, not what it did -- it dumped the table,
so a company that restated a figure appeared twice with two different values
and nothing said which one the site drew. The buyer got a file that disagreed
with the API they could have bought instead.

Resolution is the accounting identity plus the latest-filing rule, the same
policy `storage.pit` and `company.balancesheet` apply: latest filing wins,
source preference breaks a same-day tie, and the figures are reconciled
against A = L + E at ingest. `restated` and `source` ride along on every row
so the choice is auditable rather than merely asserted.


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
from typing import Any, NamedTuple

import structlog
from sqlalchemy import select

from src.storage.db import session_scope
from src.storage.models import Fundamental

log = structlog.get_logger(__name__)

# Rows fetched per round trip. Large enough that the per-batch overhead
# disappears, small enough that one batch is a few megabytes, not a few hundred.
_CHUNK = 5_000

# Same order and same reasoning as `storage.pit`. Named here so the export and
# the read path cannot drift apart silently.
SOURCE_PREFERENCE: tuple[str, ...] = ("sec", "fmp", "yahoo")

# The provenance a buyer should be able to read off the file without an
# invoice in front of them. NOT a comment line inside the CSV: a leading `#`
# row makes `pd.read_csv(path)` take it as the header, and this file is sold
# to people whose first move is exactly that. It rides on the response
# instead, where it is machine-readable and costs the parser nothing.
PROVENANCE: dict[str, str] = {
    "X-Dataset-Source": "SEC EDGAR XBRL, as reported",
    "X-Dataset-Grain": "one row per company, metric and period; latest filing wins",
    "X-Dataset-Reconciliation": "Reconciled using A = L + E; exceptions flagged, not hidden",
    "X-Dataset-Static": "static snapshot; it does not update",
    "X-Dataset-About": "https://toscale.pro/dataset",
}

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
    return f"balanceproof-facts-{(today or dt.date.today()).isoformat()}.csv"


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

    # Ordered so the WINNER of each (ticker, metric, period) arrives LAST:
    # ascending filing_date, then source rank ASCENDING. That is the same rule
    # `pit.get_fundamentals` and `balancesheet._resolve_restatements` apply,
    # expressed in SQL, which is the whole point -- the file somebody pays for
    # and the figures on the site must be the same numbers.
    #
    # It used to be an unfiltered dump: four rows for one company and period,
    # two different values for `total_assets`, and no column telling the buyer
    # which one the drawing used. `restated=True` and the source ranking are
    # both carried through so the resolution is auditable rather than merely
    # asserted.
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
        .order_by(
            Fundamental.ticker,
            Fundamental.metric,
            Fundamental.period_end,
            Fundamental.filing_date,
            # ASCENDING, and this was a real bug the other way round. The
            # writer keeps the LAST row of each run, so descending source rank
            # put `sec` first and `yahoo` last -- and the least-preferred
            # source was the one written to the file. The same fixture that
            # made `get_balance_sheet` return the SEC figure made this CSV
            # write Yahoo's, on a site whose whole claim is that the file and
            # the API are the same numbers.
            _source_rank_sql().asc(),
        )
        .execution_options(stream_results=True, yield_per=_CHUNK)
    )

    rows = 0
    written = 0
    # The last row of each run wins. Held rather than emitted immediately, so
    # a restatement arriving later in the same run replaces it and only one
    # row per (ticker, metric, period) ever reaches the file.
    held: tuple[Any, ...] | None = None
    held_key: tuple[Any, Any, Any] | None = None
    with session_scope() as session:
        for chunk in session.execute(stmt).partitions(_CHUNK):
            for row in chunk:
                key = (row[0], row[1], row[3])
                if held_key is not None and key != held_key:
                    writer.writerow([_fmt(v) for v in held])
                    written += 1
                held, held_key = tuple(row), key
            rows += len(chunk)
            yield drain()
        if held is not None:
            writer.writerow([_fmt(v) for v in held])
            written += 1
            yield drain()
    log.info("dataset_streamed", rows=rows, written=written)


def _source_rank_sql():
    """Source preference as a SQL expression, most-preferred highest.

    Mirrors `pit._SOURCE_PREFERENCE`, most-preferred HIGHEST. Ordered ASC
    alongside an ascending filing_date, because the writer keeps the last row
    of each run: ascending rank puts the most-preferred source last, so on a
    same-day tie the SEC as-reported figure is the one that survives -- and
    as-reported is what this dataset claims to contain.
    """
    from sqlalchemy import case

    return case(
        {s: len(SOURCE_PREFERENCE) - i for i, s in enumerate(SOURCE_PREFERENCE)},
        value=Fundamental.source,
        else_=0,
    )


_COUNT_TTL_S = 900.0
_SHAPE_TTL_S = 900.0
_count_cache: tuple[float, int] | None = None


def facts_label() -> str:
    """The dataset's size as marketing copy says it: "1.8M", "1.24M", "980k".

    ONE source for a number that was being written down in five places. The
    site said "1.7M data points" on the home page, /api, the blog and llms.txt
    while /dataset -- which computes it live -- said "1.8M rows", for the same
    table. Two different totals for one product, on a site whose entire pitch
    is that its figures agree with each other.

    They drifted because four of the five were string literals typed at four
    different times. Replacing them with a fifth literal would only reset the
    clock, so they now all call this, and it reads the same `row_count()` the
    dataset page does -- memoised for fifteen minutes against a table that
    moves four times a year.

    Returns "" when the count cannot be read. Every caller renders that as
    nothing rather than as a zero: "0 facts" on the front page of a data
    product is worse than saying nothing at all.
    """
    try:
        n = row_count()
    except Exception as exc:  # noqa: BLE001 - copy must not take a page down
        log.warning("facts_label_failed", error=str(exc)[:200])
        return ""
    if n <= 0:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


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
