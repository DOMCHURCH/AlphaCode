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

ONE QUARTER, ONE PERIOD END. SEC's bulk datasets round every period end to the
nearest month end (Apple's 2026-03-28 arrives as 2026-03-31) and the XBRL frames
carry the real date, so a filing loaded by both routes is stored twice under two
dates -- and the one-row-per-period rule above, keyed on the raw date, kept both.
The API already treats the real date as canonical (`src.company.periods`); the
file now applies the same rule before it resolves restatements, per ticker:

* a rounded row whose real-dated twin (same filing, same source, same metric)
  carries the SAME value is dropped -- the twin stands for it;
* a rounded row whose twin carries a DIFFERENT value stays, under its rounded
  date, and is counted (`Collapse.conflicts`) -- neither figure is guessed at.
  Both copies carry a `period_note` ("value differs from the <date> row"), the
  file's last column and blank everywhere else;
* a rounded row with NO twin for that metric is re-dated to the real period end
  rather than dropped, so no figure is lost.

A month end that has no real-dated twin in its own filing (a filing loaded only
from the bulk datasets) is untouched: it still carries SEC's date. The API also
aliases across filings, so such a row can still differ between this file and
`/api`; `scripts/fix_period_ends.py` and a re-load are what close that gap.

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
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

import structlog
from sqlalchemy import select

from src.company.periods import filing_twins
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
    "X-Dataset-Freshness": (
        "built from the database when you request it; a copy you save does not change"
    ),
    "X-Dataset-Period-Ends": (
        "the real period end where the filing reports one; filings loaded only from "
        "SEC bulk data sets keep SEC's month-end date, up to 15 days from the actual one"
    ),
    "X-Dataset-About": "https://balanceproof.dev/dataset",
}

# `period_note` is LAST on purpose: a reader that takes the first nine columns by
# position reads this file exactly as it read the last one. Blank on every row
# except the two copies of a figure whose value differs between the month-end
# date and the real date (see `Collapse.conflicts`).
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
    "period_note",
)


def filename(today: dt.date | None = None) -> str:
    return f"balanceproof-facts-{(today or dt.date.today()).isoformat()}.csv"


def _fmt(v: object) -> object:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return "" if v is None else v


_TICKER, _METRIC, _VALUE, _PERIOD, _FISCAL, _FILED, _SOURCE = range(7)  # SELECT order
_COUNT_CHUNK = 150  # tickers per query in `file_row_count`
_MISSING = object()


@dataclass
class Collapse:
    """What the canonical-period pass did, counted the way the code does it.

    `removed`: rounded rows dropped because their real-dated twin (same filing,
    source and metric) carries the same value. `redated`: rounded rows with no
    twin for that metric, written under the real period end instead. `conflicts`:
    rounded rows kept under their rounded date because the twin's value differs.
    Both figures are written, one under each date, and both rows carry a
    `period_note` naming the other date, so a pair is recognisable from either
    side.

    These are counts of RAW rows touched, before the latest-filing rule picks a
    winner, so they are not the same as how much shorter the file got.
    """

    removed: int = 0
    redated: int = 0
    conflicts: int = 0

    def add(self, other: Collapse) -> None:
        self.removed += other.removed
        self.redated += other.redated
        self.conflicts += other.conflicts


def _twin_map(
    filed_periods: Iterable[tuple[dt.date, dt.date]],
) -> dict[tuple[dt.date, dt.date], dt.date]:
    """{(filing_date, rounded month end): real period end} for ONE ticker.

    Takes (filing_date, period_end) pairs, so it can be fed from the rows being
    written or from a bare list of distinct dates (`file_row_count`).

    The pairing rule of `src.company.periods` (a month end with a non-month-end
    date 1 to 15 days away in the same filing is that date's rounded copy), kept
    per filing rather than per date so a twin in one filing never reaches into
    another. Ambiguous cases are left alone, as `scripts/fix_period_ends.py`
    leaves them: two equally near real dates, or one real date claimed by two
    month ends, means guessing which is "the" twin.
    """
    dates: dict[dt.date, set[dt.date]] = {}
    for filed, period_end in filed_periods:
        dates.setdefault(filed, set()).add(period_end)
    out: dict[tuple[dt.date, dt.date], dt.date] = {}
    for filed, period_ends in dates.items():
        twins = filing_twins(period_ends)
        claimed = Counter(real for reals in twins.values() for real in set(reals))
        for month_end, reals in twins.items():
            if len(reals) == 1 and claimed[reals[0]] == 1:
                out[(filed, month_end)] = reals[0]
    return out


def _source_rank(source: str | None) -> int:
    """Most-preferred highest, the same ranking `balancesheet` and `pit` use."""
    try:
        return len(SOURCE_PREFERENCE) - SOURCE_PREFERENCE.index(source)  # type: ignore[arg-type]
    except ValueError:
        return 0


