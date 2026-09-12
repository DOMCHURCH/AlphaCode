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

    from sqlalchemy import func, select

    import src.backfill as bf
    from src.ingest import yahoo
    from src.storage.db import session_scope
    from src.storage.models import DailyBar

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


# ---------------------------------------------------------------------------
# Naming the companies company_tickers.json does not list
# ---------------------------------------------------------------------------
# The bug this covers rendered as "AVB (AVB) Balance Sheet": a company with
# filed fundamentals, a drawable balance sheet, and no row in `universe` at
# all, because the reference file lists CURRENT listings and a company page
# exists for anything that has ever filed. 73 companies were in that state.


def _seed_company(ticker, *, name=None, as_of=dt.date(2026, 9, 7), cik=None,
                  assets=1000.0, in_universe=True):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        if in_universe:
            s.add(UniverseSnapshot(
                as_of_date=as_of, ticker=ticker, name=name, cik=cik,
            ))
        s.add(Fundamental(
            ticker=ticker, metric="total_assets", value=assets,
            period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
            filing_date=dt.date(2026, 8, 1), source="sec",
        ))


def test_a_drawable_company_absent_from_the_universe_is_selected(bf_db):
    """The AVB case. No universe row at all, so nothing to read a name from."""
    from src.backfill import _drawable_without_a_name
    from src.storage.db import session_scope

    _seed_company("AVB", in_universe=False)
    _seed_company("WMT", name="Walmart Inc.")

    with session_scope() as s:
        assert _drawable_without_a_name(s) == ["AVB"]


def test_a_name_equal_to_the_ticker_counts_as_missing(bf_db):
    from src.backfill import _drawable_without_a_name
    from src.storage.db import session_scope

    _seed_company("XYZ", name="XYZ")
    _seed_company("ABC", name="")
    _seed_company("DEF", name=None)
    _seed_company("GHI", name="Ghi Industries Inc")

    with session_scope() as s:
        assert _drawable_without_a_name(s) == ["ABC", "DEF", "XYZ"]


def test_the_newest_row_decides_even_when_an_older_one_has_a_name(bf_db):
    """The reader takes the most recent universe row at or before today, so a
    named row from last month does not rescue an unnamed one from today. A
    looser "has any row with a name" query would call this fixed while the
    page still rendered the bare ticker."""
    from src.backfill import _drawable_without_a_name
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed_company("OLD", name="Old Industries", as_of=dt.date(2026, 1, 1))
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date(2026, 9, 7), ticker="OLD", name=None,
        ))

    with session_scope() as s:
        assert _drawable_without_a_name(s) == ["OLD"]


def test_a_company_with_no_balance_sheet_is_not_selected(bf_db):
    """There is no page to fix. Naming it would spend an SEC request on a
    ticker nobody can reach."""
    from src.backfill import _drawable_without_a_name
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        s.add(Fundamental(
            ticker="NOPE", metric="total_assets", value=0.0,
            period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
            filing_date=dt.date(2026, 8, 1), source="sec",
        ))

    with session_scope() as s:
        assert _drawable_without_a_name(s) == []


def test_the_cik_is_found_in_any_table_that_has_one(bf_db):
    """A ticker missing from `universe` is the whole point of this pass, so
    the CIK lookup cannot depend on `universe` alone."""
    from src.backfill import _cik_for
    from src.storage.db import session_scope
    from src.storage.models import FilingEvent, SectorMap

    _seed_company("FROMUNI", name=None, cik="0000000111")
    with session_scope() as s:
        s.add(SectorMap(ticker="FROMSEC", cik="0000000222"))
        s.add(FilingEvent(
            ticker="FROMFIL", cik="0000000333", form="10-Q",
            filing_date=dt.date(2026, 8, 1), accession="0001",
        ))

    with session_scope() as s:
        assert _cik_for(s, "FROMUNI") == "0000000111"
        assert _cik_for(s, "FROMSEC") == "0000000222"
        assert _cik_for(s, "FROMFIL") == "0000000333"
        assert _cik_for(s, "NOWHERE") is None


def test_the_repair_writes_the_name_sec_returns(bf_db, monkeypatch):
    import asyncio

    from sqlalchemy import select

    from src import backfill
    from src.storage.db import session_scope
    from src.storage.models import SectorMap, UniverseSnapshot

    _seed_company("AVB", in_universe=False)
    with session_scope() as s:
        s.add(SectorMap(ticker="AVB", cik="0000915912"))

    seen = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def fake_submissions(_client, cik):
        seen.append(str(cik))
        return {"name": "AVALONBAY COMMUNITIES INC"}

    monkeypatch.setattr(backfill.sec_edgar, "make_client", lambda **k: _FakeClient())
    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", fake_submissions)

    report = asyncio.run(backfill.backfill_missing_company_names(rps=0))

    assert seen == ["0000915912"]
    assert report["named"] == 1 and report["written"] == 1
    assert report["no_cik"] == [] and report["unnamed"] == []

    with session_scope() as s:
        rows = list(s.execute(
            select(UniverseSnapshot).where(UniverseSnapshot.ticker == "AVB")
        ).scalars())
    assert [r.name for r in rows] == ["AVALONBAY COMMUNITIES INC"]
    assert rows[0].as_of_date == dt.date.today(), (
        "backdating into an old snapshot would rewrite what was known then"
    )


