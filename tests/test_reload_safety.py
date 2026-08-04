"""The reload must never destroy data it cannot replace.

This file exists because of a specific incident: the reload wiped the
fundamentals table, then discovered SEC was returning 429 on every quarter. It
deleted 1,231,927 rows and wrote 0. The guard at the time was on the *trigger*
(`confirm=true`) rather than on the *operation*, so it stopped stray taps and
did nothing at all about the actual failure mode.

So the invariants asserted here are behavioural, not structural:

  * a download failure leaves every existing row in place
  * a parse failure part-way through leaves every existing row in place
  * a reload that would write nothing leaves every existing row in place
  * a successful reload replaces the table wholesale

Plus the throttle handling that stopped us earning the 429 in the first place:
one shared disk cache, a floor between fetches, Retry-After honoured.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import zipfile

import pytest

from src.ingest import sec_cache


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'reload.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An isolated cache dir, and no memory of the last fetch's timestamp."""
    monkeypatch.setenv("SEC_CACHE_DIR", str(tmp_path / "sec-cache"))
    monkeypatch.setattr(sec_cache, "_last_fetch_at", 0.0)
    monkeypatch.setattr(sec_cache, "MIN_SECONDS_BETWEEN_FETCHES", 0.0)
    monkeypatch.setattr(sec_cache, "BASE_BACKOFF_SECONDS", 0.0)
    return sec_cache.cache_dir()


def _zip_bytes(num_rows: list[dict], sub_rows: list[dict] | None = None) -> bytes:
    sub_cols = ["adsh", "cik", "name", "form", "period", "filed", "fp"]
    num_cols = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom",
                "segments", "value"]
    sub_rows = sub_rows if sub_rows is not None else [
        {"adsh": "a1", "cik": "0000019617", "name": "JPMORGAN", "form": "10-K",
         "period": "20251231", "filed": "20260213", "fp": "FY"},
    ]

    def tsv(cols, rows):
        return "\n".join(
            ["\t".join(cols)]
            + ["\t".join(str(r.get(c, "")) for c in cols) for r in rows]
        )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub.txt", tsv(sub_cols, sub_rows))
        z.writestr("num.txt", tsv(num_cols, num_rows))
    # The cache rejects anything under 1KB as a truncated download.
    payload = buf.getvalue()
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as z:
        with zipfile.ZipFile(io.BytesIO(payload)) as src:
            for n in src.namelist():
                z.writestr(n, src.read(n))
        z.writestr("pad.txt", "x" * 4096)
    return buf2.getvalue()


def _num(**kw) -> dict:
    r = {"adsh": "a1", "version": "us-gaap/2025", "coreg": "", "segments": "",
         "ddate": "20251231", "qtrs": "0", "uom": "USD"}
    r.update(kw)
    return r


GOOD_ZIP = _zip_bytes([
    _num(tag="Assets", value="4424900000000"),
    _num(tag="Liabilities", value="4062462000000"),
    _num(tag="StockholdersEquity", value="362438000000"),
])
CIK_MAP = {"19617": "JPM"}


def _seed(n: int = 3) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        for i in range(n):
            s.add(Fundamental(
                ticker="OLD", metric=f"m{i}", value=float(i),
                period_end=dt.date(2020, 12, 31), fiscal_period="FY",
                filing_date=dt.date(2021, 2, 1), source="sec", restated=False,
            ))


def _count() -> int:
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        return s.execute(select(func.count()).select_from(Fundamental)).scalar_one()


# --------------------------------------------------------------------------
# prefetch: all-or-nothing, before anything is destroyed
# --------------------------------------------------------------------------
def test_prefetch_is_all_or_nothing(cache, monkeypatch):
    """The second quarter failing must raise, not return a partial list.

    A caller that got a partial list back would happily wipe on the strength of
    the quarters that did arrive.
    """
    calls: list[tuple[int, int]] = []

    async def fake_fetch(year, quarter, *, timeout=300.0, use_cache=True):
        calls.append((year, quarter))
        if quarter == 3:
            raise sec_cache.SECThrottled("429")
        return GOOD_ZIP

    monkeypatch.setattr(sec_cache, "fetch_dataset", fake_fetch)

    with pytest.raises(sec_cache.SECThrottled):
        asyncio.run(sec_cache.prefetch_quarters([(2026, 1), (2025, 4), (2025, 3)]))

    assert calls == [(2026, 1), (2025, 4), (2025, 3)]