def _collapse_ticker(
    rows: Sequence[tuple[Any, ...]],
    twin: dict[tuple[dt.date, dt.date], dt.date] | None = None,
) -> tuple[list[tuple[Any, ...]], Collapse]:
    """One ticker's raw rows -> (the rows to write, what was collapsed).

    Rounded rows are first merged into their real-dated twin, re-dated, or kept
    as conflicts (see `Collapse`); then the latest filing wins per
    (metric, period_end), source preference breaking a same-day tie. The result
    is ordered by (metric, period_end), the order the file has always had, and
    every row gains a trailing `period_note`: blank, or on the two copies of a
    conflicting figure, "value differs from the <other date> row".

    `twin` is for a caller that has already worked the pairing out from ALL of
    the ticker's dates and is passing only some of its rows (`file_row_count`):
    recomputing it from a subset could miss an ambiguity and pair wrongly.
    """
    stats = Collapse()
    if twin is None:
        twin = _twin_map((r[_FILED], r[_PERIOD]) for r in rows)
    # row key -> (note, key of the row it disagrees with)
    notes: dict[tuple[Any, ...], tuple[str, tuple[Any, ...]]] = {}
    if not twin:
        kept = list(rows)  # a calendar-aligned filer: nothing to collapse
    else:
        value_at = {(r[_METRIC], r[_PERIOD], r[_FILED], r[_SOURCE]): r[_VALUE] for r in rows}
        kept = []
        for r in rows:
            real = twin.get((r[_FILED], r[_PERIOD]))
            if real is None:
                kept.append(r)
                continue
            other = value_at.get((r[_METRIC], real, r[_FILED], r[_SOURCE]), _MISSING)
            if other is _MISSING:
                kept.append(r[:_PERIOD] + (real,) + r[_PERIOD + 1:])
                stats.redated += 1
            elif other == r[_VALUE]:
                stats.removed += 1
            else:
                kept.append(r)
                stats.conflicts += 1
                rounded = r[_PERIOD]
                k_rounded = (r[_METRIC], rounded, r[_FILED], r[_SOURCE])
                k_real = (r[_METRIC], real, r[_FILED], r[_SOURCE])
                notes[k_rounded] = (f"value differs from the {real.isoformat()} row", k_real)
                notes[k_real] = (f"value differs from the {rounded.isoformat()} row", k_rounded)

    kept.sort(key=lambda r: (r[_METRIC], r[_PERIOD], r[_FILED], _source_rank(r[_SOURCE]), r[_SOURCE] or ""))
    winners: list[tuple[Any, ...]] = []
    for r in kept:
        if winners and (winners[-1][_METRIC], winners[-1][_PERIOD]) == (r[_METRIC], r[_PERIOD]):
            winners[-1] = r  # a later filing, or a preferred source on the same day
        else:
            winners.append(r)
    # A note is only true while the row it points at is in the file too: a later
    # filing can replace one side of a pair, and the survivor then has nothing
    # left to disagree with.
    written = {(r[_METRIC], r[_PERIOD], r[_FILED], r[_SOURCE]) for r in winners}
    out = []
    for r in winners:
        note, partner = notes.get((r[_METRIC], r[_PERIOD], r[_FILED], r[_SOURCE]), ("", None))
        out.append(r + (note if partner in written else "",))
    return out, stats


