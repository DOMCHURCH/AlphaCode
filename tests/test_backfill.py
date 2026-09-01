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
    from src.ingest import polygon, stooq, yahoo
    from src.storage.db import session_scope
    from src.storage.models import DailyBar

    # Keyless sources come first in the chain; stub BOTH to fail so this test
    # deterministically exercises the paid-tier Polygon fallback it is about.
    # Leaving Yahoo live would make the suite hit the network.
    async def no_stooq():
        raise RuntimeError("stooq unavailable in test")

    async def no_yahoo(tickers, start, end, **kw):
        return []

    monkeypatch.setattr(stooq, "download_bulk", no_stooq)
    monkeypatch.setattr(yahoo, "fetch_daily_bars_batch", no_yahoo)

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
    from src.ingest import polygon, stooq, yahoo

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

    async def no_yahoo(tickers, start, end, **kw):
        return []

    monkeypatch.setattr(polygon, "fetch_grouped_daily", boom_grouped)
    monkeypatch.setattr(stooq, "download_bulk", fake_download)
    monkeypatch.setattr(stooq, "load_bulk_from_zip", fake_load)
    # Yahoo leads the keyless chain now; stub it empty so this test still
    # exercises the Stooq leg. NEVER leave it live -- the real call would
    # enumerate the universe and hit the network from the test suite.
    monkeypatch.setattr(yahoo, "fetch_daily_bars_batch", no_yahoo)

    rows = asyncio.run(bf.backfill_bars(252, end=END))
    assert rows > 0
    assert polygon_called == [], "free-tier backfill hit Polygon"
    st = bf.get_backfill_state()
    assert st["source"] == "stooq"
    assert st["phase"] == "done"
    assert st["polygon_tier"] == "free"
    # Both keyless sources are offered; polygon is excluded on the free tier.
    assert st["sources_available"] == ["yahoo_batch", "stooq"]


def test_fmp_batch_eod_is_fallback_when_stooq_fails(bf_db, monkeypatch):
    """With an FMP key, batch EOD is the second whole-market bulk option."""
    import asyncio

    import src.backfill as bf
    from src.ingest import fmp, stooq, yahoo

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setenv("FMP_API_KEY", "fk")
    monkeypatch.setenv("POLYGON_TIER", "free")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    async def no_stooq():
        raise RuntimeError("stooq down")

    async def no_yahoo(tickers, start, end, **kw):
        return []

    async def fake_batch(date):
        return [{"ticker": "AAA", "date": date, "open": 1, "high": 1,
                 "low": 1, "close": 9.0, "volume": 100}]

    monkeypatch.setattr(stooq, "download_bulk", no_stooq)
    monkeypatch.setattr(yahoo, "fetch_daily_bars_batch", no_yahoo)
    monkeypatch.setattr(fmp, "fetch_batch_eod", fake_batch)

    rows = asyncio.run(bf.backfill_bars(5, end=END))
    assert rows > 0
    st = bf.get_backfill_state()
    assert st["source"] == "fmp_batch_eod"
    assert st["phase"] == "done"
    assert {"yahoo_batch", "stooq", "fmp_batch_eod"} <= set(st["sources_available"])


def test_fmp_batch_eod_capability_probe_falls_through(bf_db, monkeypatch):
    """A plan without batch EOD (402/403) must not loop a dead endpoint -- the
    probe detects it and the chain reports all sources failed."""
    import asyncio

    import src.backfill as bf
    from src.ingest import fmp, stooq, yahoo
    from src.ingest.base import PermanentAPIError

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setenv("FMP_API_KEY", "fk")
    monkeypatch.setenv("POLYGON_TIER", "free")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    async def no_stooq():
        raise RuntimeError("stooq down")

    async def no_yahoo(tickers, start, end, **kw):
        return []

    async def not_entitled(date):
        raise PermanentAPIError("fmp 403: Exclusive Endpoint, upgrade your plan")

    monkeypatch.setattr(stooq, "download_bulk", no_stooq)
    monkeypatch.setattr(yahoo, "fetch_daily_bars_batch", no_yahoo)
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


