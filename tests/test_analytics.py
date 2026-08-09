"""The visitor counter, and what "accurate" has to mean for it.

Two separate promises are under test:

  1. Every qualifying request is recorded exactly once and survives a restart.
     No sampling, no in-memory counter, no rollup that discards the detail.
  2. Nothing is called something it is not. A count of distinct IP hashes is
     reported as addresses, never as people. Bot traffic is kept and shown
     rather than quietly dropped.

And one thing that must never happen: counting must not change, slow or break
the page being counted.
"""

from __future__ import annotations

import datetime as dt

import pytest

BROWSER = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari")


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'visits.db'}")
    monkeypatch.setenv("API_KEY", "")
    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from src import api

    with TestClient(api.app) as c:
        yield c


def _rows() -> list:
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import PageView

    with session_scope() as s:
        return list(s.execute(select(PageView)).scalars().all())


# ------------------------------------------------------------------- recording
def test_every_page_request_is_recorded_exactly_once(client):
    """No sampling. Five requests are five rows."""
    for _ in range(5):
        client.get("/", headers={"user-agent": BROWSER})

    rows = _rows()

    assert len(rows) == 5
    assert all(r.path == "/" for r in rows)
    assert all(r.is_bot is False for r in rows)


def test_the_admin_page_is_never_counted_as_a_visit(client):
    """/admin polls /admin.json every ten seconds while it is open. Counting
    that would make the operator most of the site's traffic."""
    client.get("/admin", headers={"user-agent": BROWSER})
    for _ in range(10):
        client.get("/admin.json", headers={"user-agent": BROWSER})

    assert _rows() == []


@pytest.mark.parametrize("path", [
    "/static/company.css", "/health", "/favicon.ico", "/api", "/status",
])
def test_assets_and_probes_are_not_visits(client, path):
    client.get(path, headers={"user-agent": BROWSER})

    assert _rows() == []


def test_a_company_page_records_which_company(client):
    """So "which company did people look at" stays answerable. A running
    total could never be re-cut this way."""
    client.get("/company/JPM", headers={"user-agent": BROWSER})
    client.get("/company/msft", headers={"user-agent": BROWSER})

    tickers = sorted(r.ticker for r in _rows())

    assert tickers == ["JPM", "MSFT"]


def test_a_404_is_still_a_visit(client):
    """Somebody looked. Counting only the successes would make the total
    disagree with the server log for no useful reason."""
    client.get("/company/NOSUCH", headers={"user-agent": BROWSER})

    rows = _rows()

    assert len(rows) == 1
    assert rows[0].status == 404


def test_the_address_is_hashed_not_stored(client):
    from src.analytics import hash_ip

    client.get("/", headers={"user-agent": BROWSER})
    row = _rows()[0]

    assert row.ip_hash != "testclient"
    assert len(row.ip_hash) == 32
    assert hash_ip("1.2.3.4") == hash_ip("1.2.3.4"), "stable, or uniques inflate"
    assert hash_ip("1.2.3.4") != hash_ip("1.2.3.5")


def test_the_proxy_is_not_mistaken_for_the_visitor(client):
    """On Railway every request arrives from the load balancer. Counting that
    address would report "1 unique visitor" forever -- not imprecise, wrong."""
    for ip in ("203.0.113.7", "203.0.113.8", "203.0.113.9"):
        client.get("/", headers={"user-agent": BROWSER, "x-forwarded-for": ip})

    hashes = {r.ip_hash for r in _rows()}

    assert len(hashes) == 3, "three visitors behind one proxy are three"


def test_the_leftmost_forwarded_address_is_the_client(client):
    """X-Forwarded-For grows left-to-right: client, then each proxy."""
    from src.analytics import client_ip, hash_ip

    assert client_ip({"x-forwarded-for": "9.9.9.9, 10.0.0.1, 10.0.0.2"},
                     "10.0.0.2") == "9.9.9.9"
    assert client_ip({"x-real-ip": "8.8.8.8"}, "10.0.0.2") == "8.8.8.8"
    assert client_ip({}, "10.0.0.2") == "10.0.0.2"
    assert client_ip({}, "") == "unknown"

    client.get("/", headers={"user-agent": BROWSER,
                             "x-forwarded-for": "9.9.9.9, 10.0.0.1"})
    assert _rows()[0].ip_hash == hash_ip("9.9.9.9")


# ------------------------------------------------------------------------ bots
@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "python-requests/2.31.0",
    "curl/8.4.0",
    "facebookexternalhit/1.1",
    "Mozilla/5.0 HeadlessChrome/120.0.0.0",
    None,  # no User-Agent at all is not a browser
])
def test_automated_clients_are_identified(ua):
    from src.analytics import is_bot

    assert is_bot(ua) is True


