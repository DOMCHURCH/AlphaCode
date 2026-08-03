"""Upsert correctness + write accounting.

Two bugs this locks down: (1) a column named `items` collided with
ColumnCollection.items() so every FilingEvent upsert died with "can't adapt type
'method'"; (2) that failure was swallowed per-ticker and the run continued having
persisted ZERO filings -- a stage must fail loudly when it writes nothing it
tried to."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from src.storage import repository
from src.storage.models import FilingEvent


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    repository.reset_write_ledger()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()
        repository.reset_write_ledger()


def _filing(accession: str, items: str) -> dict:
    return {
        "ticker": "AAPL", "cik": "320193", "form": "8-K", "items": items,
        "filing_date": dt.date(2025, 6, 1), "accession": accession,
        "primary_doc": None, "detail": {},
    }


def test_upsert_updates_a_column_named_items(db):
    """The reserved-name bug: on_conflict_do_update must set the `items` COLUMN,
    not ColumnCollection.items (a bound method). Re-upserting the same key
    triggers the update path that used to explode."""
    from src.storage.db import session_scope

    with session_scope() as s:
        assert repository.save_filings(s, [_filing("0001", "2.02")]) == 1
    # Same (accession, ticker), new items value -> on_conflict_do_update path.
    with session_scope() as s:
        assert repository.save_filings(s, [_filing("0001", "5.02")]) == 1
    with session_scope() as s:
        got = s.execute(
            select(FilingEvent.items).where(FilingEvent.accession == "0001")
        ).scalar_one()
    assert got == "5.02"  # the update landed, no "can't adapt type 'method'"


def test_write_ledger_counts_successful_writes(db):
    from src.storage.db import session_scope

    repository.reset_write_ledger()
    with session_scope() as s:
        repository.save_filings(s, [_filing("a1", "2.02"), _filing("a2", "1.01")])
    assert repository.get_write_ledger()["filing_events"] == {"attempted": 2, "written": 2}
    assert repository.assert_writes(min_attempts=100) == []


def test_upsert_records_zero_written_when_the_execute_fails():
    """A failed execute must still record attempted>0, written=0 -- that is the
    signal a swallowed persist bug leaves behind."""

    class _BoomSession:
        bind = None  # -> sqlite dialect

        def execute(self, *a, **k):
            raise RuntimeError("can't adapt type 'method'")

    repository.reset_write_ledger()
    rows = [_filing(f"a{i}", "2.02") for i in range(150)]
    with pytest.raises(RuntimeError):
        repository._upsert(_BoomSession(), FilingEvent, rows, ["accession", "ticker"])

    stats = repository.get_write_ledger()["filing_events"]
    assert stats["attempted"] == 150 and stats["written"] == 0
    assert "filing_events" in repository.assert_writes(min_attempts=100)


# ---------------------------------------------------------------------------
# Dedup: SEC bulk num.txt repeats the same fact in one batch (amended filings,
# and several XBRL tags mapping to one metric). Postgres aborts the whole
# statement with CardinalityViolation ("cannot affect row a second time"), so the
# collapse happens in Python -- and WHICH row survives is a PIT decision.
# ---------------------------------------------------------------------------
def _fund(metric: str, value: float, filed: dt.date, period=dt.date(2024, 6, 30)) -> dict:
    return {
        "ticker": "AAPL", "metric": metric, "value": value, "period_end": period,
        "fiscal_period": "Q3", "filing_date": filed, "source": "sec", "restated": False,
    }


def test_duplicate_fact_keeps_the_earliest_filing_date(db):
    """The original filing is what was known at the time. A later restatement of
    the same period must NOT overwrite it inside one load -- that would import
    information that did not exist yet."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    rows = [
        # Deliberately out of order, and the restatement is FIRST in the list, so
        # "whatever is last/first in the file wins" would pick the wrong one.
        _fund("revenue", 999.0, dt.date(2025, 2, 1)),   # later restatement
        _fund("revenue", 100.0, dt.date(2024, 8, 1)),   # as first reported
        _fund("revenue", 555.0, dt.date(2024, 11, 5)),  # amended 10-Q/A
    ]
    with session_scope() as s:
        assert repository.save_fundamentals(s, rows) == 1  # 3 in, 1 written

    with session_scope() as s:
        got = s.execute(
            select(Fundamental.value, Fundamental.filing_date)
        ).all()
    assert len(got) == 1
    value, filed = got[0]
    assert value == 100.0, "kept a restatement instead of the as-first-reported value"
    assert filed == dt.date(2024, 8, 1)


def test_dedup_is_per_natural_key_not_global(db):
    """Different metrics/periods are different facts and must all survive."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    rows = [
        _fund("revenue", 1.0, dt.date(2024, 8, 1)),
        _fund("net_income", 2.0, dt.date(2024, 8, 1)),
        _fund("revenue", 3.0, dt.date(2024, 8, 1), period=dt.date(2024, 3, 31)),
        _fund("revenue", 9.0, dt.date(2024, 9, 1)),  # dup of row 1 -> dropped
    ]
    with session_scope() as s:
        assert repository.save_fundamentals(s, rows) == 3
    with session_scope() as s:
        assert s.execute(select(Fundamental)).scalars().all().__len__() == 3


def test_a_later_load_may_still_add_a_restatement(db):
    """filing_date stays in the DB constraint: a restatement filed later is a real
    new fact, visible only from its own filing date. Collapsing within a load must
    not stop a subsequent load from recording it."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        repository.save_fundamentals(s, [_fund("revenue", 100.0, dt.date(2024, 8, 1))])
    with session_scope() as s:
        repository.save_fundamentals(s, [_fund("revenue", 120.0, dt.date(2025, 2, 1))])
    with session_scope() as s:
        rows = s.execute(
            select(Fundamental.value, Fundamental.filing_date)
            .order_by(Fundamental.filing_date)
        ).all()
    assert [r[0] for r in rows] == [100.0, 120.0]


def test_earnings_same_day_filings_collapse_deterministically(db):
    """A 10-Q and a 10-K/A filed the same day collide on (ticker, report_date).
    Neither is lookahead; the tie must break the same way every time."""
    from src.storage.db import session_scope
    from src.storage.models import EarningsEvent

    day = dt.date(2024, 8, 1)
    rows = [
        {"ticker": "AAPL", "report_date": day, "period_end": dt.date(2023, 12, 31),
         "actual_eps": None, "consensus_eps": None, "surprise_pct": None,
         "gap_pct": None, "is_future": False},                       # stale amendment
        {"ticker": "AAPL", "report_date": day, "period_end": dt.date(2024, 6, 30),
         "actual_eps": 1.40, "consensus_eps": None, "surprise_pct": None,
         "gap_pct": None, "is_future": False},                       # the real event
    ]
    with session_scope() as s:
        assert repository.save_earnings(s, rows) == 1
    with session_scope() as s:
        got = s.execute(select(EarningsEvent.actual_eps, EarningsEvent.period_end)).all()
    assert got == [(1.40, dt.date(2024, 6, 30))]


def test_upsert_dedupes_on_conflict_cols_for_every_table(db):
    """The universal safety net: no caller can hand _upsert a batch that would
    touch the same row twice, whatever the table."""
    from src.storage.db import session_scope

    with session_scope() as s:
        # Same (accession, ticker) twice in ONE batch -- the CardinalityViolation
        # shape. Must collapse to a single write, not raise.
        assert repository.save_filings(
            s, [_filing("dup1", "2.02"), _filing("dup1", "5.02")]
        ) == 1
