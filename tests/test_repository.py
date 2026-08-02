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

from src import pipeline
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


def test_pipeline_aborts_when_a_stage_writes_nothing_it_tried():
    """>100 attempted, 0 written -> DataQualityError, not a warning-and-continue."""
    repository.reset_write_ledger()
    repository._record_write("filing_events", 150, 0)
    with pytest.raises(pipeline.DataQualityError, match="silently failing"):
        pipeline._check_stage_writes("Stage 3 catalysts")


def test_pipeline_does_not_abort_when_writes_landed():
    repository.reset_write_ledger()
    repository._record_write("filing_events", 150, 150)
    # A small attempt that wrote 0 is NOT an abort (could be a genuinely empty
    # result), only a bulk zero is.
    repository._record_write("news_aggregates", 5, 0)
    assert pipeline._check_stage_writes("Stage 3 catalysts")  # returns stats, no raise
