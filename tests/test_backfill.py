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
    monkeypatch.setenv("POLYGON_API_KEY", "x")
    monkeypatch.setenv("POLYGON_TIER", "paid")  # paid tier: Polygon allowed for backfill
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
    from src.ingest import polygon, stooq
    from src.storage.db import session_scope
    from src.storage.models import DailyBar

    # Bulk sources come first in the chain; stub Stooq to fail so this test
    # deterministically exercises the paid-tier Polygon fallback it is about.
    async def no_stooq():
        raise RuntimeError("stooq unavailable in test")

    monkeypatch.setattr(stooq, "download_bulk", no_stooq)

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
    st = bf.get_backfill_state()
    assert st["phase"] == "done"
    assert st["source"] == "polygon"


def test_free_tier_polygon_key_uses_stooq_not_polygon(bf_db, monkeypatch):
    """The whole point: a Polygon key on the FREE tier must NOT drive the
    multi-day backfill (that 429s). Stooq bulk is used instead."""
    import asyncio

    import src.backfill as bf
    from src.ingest import polygon, stooq

    monkeypatch.setenv("POLYGON_API_KEY", "x")
    monkeypatch.setenv("POLYGON_TIER", "free")  # key present, but free tier
    from src.config.settings import get_settings

    get_settings.cache_clear()

    polygon_called: list = []

    async def boom_grouped(day, adjusted=True):
        polygon_called.append(day)
        raise AssertionError("Polygon must not be used for a free-tier backfill")

    async def fake_download():
        return "/tmp/does-not-matter.zip"

    def fake_load(zip_path, save, *, since=None, max_tickers=None, on_progress=None):
        n = save([{"ticker": "AAA", "date": END, "open": 1, "high": 1,
                   "low": 1, "close": 9.0, "volume": 100}])
        if on_progress:
            on_progress(1, n)
        return n

    monkeypatch.setattr(polygon, "fetch_grouped_daily", boom_grouped)
    monkeypatch.setattr(stooq, "download_bulk", fake_download)
    monkeypatch.setattr(stooq, "load_bulk_from_zip", fake_load)

    rows = asyncio.run(bf.backfill_bars(252, end=END))
    assert rows > 0
    assert polygon_called == [], "free-tier backfill hit Polygon"
    st = bf.get_backfill_state()
    assert st["source"] == "stooq"
    assert st["phase"] == "done"
    assert st["polygon_tier"] == "free"
    assert st["sources_available"] == ["stooq"]  # polygon excluded on free tier


def test_fmp_batch_eod_is_fallback_when_stooq_fails(bf_db, monkeypatch):
    """With an FMP key, batch EOD is the second whole-market bulk option."""
    import asyncio

    import src.backfill as bf
    from src.ingest import fmp, stooq

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setenv("FMP_API_KEY", "fk")
    monkeypatch.setenv("POLYGON_TIER", "free")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    async def no_stooq():
        raise RuntimeError("stooq down")

    async def fake_batch(date):
        return [{"ticker": "AAA", "date": date, "open": 1, "high": 1,
                 "low": 1, "close": 9.0, "volume": 100}]

    monkeypatch.setattr(stooq, "download_bulk", no_stooq)
    monkeypatch.setattr(fmp, "fetch_batch_eod", fake_batch)

    rows = asyncio.run(bf.backfill_bars(5, end=END))
    assert rows > 0
    st = bf.get_backfill_state()
    assert st["source"] == "fmp_batch_eod"
    assert st["phase"] == "done"
    assert "stooq" in st["sources_available"] and "fmp_batch_eod" in st["sources_available"]


def test_fmp_batch_eod_capability_probe_falls_through(bf_db, monkeypatch):
    """A plan without batch EOD (402/403) must not loop a dead endpoint -- the
    probe detects it and the chain reports all sources failed."""
    import asyncio

    import src.backfill as bf
    from src.ingest import fmp, stooq
    from src.ingest.base import PermanentAPIError

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setenv("FMP_API_KEY", "fk")
    monkeypatch.setenv("POLYGON_TIER", "free")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    async def no_stooq():
        raise RuntimeError("stooq down")

    async def not_entitled(date):
        raise PermanentAPIError("fmp 403: Exclusive Endpoint, upgrade your plan")

    monkeypatch.setattr(stooq, "download_bulk", no_stooq)
    monkeypatch.setattr(fmp, "fetch_batch_eod", not_entitled)

    rows = asyncio.run(bf.backfill_bars(5, end=END))
    assert rows == 0
    st = bf.get_backfill_state()
    assert st["phase"] == "error"
    assert "batch-request-end-of-day-prices" in st["last_error"]


def test_free_tier_polygon_bucket_is_five_per_min(monkeypatch):
    """The limiter must actually throttle a free Polygon plan, not fire at paid
    speed -- that mismatch is what produced the 429s."""
    from src.config import rate_limits
    from src.config.settings import get_settings

    monkeypatch.setenv("POLYGON_TIER", "free")
    monkeypatch.setenv("POLYGON_FREE_RATE_PER_MIN", "5")
    get_settings.cache_clear()
    b = rate_limits.bucket_for("polygon")
    assert b is not None
    assert abs(b.rps - 5 / 60.0) < 1e-9
    assert b.burst == 5

    monkeypatch.setenv("POLYGON_TIER", "paid")
    get_settings.cache_clear()
    bp = rate_limits.bucket_for("polygon")
    assert bp.rps == 50.0  # the paid bucket, untouched
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# company names: the index onto everything else
# --------------------------------------------------------------------------
def test_a_fundamentals_load_stores_the_company_names_it_already_fetched(
    bf_db, monkeypatch
):
    """Search by name was dead on a fully loaded instance.

    `_cik_to_ticker` fetches SEC's company list -- which carries the NAME --
    for every fundamentals and earnings load, kept the CIK and dropped the
    name. Nothing else wrote names automatically, so an instance could load
    bars, sectors, fundamentals and earnings and still answer nothing but bare
    tickers: "walmart" was a miss on a database holding WMT.
    """
    import asyncio

    from sqlalchemy import select

    import src.backfill as bf
    from src.ingest import sec_edgar
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    async def fake_tickers():
        return [
            {"ticker": "WMT", "name": "Walmart Inc.", "cik": "0000104169"},
            {"ticker": "JPM", "name": "JPMORGAN CHASE & CO", "cik": "0000019617"},
        ]

    monkeypatch.setattr(sec_edgar, "fetch_company_tickers", fake_tickers)

    cik_map = asyncio.run(bf._cik_to_ticker())

    assert cik_map == {"104169": "WMT", "19617": "JPM"}, "the map still works"
    with session_scope() as s:
        names = dict(
            s.execute(
                select(UniverseSnapshot.ticker, UniverseSnapshot.name)
            ).all()
        )
    assert names == {"WMT": "Walmart Inc.", "JPM": "JPMORGAN CHASE & CO"}


def test_a_failure_to_store_names_never_fails_the_load(bf_db, monkeypatch):
    """Names are an index onto the data, not the data. A load that got the
    fundamentals and lost the names is degraded; one that raises is broken."""
    import asyncio

    import src.backfill as bf
    from src.ingest import sec_edgar

    async def fake_tickers():
        return [{"ticker": "WMT", "name": "Walmart Inc.", "cik": "0000104169"}]

    def boom(_reference):
        raise RuntimeError("universe table is unwritable")

    monkeypatch.setattr(sec_edgar, "fetch_company_tickers", fake_tickers)
    monkeypatch.setattr(bf, "_save_company_names", boom)

    assert asyncio.run(bf._cik_to_ticker()) == {"104169": "WMT"}