# ---------------------------------------------------------------------------
# Yahoo is the keyless primary now that Stooq's archive sits behind a challenge
# ---------------------------------------------------------------------------
def test_yahoo_leads_the_keyless_chain(monkeypatch):
    """With no keys at all, the service must still have a working bar source --
    and it must be the one that is actually reachable."""
    from src.backfill import _wide_end_backfill_chain
    from src.config.settings import get_settings

    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setenv("FMP_API_KEY", "")
    get_settings.cache_clear()
    try:
        names = [n for n, _ in _wide_end_backfill_chain(get_settings())]
    finally:
        get_settings.cache_clear()
    assert names[0] == "yahoo_batch"
    # Stooq is kept behind it, not deleted: one request for the whole market's
    # history is still the better source if the challenge ever lifts.
    assert "stooq" in names


def test_the_bar_universe_prefers_tickers_we_already_track(bf_db, monkeypatch):
    """A daily top-up should refresh what the site shows, not re-crawl every
    registrant -- and must not call SEC at all when it has bars to go on."""
    import asyncio

    import src.backfill as bf
    from src.storage.db import session_scope
    from src.storage.models import DailyBar

    async def boom():
        raise AssertionError("SEC must not be called when bars already exist")

    monkeypatch.setattr(bf, "_cik_to_ticker", boom)
    with session_scope() as s:
        for t in ("BBB", "AAA"):
            s.add(DailyBar(ticker=t, date=END, open=1, high=1, low=1,
                           close=1, volume=1))

    assert asyncio.run(bf._bar_universe(None)) == ["AAA", "BBB"]


def test_the_bar_universe_falls_back_to_secs_list_on_a_cold_database(
    bf_db, monkeypatch
):
    import asyncio

    import src.backfill as bf

    async def fake_map():
        return {"320193": "AAPL", "789019": "MSFT"}

    monkeypatch.setattr(bf, "_cik_to_ticker", fake_map)
    assert asyncio.run(bf._bar_universe(None)) == ["AAPL", "MSFT"]
    assert asyncio.run(bf._bar_universe(1)) == ["AAPL"]  # cap applies


def test_yahoo_returning_nothing_raises_so_the_chain_moves_on(bf_db, monkeypatch):
    """A silent zero would look like "the market had no data today". The loader
    must raise with the reason so the orchestrator records it and tries the
    next source."""
    import asyncio

    import pytest

    import src.backfill as bf
    from src.ingest import yahoo

    async def empty(tickers, start, end, **kw):
        return []

    monkeypatch.setattr(bf, "_bar_universe", lambda cap: _done(["AAA"]))
    monkeypatch.setattr(yahoo, "fetch_daily_bars_batch", empty)
    monkeypatch.setattr(yahoo, "last_error", lambda: "Yahoo download timed out")

    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(bf._backfill_bars_yahoo(30, END))


def test_yahoo_rows_are_written(bf_db, monkeypatch):
    import asyncio

    import src.backfill as bf
    from src.ingest import yahoo
    from src.storage.db import session_scope
    from src.storage.models import DailyBar
    from sqlalchemy import func, select

    async def rows(tickers, start, end, **kw):
        return [{"ticker": "AAA", "date": END, "open": 1.0, "high": 2.0,
                 "low": 0.5, "close": 1.5, "volume": 10}]

    monkeypatch.setattr(bf, "_bar_universe", lambda cap: _done(["AAA"]))
    monkeypatch.setattr(yahoo, "fetch_daily_bars_batch", rows)

    n = asyncio.run(bf._backfill_bars_yahoo(30, END))
    assert n == 1
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(DailyBar)).scalar_one() == 1


async def _done(value):
    """Wrap a plain value as an awaited result, for stubbing async helpers."""
    return value