def test_the_three_companies_named_after_their_ticker_are_left_alone(
    bf_db, monkeypatch
):
    """RH, CTW and VTEX match the query and are not broken. Rewriting them
    would spend requests to confirm what is already there, and counting them
    as failures would make a clean run look permanently unfinished."""
    import asyncio

    from src import backfill

    for t in ("RH", "CTW", "VTEX"):
        _seed_company(t, name=t)

    async def boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("SEC was called for a company that is not broken")

    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", boom)

    report = asyncio.run(backfill.backfill_missing_company_names(rps=0))

    assert report["matched"] == 3
    assert report["checked"] == 0
    assert report["named"] == 0
    assert report["name_is_the_ticker"] == ["CTW", "RH", "VTEX"]


def test_a_ticker_with_no_cik_anywhere_is_reported_not_guessed(bf_db, monkeypatch):
    """Inventing a CIK writes another company's name onto this page, which is
    worse than the empty field it replaces."""
    import asyncio

    from src import backfill

    _seed_company("GHOST", in_universe=False)

    async def no_reference():
        return []

    monkeypatch.setattr(backfill.sec_edgar, "fetch_company_tickers", no_reference)

    report = asyncio.run(backfill.backfill_missing_company_names(rps=0))

    assert report["no_cik"] == ["GHOST"]
    assert report["named"] == 0 and report["written"] == 0


def test_a_missing_cik_falls_back_to_secs_own_ticker_file(bf_db, monkeypatch):
    """Two of the 73 had no CIK in any table but were in company_tickers.json.
    Asking SEC's own file is not guessing -- it is the authority the rest of
    this module already uses."""
    import asyncio

    from sqlalchemy import select

    from src import backfill
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed_company("LGSP", in_universe=False)

    async def reference():
        return [{"ticker": "LGSP", "cik": 1970129, "name": "ignored"}]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def fake_submissions(_client, cik):
        assert str(cik) == "1970129"
        return {"name": "LEGEND SPICES, INC."}

    monkeypatch.setattr(backfill.sec_edgar, "fetch_company_tickers", reference)
    monkeypatch.setattr(backfill.sec_edgar, "make_client", lambda **k: _FakeClient())
    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", fake_submissions)

    report = asyncio.run(backfill.backfill_missing_company_names(rps=0))

    assert report["no_cik"] == []
    assert report["named"] == 1

    with session_scope() as s:
        name = s.execute(
            select(UniverseSnapshot.name)
            .where(UniverseSnapshot.ticker == "LGSP")
        ).scalar_one()
    assert name == "LEGEND SPICES, INC."


def test_a_blank_name_from_sec_is_left_blank_not_filled_with_the_ticker(
    bf_db, monkeypatch
):
    """A 200 with no name is a real answer -- some CIKs are trusts or filing
    agents. Writing the ticker there would satisfy the query and tell the
    reader nothing true."""
    import asyncio

    from src import backfill

    _seed_company("BLANK", name=None, cik="0000000444")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def fake_submissions(_client, _cik):
        return {"name": "   "}

    monkeypatch.setattr(backfill.sec_edgar, "make_client", lambda **k: _FakeClient())
    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", fake_submissions)

    report = asyncio.run(backfill.backfill_missing_company_names(rps=0))

    assert report["unnamed"] == ["BLANK"]
    assert report["named"] == 0 and report["written"] == 0


# ---------------------------------------------------------------------------
# The name the filings were actually filed under
# ---------------------------------------------------------------------------
# `universe.name` is SEC's CURRENT name for a CIK, and the filings on a page
# were filed earlier. Equity Residential renamed to Vivmark Residential on
# 2026-08-12 and its balance sheets, all filed before that, now render under a
# name no reader recognises. SEC does not solve this either: companyfacts
# labels the identical facts with the new name.


def test_the_most_recent_former_name_is_the_one_kept():
    """Helix has two. The page has room for the one a reader might recognise,
    and the full history is one click away on EDGAR."""
    import datetime as dt

    from src.backfill import _latest_former_name

    name, until = _latest_former_name({"formerNames": [
        {"name": "CAL DIVE INTERNATIONAL INC",
         "from": "1996-09-04T04:00:00.000Z", "to": "2006-03-06T05:00:00.000Z"},
        {"name": "HELIX ENERGY SOLUTIONS GROUP INC",
         "from": "2006-03-09T05:00:00.000Z", "to": "2026-08-31T04:00:00.000Z"},
    ]})
    assert name == "HELIX ENERGY SOLUTIONS GROUP INC"
    assert until == dt.date(2026, 8, 31)


