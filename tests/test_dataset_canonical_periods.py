"""The paid CSV and the API must agree on what a quarter is.

SEC's bulk datasets round a period end to the month end (Apple's 2026-03-28 reads
2026-03-31) and the XBRL frames carry the real date, so a filing loaded by both
routes is stored twice. The API already treats the real date as canonical
(`src.company.periods`); `dataset.iter_csv` used to key on the raw date and sold
both copies. These tests pin the rule it applies now, per ticker and per filing:

* a rounded row whose real-dated twin carries the same value is dropped;
* a rounded row whose twin differs stays under its rounded date and is counted;
* a rounded row with no twin for that metric is re-dated, never dropped.

Everything uses temporary SQLite databases and no external service.
"""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest

D = dt.date

# (ticker, [(real period end, rounded month end, filing date), ...]) -- the real
# dates are the ones in the live table. DE's real dates fall AFTER the month end
# (08-02 against 07-31); the others fall before it.
QUARTERS = {
    "AAPL": [(D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)),
             (D(2025, 12, 27), D(2025, 12, 31), D(2026, 1, 30)),
             (D(2025, 9, 27), D(2025, 9, 30), D(2025, 10, 31)),
             (D(2025, 6, 28), D(2025, 6, 30), D(2025, 8, 1))],
    "NVDA": [(D(2026, 4, 26), D(2026, 4, 30), D(2026, 5, 20)),
             (D(2026, 1, 25), D(2026, 1, 31), D(2026, 2, 26)),
             (D(2025, 10, 26), D(2025, 10, 31), D(2025, 11, 19)),
             (D(2025, 7, 27), D(2025, 7, 31), D(2025, 8, 27))],
    "ADBE": [(D(2026, 5, 29), D(2026, 5, 31), D(2026, 6, 15)),
             (D(2026, 2, 27), D(2026, 2, 28), D(2026, 3, 14)),
             (D(2025, 11, 28), D(2025, 11, 30), D(2025, 12, 12)),
             (D(2025, 8, 29), D(2025, 8, 31), D(2025, 9, 12))],
    "JNJ": [(D(2026, 6, 28), D(2026, 6, 30), D(2026, 7, 20)),
            (D(2026, 3, 29), D(2026, 3, 31), D(2026, 4, 22)),
            (D(2025, 12, 28), D(2025, 12, 31), D(2026, 2, 12)),
            (D(2025, 9, 28), D(2025, 9, 30), D(2025, 10, 21))],
    "DE": [(D(2026, 8, 2), D(2026, 7, 31), D(2026, 8, 27)),
           (D(2026, 5, 3), D(2026, 4, 30), D(2026, 5, 22)),
           (D(2026, 2, 1), D(2026, 1, 31), D(2026, 2, 20)),
           (D(2025, 11, 2), D(2025, 10, 31), D(2025, 11, 25))],
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'dataset.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    yield
    get_settings.cache_clear()
    reset_engine_cache()


def add(ticker, metric, value, period, filed, *, source="sec"):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        s.add(Fundamental(
            ticker=ticker, metric=metric, value=value, period_end=period,
            fiscal_period="Q2", filing_date=filed, source=source,
        ))


def seed_filer(ticker, *, conflict=False):
    """Every quarter loaded by both routes: total assets and liabilities under
    both dates (equal values), equity ONLY under the rounded date (the bulk route
    had it, the frames did not). With `conflict`, one more metric whose two
    copies disagree, in the newest quarter."""
    for i, (real, rounded, filed) in enumerate(QUARTERS[ticker]):
        for metric, value in (("total_assets", 1000.0 + i), ("total_liabilities", 600.0 + i)):
            add(ticker, metric, value, rounded, filed)
            add(ticker, metric, value, real, filed)
        add(ticker, "total_equity", 400.0 + i, rounded, filed)
        if conflict and i == 0:
            add(ticker, "investment_securities", 1350.0, rounded, filed)
            add(ticker, "investment_securities", 1303.0, real, filed)


def stream(collapse=None):
    from src.dataset import iter_csv

    text = "".join(iter_csv(collapse))
    return list(csv.DictReader(io.StringIO(text)))


def tup(metric, value, period, filed, *, source="sec", ticker="T"):
    """A row in the SELECT order `_collapse_ticker` takes."""
    return (ticker, metric, value, period, "Q2", filed, source, False, None)


REAL, ROUNDED, FILED = D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)


# ---------------------------------------------------------------------------
# The rule, on rows
# ---------------------------------------------------------------------------

