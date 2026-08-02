"""Backfill sourcing tests.

The Polygon path must be resumable: on a re-run it should skip days already in
the store rather than re-download them. Re-fetching loaded days is what pinned
`bar_dates` in place on a rate-limited plan (the "stuck at N / 252" loader).
"""

from __future__ import annotations

import datetime as dt

import pytest

END = dt.date(2026, 8, 1)


@pytest.fixture
def bf_db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bf.db'}")
    monkeypatch.setenv("POLYGON_API_KEY", "x")  # force the Polygon path
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _business_days(end: dt.date, n: int) -> list[dt.date]:
    out, day = [], end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= dt.timedelta(days=1)
    return out


def test_polygon_backfill_skips_days_already_stored(bf_db, monkeypatch):
    import asyncio

    from sqlalchemy import func, select

    import src.backfill as bf
    from src.ingest import polygon
    from src.storage.db import session_scope
    from src.storage.models import DailyBar

    # Pre-seed 200 recent business days as already loaded.
    seeded = _business_days(END, 200)
    with session_scope() as s:
        for d in seeded:
            s.add(DailyBar(ticker="AAA", date=d, open=1, high=1, low=1,
                           close=10.0, volume=100))

    called: list[dt.date] = []

    async def fake_grouped(day, adjusted=True):
        called.append(day)
        return [{"ticker": "AAA", "date": day, "open": 1, "high": 1,
                 "low": 1, "close": 10.0, "volume": 100}]

    monkeypatch.setattr(polygon, "fetch_grouped_daily", fake_grouped)

    # Ask for 252 sessions; 200 already exist, so only ~52 calls should be made.
    asyncio.run(bf.backfill_bars(252, end=END))

    assert len(called) <= 60, f"re-downloaded loaded days: {len(called)} calls"
    assert not set(called) & set(seeded), "an already-stored day was re-fetched"

    with session_scope() as s:
        bar_dates = s.execute(
            select(func.count(func.distinct(DailyBar.date)))
        ).scalar_one()
    assert bar_dates >= 252
    assert bf.get_backfill_state()["phase"] == "done"