def test_a_company_that_never_renamed_records_nothing():
    from src.backfill import _latest_former_name

    assert _latest_former_name({}) == (None, None)
    assert _latest_former_name({"formerNames": []}) == (None, None)
    # An entry with no end date is a name still in force, not a former one.
    assert _latest_former_name(
        {"formerNames": [{"name": "X", "from": "2020-01-01", "to": ""}]}
    ) == (None, None)
    assert _latest_former_name(
        {"formerNames": [{"name": "X", "to": "not-a-date"}]}
    ) == (None, None)


def test_the_former_name_is_written_onto_the_row_the_page_reads(bf_db, monkeypatch):
    """Onto the ticker's EXISTING snapshot, never a fresh one at today's date.

    `save_universe` upserts on (as_of_date, ticker), so writing at a date the
    ticker has no row for inserts one whose name and cik are NULL -- and that
    row, being newest, is the one the company page reads. The page would lose
    the company's name to a change meant to show more of it.
    """
    import asyncio
    import datetime as dt

    from sqlalchemy import select

    from src import backfill
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed_company("EQR", name="VIVMARK RESIDENTIAL", cik="906107",
                  as_of=dt.date(2026, 9, 7))

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def fake_submissions(_client, cik):
        assert str(cik) == "906107"
        return {"formerNames": [
            {"name": "EQUITY RESIDENTIAL", "from": "2002-11-13T05:00:00.000Z",
             "to": "2026-08-12T04:00:00.000Z"},
        ]}

    monkeypatch.setattr(backfill.sec_edgar, "make_client", lambda **k: _FakeClient())
    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", fake_submissions)

    report = asyncio.run(backfill.backfill_former_names(rps=0))
    assert report["named"] == 1 and report["written"] == 1

    with session_scope() as s:
        rows = list(s.execute(
            select(UniverseSnapshot).where(UniverseSnapshot.ticker == "EQR")
        ).scalars())
    assert len(rows) == 1, "a second, nameless row was inserted"
    assert rows[0].as_of_date == dt.date(2026, 9, 7)
    assert rows[0].name == "VIVMARK RESIDENTIAL", "the current name was lost"
    assert rows[0].former_name == "EQUITY RESIDENTIAL"
    assert rows[0].former_name_until == dt.date(2026, 8, 12)


def test_a_rerun_does_not_re_ask_about_companies_already_recorded(bf_db, monkeypatch):
    import asyncio
    import datetime as dt

    from src import backfill

    _seed_company("EQR", name="VIVMARK RESIDENTIAL", cik="906107",
                  as_of=dt.date(2026, 9, 7))

    calls = []

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def fake_submissions(_client, cik):
        calls.append(str(cik))
        return {"formerNames": [
            {"name": "EQUITY RESIDENTIAL", "to": "2026-08-12T04:00:00.000Z"},
        ]}

    monkeypatch.setattr(backfill.sec_edgar, "make_client", lambda **k: _FakeClient())
    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", fake_submissions)

    asyncio.run(backfill.backfill_former_names(rps=0))
    asyncio.run(backfill.backfill_former_names(rps=0))
    assert calls == ["906107"], "the second run re-fetched a recorded company"

    # ...unless asked to look again, which is what a long gap needs.
    asyncio.run(backfill.backfill_former_names(rps=0, only_missing=False))
    assert calls == ["906107", "906107"]


def test_a_company_with_no_cik_is_skipped(bf_db, monkeypatch):
    import asyncio
    import datetime as dt

    from src import backfill

    _seed_company("GHOST", name="Ghost Inc", cik=None, as_of=dt.date(2026, 9, 7))

    async def boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("SEC was called for a company with no CIK")

    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", boom)
    report = asyncio.run(backfill.backfill_former_names(rps=0))
    assert report["checked"] == 0 and report["named"] == 0


def test_sec_re_recording_the_current_name_is_not_a_rename(bf_db, monkeypatch):
    """ADM, Cisco and Citigroup all carry a formerNames entry dated yesterday
    whose name is what they are still called. Storing those would put
    "formerly <the current name>" under hundreds of headings."""
    import asyncio
    import datetime as dt

    from src import backfill

    _seed_company("ADM", name="Archer-Daniels-Midland Co", cik="7084",
                  as_of=dt.date(2026, 9, 7))

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    async def fake_submissions(_client, _cik):
        return {"formerNames": [
            {"name": "Archer-Daniels-Midland Co", "to": "2026-09-11T04:00:00.000Z"},
        ]}

    monkeypatch.setattr(backfill.sec_edgar, "make_client", lambda **k: _FakeClient())
    monkeypatch.setattr(backfill.sec_edgar, "fetch_submissions", fake_submissions)

    report = asyncio.run(backfill.backfill_former_names(rps=0))
    assert report["checked"] == 1
    assert report["named"] == 0, "a same-name entry was stored as a rename"