def test_a_twin_carrying_the_same_value_collapses_into_the_real_dated_row():
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        tup("total_assets", 100.0, ROUNDED, FILED),
        tup("total_assets", 100.0, REAL, FILED),
    ])
    assert [(r[3], r[2]) for r in out] == [(REAL, 100.0)]
    assert stats == Collapse(removed=1, redated=0, conflicts=0)


def test_a_metric_that_exists_only_on_the_rounded_side_is_re_dated_not_dropped():
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        tup("total_assets", 100.0, ROUNDED, FILED),
        tup("total_assets", 100.0, REAL, FILED),
        tup("total_equity", 40.0, ROUNDED, FILED),
    ])
    by_metric = {r[1]: (r[3], r[2]) for r in out}
    assert by_metric["total_equity"] == (REAL, 40.0), "lost, or kept under SEC's date"
    assert len(out) == 2
    assert stats == Collapse(removed=1, redated=1, conflicts=0)


def test_twins_with_different_values_keep_both_rows_and_are_counted():
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        tup("investment_securities", 1350.0, ROUNDED, FILED),
        tup("investment_securities", 1303.0, REAL, FILED),
    ])
    assert sorted((r[3], r[2]) for r in out) == [(REAL, 1303.0), (ROUNDED, 1350.0)]
    assert stats == Collapse(removed=0, redated=0, conflicts=1)


def test_the_counts_match_a_filing_built_to_produce_them():
    """3 metrics with equal twins, 2 only on the rounded side, 1 that disagrees:
    3 dropped, 2 re-dated, 1 conflict, and 3 + 2 + 2 rows written."""
    from src.dataset import Collapse, _collapse_ticker

    rows = []
    for m in ("a", "b", "c"):
        rows += [tup(m, 1.0, ROUNDED, FILED), tup(m, 1.0, REAL, FILED)]
    for m in ("d", "e"):
        rows.append(tup(m, 2.0, ROUNDED, FILED))
    rows += [tup("f", 3.0, ROUNDED, FILED), tup("f", 4.0, REAL, FILED)]

    out, stats = _collapse_ticker(rows)
    assert stats == Collapse(removed=3, redated=2, conflicts=1)
    assert len(out) == len(rows) - stats.removed == 7


def test_a_twin_in_another_filing_is_not_merged():
    """The rule is per filing: a rounded row in one filing is not collapsed into
    a real-dated row from a different filing, however near the dates are."""
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        tup("total_assets", 100.0, ROUNDED, D(2026, 5, 1)),
        tup("total_assets", 100.0, REAL, D(2026, 8, 1)),
    ])
    assert len(out) == 2
    assert stats == Collapse()


def test_an_ambiguous_twin_is_left_alone():
    """Two real dates equally near one month end: which is "the" twin is a guess,
    so nothing is touched (the same refusal as scripts/fix_period_ends.py)."""
    from src.dataset import Collapse, _collapse_ticker

    month_end, a, b = D(2026, 4, 30), D(2026, 4, 25), D(2026, 5, 5)
    out, stats = _collapse_ticker([
        tup("total_assets", 100.0, month_end, FILED),
        tup("total_assets", 100.0, a, FILED),
        tup("total_assets", 100.0, b, FILED),
    ])
    assert len(out) == 3
    assert stats == Collapse()


def test_a_rounded_row_is_only_collapsed_into_a_twin_from_the_same_source():
    """SEC's rounded row and a vendor's real-dated row are not each other's twin:
    the SEC row is re-dated, then the usual source preference picks SEC."""
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        tup("total_assets", 100.0, ROUNDED, FILED, source="sec"),
        tup("total_assets", 99.0, REAL, FILED, source="yahoo"),
    ])
    assert [(r[3], r[2], r[6]) for r in out] == [(REAL, 100.0, "sec")]
    assert stats == Collapse(removed=0, redated=1, conflicts=0)


def test_a_restatement_after_a_collapse_still_wins_on_the_real_date():
    from src.dataset import _collapse_ticker

    later = D(2026, 8, 1)
    out, _ = _collapse_ticker([
        tup("total_assets", 100.0, ROUNDED, FILED),
        tup("total_assets", 100.0, REAL, FILED),
        tup("total_assets", 120.0, REAL, later),   # restated in a later 10-Q
    ])
    assert [(r[3], r[2], r[5]) for r in out] == [(REAL, 120.0, later)]