def iter_csv(collapse: Collapse | None = None) -> Iterator[str]:
    """Yield the dataset as CSV text, header first.

    `collapse`, when given, is filled in with what the canonical-period pass did
    across the whole file (see `Collapse`); the same totals are logged.

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

    # Rows arrive grouped by ticker -- the unit `_collapse_ticker` works on,
    # because a rounded date and its real twin are found by comparing the dates
    # of one filing across ALL its metrics. Within a ticker the order is
    # irrelevant to the result (it is re-sorted there), but it is kept explicit
    # so the stream is the same on every run. A ticker is a few hundred rows,
    # so holding one at a time keeps the memory bound the module promises.
    #
    # The winner of each (ticker, metric, period) is the latest filing, with
    # source preference breaking a same-day tie: the rule `pit.get_fundamentals`
    # and `balancesheet._resolve_restatements` apply, so the file somebody pays
    # for and the figures on the site are the same numbers. `restated=True` and
    # the source are carried through so the resolution is auditable rather than
    # merely asserted.
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
    total = collapse if collapse is not None else Collapse()
    group: list[tuple[Any, ...]] = []

    def flush() -> None:
        nonlocal written
        out, stats = _collapse_ticker(group)
        for r in out:
            writer.writerow([_fmt(v) for v in r])
        written += len(out)
        total.add(stats)
        group.clear()

    with session_scope() as session:
        for chunk in session.execute(stmt).partitions(_CHUNK):
            for row in chunk:
                if group and row[_TICKER] != group[0][_TICKER]:
                    flush()
                group.append(tuple(row))
            rows += len(chunk)
            yield drain()
        if group:
            flush()
            yield drain()
    log.info(
        "dataset_streamed", rows=rows, written=written,
        twins_collapsed=total.removed, redated=total.redated,
        conflicts=total.conflicts,
    )


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


# Last-resort copy for prose that has to scan as English when the live count
# cannot be read (blog posts, llms.txt). NOT a source of truth: facts_label()
# and home_page.companies_label() are, and every page prefers them. These two
# exist so the fallback is one place to update, not two literals in two files.
# FALLBACK_COMPANIES: last checked against the live site on 2026-10-08 (6,246 companies).
# FALLBACK_FACTS: 2026-10-17, when the file was 1,257,194 rows (the table holds
# 2,016,659, which is NOT what the file contains).
FALLBACK_COMPANIES = "6,246"
FALLBACK_FACTS = "1.3M"

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


def file_row_count() -> int:
    """How many rows `iter_csv` writes, worked out without writing them.

    The table holds about 60% more rows than the file: restated figures and
    several sources for one figure collapse to one row per (ticker, metric,
    period_end), and the canonical-period pass removes the month-end copies on
    top of that. Quoting the table's size as the file's was a 38% overstatement.

    Exact, not an estimate, and by construction the same rule: the base is the
    number of distinct (ticker, metric, period_end) -- the file's grain before
    the period pass -- and the period pass is applied by `_collapse_ticker`
    itself, so the two cannot drift. Only tickers that have a month-end/real-date
    twin are touched, and only the rows on those twin dates are fetched; the
    pairing is still worked out from ALL of a ticker's dates, so an ambiguity
    elsewhere in a filing is seen. `test_dataset_canonical_periods` holds this
    equal to the number of rows the stream actually produces.
    """
    from sqlalchemy import func

    with session_scope() as session:
        base = int(session.execute(
            select(func.count()).select_from(
                select(Fundamental.ticker, Fundamental.metric, Fundamental.period_end)
                .distinct().subquery()
            )
        ).scalar_one())

        dates: dict[str, list[tuple[dt.date, dt.date]]] = defaultdict(list)
        for ticker, filed, period_end in session.execute(
            select(Fundamental.ticker, Fundamental.filing_date, Fundamental.period_end)
            .distinct()
        ):
            dates[ticker].append((filed, period_end))

        twins = {t: tw for t, pairs in dates.items() if (tw := _twin_map(pairs))}
        involved = {
            t: {m for (_, m) in tw} | set(tw.values()) for t, tw in twins.items()
        }

        # A few hundred tickers have a twin; fetching them a chunk at a time keeps
        # this to a handful of round trips instead of one per ticker. A chunk's
        # query asks for the union of its tickers' twin dates, so each ticker's
        # rows are narrowed back to its own before they are used.
        removed = 0
        tickers = sorted(twins)
        for i in range(0, len(tickers), _COUNT_CHUNK):
            chunk = tickers[i:i + _COUNT_CHUNK]
            wanted = set().union(*(involved[t] for t in chunk))
            by_ticker: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
            for row in session.execute(
                select(
                    Fundamental.ticker, Fundamental.metric, Fundamental.value,
                    Fundamental.period_end, Fundamental.fiscal_period,
                    Fundamental.filing_date, Fundamental.source,
                    Fundamental.restated, Fundamental.ingested_at,
                ).where(Fundamental.ticker.in_(chunk), Fundamental.period_end.in_(wanted))
            ):
                if row[_PERIOD] in involved[row[_TICKER]]:
                    by_ticker[row[_TICKER]].append(tuple(row))
            for t in chunk:
                rows = by_ticker.get(t, [])
                before = len({(r[_METRIC], r[_PERIOD]) for r in rows})
                out, _ = _collapse_ticker(rows, twins[t])
                removed += before - len(out)
    return base - removed


def row_count() -> int:
    """How many rows the download contains (`file_row_count`), not how many the
    table holds. Shown on the dashboard so a buyer knows what they are paying
    for before they pay for it.

    Memoised for fifteen minutes. Working the count out reads every distinct
    (ticker, metric, period) once, and this figure is decoration on a page that
    should feel instant -- it changes when a quarter loads. A stale count for a
    quarter of an hour is invisible; a page that waits for it on every view is
    not.
    """
    global _count_cache

    now = time.monotonic()
    if _count_cache is not None and now - _count_cache[0] < _COUNT_TTL_S:
        return _count_cache[1]

    n = file_row_count()
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
        writer.writerow([_fmt(v) for v in row] + [""])  # + the blank `period_note`
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

    # The file's row count, not the table's (see `file_row_count`); shared with
    # `row_count` so the page, the dashboard and the copy quote one number.
    rows = row_count()
    with session_scope() as session:
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