def test_a_real_browser_is_not_a_bot():
    from src.analytics import is_bot

    assert is_bot(BROWSER) is False


def test_bot_traffic_is_kept_and_reported_separately(client):
    """Dropping it would make the number impossible to reconcile against the
    server log; including it in "visitors" would be a lie."""
    from src.analytics import summary

    for _ in range(3):
        client.get("/", headers={"user-agent": BROWSER})
    for _ in range(7):
        client.get("/", headers={"user-agent": "Googlebot/2.1"})

    assert len(_rows()) == 10, "every request is stored, bot or not"

    s = summary()
    assert s["all_time"]["views"] == 3, "bots are not visitors"
    assert s["bot_views_all_time"] == 7, "and they are not hidden either"


# --------------------------------------------------------------------- summary
def _seed_view(when: dt.datetime, ip: str = "a", bot: bool = False,
               path: str = "/", ticker: str | None = None) -> None:
    from src.storage.db import session_scope
    from src.storage.models import PageView

    with session_scope() as s:
        s.add(PageView(created_at=when, path=path, ticker=ticker,
                       ip_hash=ip, is_bot=bot, status=200))


def test_windows_are_rolling_not_calendar(db):
    """A count that resets at midnight UTC looks like traffic collapsing every
    morning to anyone reading it from another timezone."""
    from src.analytics import summary

    now = dt.datetime(2026, 8, 5, 1, 0)  # 01:00 UTC — just after midnight
    _seed_view(now - dt.timedelta(hours=3))   # "yesterday" by the calendar
    _seed_view(now - dt.timedelta(hours=20))

    s = summary(now)

    assert s["windows"]["24h"]["views"] == 2


def test_uniques_count_addresses_not_requests(db):
    from src.analytics import summary

    now = dt.datetime(2026, 8, 5, 12, 0)
    for _ in range(6):
        _seed_view(now - dt.timedelta(minutes=5), ip="same")
    _seed_view(now - dt.timedelta(minutes=5), ip="other")

    s = summary(now)

    assert s["windows"]["24h"]["views"] == 7
    assert s["windows"]["24h"]["addresses"] == 2


def test_the_windows_nest_correctly(db):
    from src.analytics import summary

    now = dt.datetime(2026, 8, 5, 12, 0)
    _seed_view(now - dt.timedelta(hours=2))
    _seed_view(now - dt.timedelta(days=3))
    _seed_view(now - dt.timedelta(days=20))
    _seed_view(now - dt.timedelta(days=200))

    s = summary(now)

    assert s["windows"]["24h"]["views"] == 1
    assert s["windows"]["7d"]["views"] == 2
    assert s["windows"]["30d"]["views"] == 3
    assert s["all_time"]["views"] == 4


def test_the_most_viewed_companies_are_counted(db):
    from src.analytics import summary

    now = dt.datetime(2026, 8, 5, 12, 0)
    for _ in range(4):
        _seed_view(now, path="/company/JPM", ticker="JPM")
    _seed_view(now, path="/company/WMT", ticker="WMT")
    _seed_view(now, path="/company/JPM", ticker="JPM", bot=True)  # excluded

    top = summary(now)["top_tickers"]

    assert top[0] == {"ticker": "JPM", "views": 4}
    assert {"ticker": "WMT", "views": 1} in top


def test_the_summary_survives_an_unreachable_database(db, monkeypatch):
    """/admin renders with an error line rather than not rendering."""
    from src.analytics import summary

    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr("src.storage.db.session_scope", boom)

    assert "error" in summary()


# -------------------------------------------------- counting must not break it
def test_a_failing_write_never_breaks_the_page(client, monkeypatch):
    """The counter is the least important thing on the site."""
    import src.analytics as analytics

    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(analytics, "record", boom)
    r = client.get("/")

    assert r.status_code == 200
    assert "To Scale" in r.text


def test_the_count_is_written_after_the_response(client, monkeypatch):
    """It happens off the response path, so it cannot add latency a reader
    experiences or block the event loop."""
    import src.analytics as analytics

    seen = {}
    original = analytics.record

    def spy(path, ip, ua, **kw):
        seen["status"] = kw.get("status")
        return original(path, ip, ua, **kw)

    monkeypatch.setattr(analytics, "record", spy)
    client.get("/company/NOSUCH", headers={"user-agent": BROWSER})

    # The status is only knowable once the response exists.
    assert seen["status"] == 404


def test_the_admin_panel_reports_the_numbers(client):
    for _ in range(3):
        client.get("/", headers={"user-agent": BROWSER})

    panel = client.get("/admin.json").json()["visitors"]

    assert panel["all_time"]["views"] == 3
    assert panel["windows"]["24h"]["addresses"] == 1
    # Named for what it is, not for what it is not.
    assert "addresses" in panel["windows"]["24h"]
    assert "people" not in str(panel)