def test_prefetch_skips_an_unpublished_quarter(cache, monkeypatch):
    """404 is not 429. SEC publishes a quarter some weeks after quarter end, so
    the newest one we ask for is routinely absent -- and must not block the six
    that do exist."""
    async def fake_fetch(year, quarter, *, timeout=300.0, use_cache=True):
        if (year, quarter) == (2026, 2):
            raise sec_cache.SECNotPublished("404")
        return GOOD_ZIP

    monkeypatch.setattr(sec_cache, "fetch_dataset", fake_fetch)
    staged = asyncio.run(sec_cache.prefetch_quarters([(2026, 2), (2026, 1)]))

    assert [s["source"] for s in staged] == ["unpublished", "network"]
    assert sec_cache.available_quarters(staged) == [(2026, 1)]


def test_a_404_is_not_retried(cache, monkeypatch):
    """An unpublished quarter cannot become published by asking four times."""
    slept = _stub_client(monkeypatch, [_Resp(404)])

    with pytest.raises(sec_cache.SECNotPublished):
        asyncio.run(sec_cache.fetch_dataset(2026, 2))

    assert slept == [], "a 404 must not spend the retry budget"
    assert not sec_cache.cache_path(2026, 2).exists()


def test_404_and_429_are_different_types(cache):
    """The whole point. One means skip, the other means stop -- and a caller
    that cannot tell them apart has to pick one and be wrong half the time."""
    assert not issubclass(sec_cache.SECNotPublished, sec_cache.SECThrottled)
    assert not issubclass(sec_cache.SECThrottled, sec_cache.SECNotPublished)


def test_prefetch_does_not_refetch_a_cached_quarter(cache, monkeypatch):
    """One cache, shared by the reload and the dump. This is the fix for the
    thing that earned the 429s: the same 100MB file downloaded twice."""
    sec_cache.cache_path(2026, 1).write_bytes(GOOD_ZIP)
    fetched: list[tuple[int, int]] = []

    async def fake_fetch(year, quarter, *, timeout=300.0, use_cache=True):
        fetched.append((year, quarter))
        return GOOD_ZIP

    monkeypatch.setattr(sec_cache, "fetch_dataset", fake_fetch)
    staged = asyncio.run(sec_cache.prefetch_quarters([(2026, 1), (2025, 4)]))

    assert fetched == [(2025, 4)], "a cached quarter must not be re-downloaded"
    assert [s["source"] for s in staged] == ["cache", "network"]


def test_prefetch_refetches_a_truncated_cached_file(cache, monkeypatch):
    """A half-written ZIP on disk is not 'obtainable'. Discovering that at parse
    time -- after the wipe -- is exactly the ordering this file forbids."""
    sec_cache.cache_path(2026, 1).write_bytes(b"not a zip" * 500)
    fetched: list[tuple[int, int]] = []

    async def fake_fetch(year, quarter, *, timeout=300.0, use_cache=True):
        fetched.append((year, quarter))
        return GOOD_ZIP

    monkeypatch.setattr(sec_cache, "fetch_dataset", fake_fetch)
    asyncio.run(sec_cache.prefetch_quarters([(2026, 1)]))

    assert fetched == [(2026, 1)]


# --------------------------------------------------------------------------
# 429 handling
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code: int, content: bytes = b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


