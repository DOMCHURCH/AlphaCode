"""Clearing the page caches on demand, and automatically when data lands.

The caches exist because the home page was doing five balance-sheet reads and a
full-table count on every request. Their fifteen-minute TTLs are the fallback.
What this covers is the other half: the ingest knows the moment new rows land,
so waiting out a timer to show them is choosing to be wrong on purpose, on the
one page whose subject is how much data there is.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}
Q = dt.date(2025, 12, 31)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cache.db'}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import app

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def seed(n: int, *, start: int = 0) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        for i in range(start, start + n):
            ticker = f"T{i % 5:02d}"
            if start == 0 and i < 5:
                s.add(UniverseSnapshot(
                    as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Inc"
                ))
            s.add(Fundamental(
                ticker=ticker, metric=f"m{i}", value=float(i + 1), period_end=Q,
                fiscal_period="FY", filing_date=dt.date(2026, 2, 1),
                source="sec", restated=False,
            ))


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_the_cache_clear_endpoint_needs_the_admin_secret(client):
    r = client.post("/api/admin/cache/clear")
    assert r.status_code == 403, r.text


def test_it_is_a_post_not_a_get(client):
    """A GET would be followed by every prefetcher that sees the URL in a log,
    and this changes server state."""
    assert client.get("/api/admin/cache/clear", headers=ADMIN).status_code == 405


def test_clearing_makes_the_next_read_see_new_rows(client):
    """The property, end to end: the figure moves without waiting out a TTL."""
    seed(10)
    from src.dataset import facts_label

    before = facts_label()
    assert before, "the fixture must produce a countable table"

    seed(5_000, start=10)
    assert facts_label() == before, "a warm cache should still be serving the old count"

    r = client.post("/api/admin/cache/clear", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert set(r.json()["cleared"]) == {"counts", "identity", "panels", "row_count"}

    assert facts_label() != before, "the count did not move after a clear"


def test_a_failing_cache_does_not_stop_the_others(client, monkeypatch):
    """This runs at the end of a successful load. A load that succeeded must
    not be reported as failed because a module-level dict refused to reset."""
    from src.company import stats

    def boom() -> None:
        raise RuntimeError("this cache is having a bad day")

    monkeypatch.setattr(stats, "_clear_counts", boom)

    r = client.post("/api/admin/cache/clear", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["cleared"]["counts"] is False
    assert body["cleared"]["panels"] is True, "the others must still have cleared"


# ---------------------------------------------------------------------------
# The automatic half
# ---------------------------------------------------------------------------

def test_a_successful_load_clears_the_caches_itself(client, monkeypatch):
    """The endpoint is the manual path. This is the one that matters, because
    nobody is watching at 04:00 when the quarterly load lands."""
    import asyncio

    from src import scheduler
    from src.company import stats

    calls = {"n": 0}
    real = stats.clear_page_caches

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(stats, "clear_page_caches", counted)

    class _Outcome:
        status = "ok"
        rows = 1_200
        detail = "loaded a quarter"

    class _Job:
        name = "fundamentals"

        async def run(self, status):
            return _Outcome()

        def min_hours(self, s):
            return 6

        def wait_hours(self, s):
            return 1

    monkeypatch.setattr(scheduler, "_write_state", lambda *a, **k: None)
    asyncio.run(scheduler._attempt(_Job(), {"detail": "due"}, dt.datetime.now()))

    assert calls["n"] == 1, "a successful load with rows must clear the caches"


def test_a_load_that_changed_nothing_clears_nothing(client, monkeypatch):
    """The fundamentals sweep runs every six hours whether or not a quarter has
    landed. A job that succeeded and wrote no rows has invalidated nothing, and
    clearing on it would throw the caches away four times a day for free."""
    import asyncio

    from src import scheduler
    from src.company import stats

    calls = {"n": 0}
    monkeypatch.setattr(
        stats, "clear_page_caches", lambda: calls.__setitem__("n", calls["n"] + 1)
    )

    class _Outcome:
        status = "ok"
        rows = 0
        detail = "nothing new"

    class _Job:
        name = "fundamentals"

        async def run(self, status):
            return _Outcome()

        def min_hours(self, s):
            return 6

        def wait_hours(self, s):
            return 1

    monkeypatch.setattr(scheduler, "_write_state", lambda *a, **k: None)
    asyncio.run(scheduler._attempt(_Job(), {"detail": "due"}, dt.datetime.now()))

    assert calls["n"] == 0, "a zero-row load must not clear the caches"