# ---------------------------------------------------------------------------
# The file, through the real generator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["AAPL", "NVDA", "ADBE", "JNJ", "DE"])
def test_total_assets_has_exactly_one_row_per_quarter_on_the_real_date(db, ticker):
    seed_filer(ticker)
    rows = [r for r in stream() if r["metric"] == "total_assets"]
    assert sorted(r["period_end"] for r in rows) == sorted(
        real.isoformat() for real, _, _ in QUARTERS[ticker]
    ), f"{ticker}: a quarter appears twice, or under SEC's rounded date"


def test_no_ticker_metric_period_is_written_twice(db):
    for t in QUARTERS:
        seed_filer(t)
    add("COST", "total_assets", 500.0, D(2026, 5, 31), D(2026, 6, 20))
    keys = [(r["ticker"], r["metric"], r["period_end"]) for r in stream()]
    assert len(keys) == len(set(keys))


def test_no_ticker_metric_canonical_period_is_written_twice_except_a_counted_conflict(db):
    """Judged by the API's own rule, not by the raw date: map every written
    period through `period_aliases` and count what still collides. With no
    conflicts seeded that is nothing; with DE's one, exactly that metric."""
    from collections import Counter

    from src.company.periods import period_aliases

    for t in QUARTERS:
        seed_filer(t, conflict=(t == "DE"))
    out = stream()
    by_ticker: dict[str, list[dict]] = {}
    for r in out:
        by_ticker.setdefault(r["ticker"], []).append(r)
    clashes = Counter()
    for t, rows in by_ticker.items():
        aliases = period_aliases(
            [(D.fromisoformat(r["period_end"]), D.fromisoformat(r["filing_date"])) for r in rows]
        )
        seen = Counter()
        for r in rows:
            pe = D.fromisoformat(r["period_end"])
            seen[(r["metric"], aliases.get(pe, pe))] += 1
        clashes.update({(t,) + k: n for k, n in seen.items() if n > 1})
    assert set(clashes) == {("DE", "investment_securities", D(2026, 8, 2))}


@pytest.mark.parametrize("ticker", ["AAPL", "NVDA", "ADBE", "JNJ", "DE"])
def test_the_rows_removed_per_ticker_match_the_expected_collapse(db, ticker):
    """Per quarter: total assets and liabilities each collapse (2 dropped) and
    equity is re-dated (1). Four quarters: 8 dropped, 4 re-dated. DE adds one
    metric whose copies disagree: 1 conflict, and both of its rows are written."""
    from src.dataset import Collapse

    conflict = ticker == "DE"
    seed_filer(ticker, conflict=conflict)
    raw = 4 * 5 + (2 if conflict else 0)   # 2 metrics x 2 dates + equity, per quarter

    done = Collapse()
    out = stream(done)
    assert done == Collapse(removed=8, redated=4, conflicts=1 if conflict else 0)
    assert len(out) == raw - done.removed


def test_a_calendar_aligned_filer_is_written_exactly_as_before(db):
    """COST-shaped: every date a month end, so there is nothing to collapse. A
    restatement and a vendor row ride along; the output is the old rule's, row
    for row, and the counters stay at zero."""
    from src.dataset import Collapse

    q1, q2 = D(2026, 2, 28), D(2026, 5, 31)
    add("COST", "total_assets", 100.0, q1, D(2026, 3, 20))
    add("COST", "total_assets", 105.0, q1, D(2026, 6, 20))            # restated later
    add("COST", "total_assets", 130.0, q2, D(2026, 6, 20))
    add("COST", "total_assets", 129.0, q2, D(2026, 6, 20), source="yahoo")  # SEC wins the tie
    add("COST", "total_equity", 40.0, q2, D(2026, 6, 20))

    done = Collapse()
    got = [(r["metric"], r["value"], r["period_end"], r["filing_date"], r["source"])
           for r in stream(done)]
    assert got == [
        ("total_assets", "105.0", "2026-02-28", "2026-06-20", "sec"),
        ("total_assets", "130.0", "2026-05-31", "2026-06-20", "sec"),
        ("total_equity", "40.0", "2026-05-31", "2026-06-20", "sec"),
    ]
    assert done == Collapse()


def test_a_re_dated_row_keeps_everything_but_its_date(db):
    seed_filer("AAPL")
    real, rounded, filed = QUARTERS["AAPL"][0]
    row = next(r for r in stream() if r["metric"] == "total_equity" and r["period_end"] == real.isoformat())
    assert (row["value"], row["fiscal_period"], row["filing_date"], row["source"]) == (
        "400.0", "Q2", filed.isoformat(), "sec")