def _stub_client(monkeypatch, responses: list[_Resp]) -> list[float]:
    """Patch httpx + sleep; returns the list every sleep duration lands in."""
    slept: list[float] = []
    queue = list(responses)

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return queue.pop(0)

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(sec_cache.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(sec_cache.asyncio, "sleep", fake_sleep)
    return slept


def test_429_is_retried_then_succeeds(cache, monkeypatch):
    slept = _stub_client(monkeypatch, [
        _Resp(429, headers={"Retry-After": "7"}),
        _Resp(200, GOOD_ZIP),
    ])
    data = asyncio.run(sec_cache.fetch_dataset(2026, 1))

    assert data == GOOD_ZIP
    assert 7.0 in slept, "a server-sent Retry-After must be honoured"
    assert sec_cache.cache_path(2026, 1).exists(), "a success must land in the cache"


def test_429_everywhere_raises_throttled_not_empty_bytes(cache, monkeypatch):
    """The caller must be able to tell 'throttled' from 'nothing to load'."""
    _stub_client(monkeypatch, [_Resp(429) for _ in range(sec_cache.MAX_ATTEMPTS)])

    with pytest.raises(sec_cache.SECThrottled):
        asyncio.run(sec_cache.fetch_dataset(2026, 1))


def test_absurd_retry_after_is_capped(cache, monkeypatch):
    slept = _stub_client(monkeypatch, [
        _Resp(429, headers={"Retry-After": "86400"}),
        _Resp(200, GOOD_ZIP),
    ])
    asyncio.run(sec_cache.fetch_dataset(2026, 1))

    assert max(slept) <= sec_cache.MAX_HONOURED_RETRY_AFTER


def test_a_cache_hit_makes_no_request(cache, monkeypatch):
    sec_cache.cache_path(2026, 1).write_bytes(GOOD_ZIP)

    def boom(**kw):
        raise AssertionError("a cached quarter must not touch the network")

    monkeypatch.setattr(sec_cache.httpx, "AsyncClient", boom)
    assert asyncio.run(sec_cache.fetch_dataset(2026, 1)) == GOOD_ZIP


# --------------------------------------------------------------------------
# the ordering itself
# --------------------------------------------------------------------------
def _patch_reload(monkeypatch, *, fetch=None, quarters=((2026, 1),)):
    """Wire reload_fundamentals to a synthetic quarter set with no network."""
    from src import backfill

    async def cik_map():
        return CIK_MAP

    monkeypatch.setattr(backfill, "_cik_to_ticker", cik_map)
    monkeypatch.setattr(
        backfill.sec_datasets, "recent_quarters", lambda as_of, n: list(quarters)
    )
    if fetch is not None:
        monkeypatch.setattr(sec_cache, "fetch_dataset", fetch)


def test_a_download_failure_deletes_nothing(db, cache, monkeypatch):
    """THE incident. SEC 429s, and the table must be exactly as it was."""
    from src import backfill

    async def throttled(year, quarter, *, timeout=300.0, use_cache=True):
        raise sec_cache.SECThrottled("SEC returned 429 for every attempt")

    _patch_reload(monkeypatch, fetch=throttled, quarters=[(2026, 1), (2025, 4)])
    _seed(3)

    with pytest.raises(sec_cache.SECThrottled):
        asyncio.run(backfill.reload_fundamentals(quarters=2))

    assert _count() == 3, "a throttled download must not cost a single row"
    state = backfill.get_reload_state()
    assert state["phase"] == "error"
    assert state["data_intact"] is True
    assert state["rows_deleted"] == 0
    assert "nothing was deleted" in state["last_error"]


def test_a_later_quarter_failing_rolls_the_delete_back(db, cache, monkeypatch):
    """Quarter 1 parses and writes, quarter 2 is corrupt. Because the delete and
    the writes share one transaction, the old rows survive."""
    from src import backfill

    sec_cache.cache_path(2026, 1).write_bytes(GOOD_ZIP)
    sec_cache.cache_path(2025, 4).write_bytes(GOOD_ZIP)
    _patch_reload(monkeypatch, quarters=[(2026, 1), (2025, 4)])
    _seed(3)

    real_parse = backfill.sec_datasets.parse_dataset
    seen = {"n": 0}

    def flaky_parse(zbytes):
        seen["n"] += 1
        if seen["n"] == 2:
            raise RuntimeError("num.txt is missing the dimensional columns")
        return real_parse(zbytes)

    monkeypatch.setattr(backfill.sec_datasets, "parse_dataset", flaky_parse)

    with pytest.raises(RuntimeError):
        asyncio.run(backfill.reload_fundamentals(quarters=2))

    assert _count() == 3, "a mid-reload failure must roll the delete back"
    assert {r for (r,) in _tickers()} == {"OLD"}
    state = backfill.get_reload_state()
    assert state["phase"] == "error"
    assert state["data_intact"] is True


def _tickers():
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        return s.execute(select(Fundamental.ticker)).all()


def test_a_reload_that_would_write_nothing_keeps_the_old_rows(db, cache, monkeypatch):
    """1.2M rows must not be swapped for zero. An empty extraction is a parser
    problem, and the answer to a parser problem is never 'commit anyway'."""
    from src import backfill

    empty = _zip_bytes([_num(tag="SomeTagWeDoNotMap", value="1")])
    sec_cache.cache_path(2026, 1).write_bytes(empty)
    _patch_reload(monkeypatch, quarters=[(2026, 1)])
    _seed(3)

    with pytest.raises(RuntimeError, match="0 rows"):
        asyncio.run(backfill.reload_fundamentals(quarters=1))

    assert _count() == 3
    assert backfill.get_reload_state()["data_intact"] is True


def test_a_successful_reload_replaces_the_table(db, cache, monkeypatch):
    from src import backfill

    sec_cache.cache_path(2026, 1).write_bytes(GOOD_ZIP)
    _patch_reload(monkeypatch, quarters=[(2026, 1)])
    _seed(3)

    out = asyncio.run(backfill.reload_fundamentals(quarters=1))

    assert out["rows_deleted"] == 3
    assert out["rows_written"] > 0
    assert {t for (t,) in _tickers()} == {"JPM"}, "the old rows are gone"
    state = backfill.get_reload_state()
    assert state["phase"] == "done"
    assert state["staged"] == [
        {"quarter": "2026q1", "year": 2026, "q": 1,
         "bytes": len(GOOD_ZIP), "source": "cache"}
    ]
    assert state["unpublished"] == []


def test_an_unpublished_newest_quarter_still_completes_the_reload(
    db, cache, monkeypatch
):
    """2026q2 404s and always has -- it is a month past quarter end. The reload
    must load the other six and call that done, not treat a quarter that does
    not exist as a reason to keep the old data."""
    from src import backfill

    requested = [(2026, 2), (2026, 1), (2025, 4)]
    fetched: list[tuple[int, int]] = []

    async def fetch(year, quarter, *, timeout=300.0, use_cache=True):
        fetched.append((year, quarter))
        if (year, quarter) == (2026, 2):
            raise sec_cache.SECNotPublished("2026q2 is not published yet (404)")
        sec_cache.cache_path(year, quarter).write_bytes(GOOD_ZIP)
        return GOOD_ZIP

    _patch_reload(monkeypatch, fetch=fetch, quarters=requested)
    _seed(3)

    out = asyncio.run(backfill.reload_fundamentals(quarters=3))

    assert fetched == requested, "the 404 must not stop the later quarters"
    assert out["quarters_loaded"] == 2
    assert out["unpublished"] == ["2026q2"]
    assert out["rows_written"] > 0
    assert {t for (t,) in _tickers()} == {"JPM"}

    state = backfill.get_reload_state()
    assert state["phase"] == "done", "6-of-7 for a 404 is a complete reload"
    assert state["quarters_available"] == 2
    assert state["quarters_requested"] == 3
    assert state["last_error"] is None


def test_every_quarter_unpublished_still_refuses_to_wipe(db, cache, monkeypatch):
    """If NOTHING is published the quarter arithmetic or the URL is wrong. That
    is not a reason to empty the table."""
    from src import backfill

    async def all_404(year, quarter, *, timeout=300.0, use_cache=True):
        raise sec_cache.SECNotPublished("404")

    _patch_reload(monkeypatch, fetch=all_404, quarters=[(2026, 2), (2026, 1)])
    _seed(3)

    with pytest.raises(RuntimeError, match="no published quarter"):
        asyncio.run(backfill.reload_fundamentals(quarters=2))

    assert _count() == 3
    assert backfill.get_reload_state()["data_intact"] is True


def test_a_429_after_a_404_still_aborts(db, cache, monkeypatch):
    """Skipping the unpublished quarter must not soften what happens next. The
    throttle still stops everything with the old rows in place."""
    from src import backfill

    async def fetch(year, quarter, *, timeout=300.0, use_cache=True):
        if (year, quarter) == (2026, 2):
            raise sec_cache.SECNotPublished("404")
        raise sec_cache.SECThrottled("429")

    _patch_reload(monkeypatch, fetch=fetch, quarters=[(2026, 2), (2026, 1)])
    _seed(3)

    with pytest.raises(sec_cache.SECThrottled):
        asyncio.run(backfill.reload_fundamentals(quarters=2))

    assert _count() == 3
    state = backfill.get_reload_state()
    assert state["data_intact"] is True
    assert state["rows_deleted"] == 0


def test_the_reload_downloads_before_it_deletes(db, cache, monkeypatch):
    """Ordering, asserted directly: at the moment the first fetch happens, every
    original row is still there."""
    from src import backfill

    counts_at_fetch: list[int] = []

    async def watching_fetch(year, quarter, *, timeout=300.0, use_cache=True):
        counts_at_fetch.append(_count())
        sec_cache.cache_path(year, quarter).write_bytes(GOOD_ZIP)
        return GOOD_ZIP

    _patch_reload(monkeypatch, fetch=watching_fetch, quarters=[(2026, 1), (2025, 4)])
    _seed(3)

    asyncio.run(backfill.reload_fundamentals(quarters=2))

    assert counts_at_fetch == [3, 3], (
        "every download must complete while the old data is still in place"
    )
