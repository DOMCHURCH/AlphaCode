"""The sign-in offer: who is shown it, and what decides when it comes back.

The offer is shown once and then not again until the reader has used the
service another hundred times. Two things make that harder than it sounds and
both are asserted here.

The COUNT is the server's, because a browser cannot see API or MCP traffic.
The DISMISSAL is the browser's, because it belongs to one person at one
keyboard. What must never happen is the third option -- a per-visitor decision
baked into the HTML -- because every reader-facing page is served from a
process-wide cache, so a panel rendered visible for one visitor would be handed
to the next one too. Hence: the markup is identical for everybody and hidden,
and only the endpoint varies.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

Q = dt.date(2025, 12, 31)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'prompt.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-the-signin-prompt")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import app

    # https, not http: the session cookie is `Secure`, so over plain http the
    # client refuses to store it and every signed-in assertion silently tests
    # the signed-out path instead.
    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def _seed_views(ip_hash: str, n: int, *, is_bot: bool = False) -> None:
    from src.storage.db import session_scope
    from src.storage.models import PageView

    with session_scope() as s:
        for i in range(n):
            s.add(PageView(path=f"/company/A{i}", ip_hash=ip_hash, is_bot=is_bot))


def _seed_demo_calls(ip_hash: str, n: int, *, day: str, source: str) -> None:
    from src.storage.db import session_scope
    from src.storage.models import DemoUsage

    with session_scope() as s:
        for _ in range(n):
            s.add(DemoUsage(ip_hash=ip_hash, day=day, ticker="AAA", source=source))


# ---------------------------------------------------------------------------
# The markup
# ---------------------------------------------------------------------------

def test_the_panel_ships_hidden_on_a_reader_facing_page(client):
    """Present in the HTML, and not visible until a script says so.

    `hidden` is the whole safety property. These pages are cached and shared,
    so the served bytes have to be the same for a first-time reader and for
    somebody who dismissed this an hour ago.
    """
    html = client.get("/").text

    assert 'id="signin-prompt"' in html
    wrap = html.split('id="signin-prompt"', 1)[1].split(">", 1)[0]
    assert "hidden" in wrap, "the offer must not render visible"
    assert 'id="sp-close"' in html, "there must be a way out"
    assert 'aria-label="Close"' in html


def test_the_panel_is_on_the_company_pages_too(client):
    """It rides on the nav, so it is wherever the nav is -- and the company
    pages are their own shell, which is exactly the kind of place a panel
    added page-by-page gets forgotten."""
    html = client.get("/company/AAA").text
    assert 'id="signin-prompt"' in html


@pytest.mark.parametrize("path", ["/login", "/dashboard"])
def test_the_panel_is_absent_where_it_would_be_absurd(client, path):
    """Offering a sign-in to somebody signing in, or already signed in."""
    html = client.get(path).text
    assert 'id="signin-prompt"' not in html, f"{path} must not carry the offer"


def test_the_panel_is_absent_when_login_is_switched_off(client, monkeypatch):
    """A deployment with no SESSION_SECRET has no door to advertise."""
    from src.config.settings import get_settings

    monkeypatch.setenv("SESSION_SECRET", "")
    get_settings.cache_clear()
    try:
        assert 'id="signin-prompt"' not in client.get("/").text
    finally:
        get_settings.cache_clear()


def test_the_offer_says_the_free_part_stays_free(client):
    """The site's entire argument is that reading it costs nothing. A panel
    that reads as a wall contradicts the page it is sitting on."""
    html = client.get("/").text
    sheet = html.split('id="signin-prompt"', 1)[1]
    assert "stops being free" in sheet or "free" in sheet
    assert "No card" in sheet


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

def test_the_endpoint_is_never_cached(client):
    """It is a different answer for every caller. A shared cache in front of
    this would hand one reader another reader's count."""
    r = client.get("/api/signin-prompt")
    assert r.status_code == 200
    assert "no-store" in r.headers.get("cache-control", "")


def test_a_signed_in_reader_is_never_offered_a_sign_in(client):
    r = client.post("/api/auth/register-password", json={
        "email": "member@example.com", "password": "a-long-enough-password-1",
        "accept_terms": True,
    })
    assert r.status_code == 201

    body = client.get("/api/signin-prompt").json()
    assert body["signed_in"] is True
    # Nothing to count and nothing to leak: the browser bails on this flag
    # before it looks at anything else.
    assert body["count"] == 0


def test_the_count_adds_the_website_the_api_and_mcp_together(client):
    """"A hundred fetches" is one number across three doors, which is the
    whole reason the count cannot live in the browser: a page has no idea how
    many MCP calls the same address has made."""
    from src.api import _caller_ip_hash

    class _Req:
        headers: dict[str, str] = {}

        class client:  # noqa: N801
            host = "testclient"

    ip_hash = _caller_ip_hash(_Req())

    _seed_views(ip_hash, 5)
    _seed_demo_calls(ip_hash, 3, day="2026-09-21", source="web")
    _seed_demo_calls(ip_hash, 2, day="2026-09-22", source="mcp")

    body = client.get("/api/signin-prompt").json()
    assert body["count"] == 10, body
    assert body["interval"] == 100


def test_demo_calls_are_counted_across_days_not_just_today(client):
    """`calls_today` resets at midnight, which is right for a rate limit and
    wrong here: a hundred calls that reset every night is not a hundred."""
    from src.demo import calls_all_time, calls_today

    _seed_demo_calls("deadbeef", 4, day="2020-01-01", source="web")
    _seed_demo_calls("deadbeef", 1, day="2020-01-02", source="mcp")

    assert calls_today("deadbeef", day="2020-01-01") == 4
    assert calls_all_time("deadbeef") == 5


def test_crawler_views_do_not_advance_the_count(client):
    """One bot on a shared office address would otherwise run the counter past
    the threshold by itself and make the offer reappear for everyone behind
    it."""
    from src.analytics import views_for

    _seed_views("cafe1234", 3, is_bot=False)
    _seed_views("cafe1234", 40, is_bot=True)

    assert views_for("cafe1234") == 3


def test_a_count_that_cannot_be_read_reports_zero_rather_than_failing(
    client, monkeypatch
):
    """Reported as zero, which leaves the offer waiting. Failing the other way
    would turn a database wobble into a panel nobody can escape."""
    from src import analytics

    def boom(_ip_hash: str) -> int:
        raise RuntimeError("the database is having a bad day")

    monkeypatch.setattr(analytics, "views_for", boom)

    r = client.get("/api/signin-prompt")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_asking_the_question_does_not_advance_the_answer(client):
    """/api is in `analytics.IGNORED_PREFIXES`, so the poll cannot count
    itself. Without this the count climbs on every page load and the offer
    returns on a schedule nobody chose."""
    from src.analytics import should_count

    assert should_count("/api/signin-prompt") is False
