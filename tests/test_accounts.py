"""The paid-access path, end to end: register, meter, pay, download, revoke.

These run against a real SQLite file (not `:memory:`): the API opens its own
connections through `session_scope`, and an in-memory database is private to the
connection that made it, so a shared file is the only way the test client and
the assertions are looking at the same rows.

What is asserted here is mostly the REFUSALS, because that is where the money
is. A register endpoint that hands back an existing key, a limit that can be
walked past, a grant switch that works with no secret set -- each of those is a
one-line mistake that costs real access, and none of them shows up in a test
that only checks the happy path.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient over a throwaway on-disk database and a known admin secret."""
    db = tmp_path / "accounts.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "3")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "50")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import _register_gate, app
    from src.dataset import reset_count_cache

    # The registration gate is a module-level sliding window shared by every
    # test in the process. Left alone, the twentieth registration in an hour
    # fails and the failure lands in whichever test happened to be running.
    _register_gate.reset()
    # Same reason: the dashboard's fact count is memoised across tests, so one
    # test's seeded database would otherwise be reported on another's page.
    reset_count_cache()

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def register(client, email="buyer@example.com"):
    r = client.post("/api/auth/register", json={"email": email})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def grant(client, action, email="buyer@example.com", secret=ADMIN_SECRET):
    return client.post(
        "/admin/grant-access",
        json={"email": email, "action": action},
        headers={"X-Admin-Secret": secret},
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_returns_a_key_on_the_free_tier(client):
    body = client.post(
        "/api/auth/register", json={"email": "New.Buyer@Example.com "}
    ).json()
    assert body["tier"] == "free"
    assert body["email"] == "new.buyer@example.com"  # trimmed and lowercased
    assert len(body["api_key"]) >= 32


def test_registering_a_known_address_never_returns_the_existing_key(client):
    """The whole point of the 409. This endpoint takes no authentication, so if
    it answered with the key, anyone who knew a customer's address could take
    their paid access by typing it in."""
    first = register(client)
    again = client.post("/api/auth/register", json={"email": "buyer@example.com"})
    assert again.status_code == 409
    assert first not in again.text


def test_registration_itself_is_rate_limited(client, monkeypatch):
    """A per-key allowance is only worth what a key costs. Registration is open,
    so without this a script takes a hundred addresses and a thousand free
    calls, and the tier means nothing."""
    from src.api import _register_gate
    from src.config.settings import get_settings

    monkeypatch.setenv("REGISTER_RATE_PER_HOUR", "2")
    get_settings.cache_clear()
    _register_gate.reset()

    assert client.post("/api/auth/register", json={"email": "a@example.com"}).status_code == 201
    assert client.post("/api/auth/register", json={"email": "b@example.com"}).status_code == 201
    third = client.post("/api/auth/register", json={"email": "c@example.com"})
    assert third.status_code == 429
    assert "Retry-After" in third.headers


def test_a_malformed_address_is_refused(client):
    assert client.post("/api/auth/register", json={"email": "not-an-email"}).status_code == 422


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_the_api_is_shut_without_a_key(client):
    assert client.get("/api/user/status").status_code == 401
    assert client.get("/api/company/JPM").status_code == 401
    assert client.get("/api/download-dataset").status_code == 401


def test_an_unknown_key_is_401_not_500(client):
    r = client.get("/api/user/status", headers={"X-API-Key": "nonsense"})
    assert r.status_code == 401


def test_status_reports_the_tier_and_the_allowance(client):
    key = register(client)
    body = client.get("/api/user/status", headers={"X-API-Key": key}).json()
    assert body["tier"] == "free"
    assert body["has_paid_download"] is False
    assert body["calls_used_this_month"] == 0
    assert body["calls_limit"] == 3
    assert body["month"] == dt.datetime.now(dt.UTC).strftime("%Y-%m")


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------

def test_a_missing_ticker_is_not_billed(client):
    """A 404 must not spend an allowance. Billing for a lookup that returned
    nothing is how a 10-call tier becomes a 3-call tier for anybody whose first
    guesses at a ticker are wrong."""
    key = register(client)
    h = {"X-API-Key": key}
    assert client.get("/api/company/NOSUCHTICKER", headers=h).status_code == 404
    assert client.get("/api/user/status", headers=h).json()["calls_used_this_month"] == 0


def test_the_free_allowance_runs_out_and_then_429s(client, monkeypatch):
    """The limit is the product. Metered by recording a row per successful
    call, so this fakes success rather than loading a company: the thing under
    test is the counter and the gate, not the balance sheet."""
    from src import accounts

    key = register(client)
    account = accounts.lookup(key)
    for _ in range(3):
        accounts.record_call(account, "/api/company")

    h = {"X-API-Key": key}
    assert client.get("/api/user/status", headers=h).json()["calls_used_this_month"] == 3
    r = client.get("/api/company/JPM", headers=h)
    assert r.status_code == 429
    assert "limit" in r.json()["detail"].lower()

    # Reading your own status must still work with the allowance spent --
    # otherwise the one screen that explains the 429 is behind the 429.
    assert client.get("/api/user/status", headers=h).status_code == 200


def test_last_months_calls_do_not_count_against_this_month(client):
    from src import accounts
    from src.storage.db import session_scope
    from src.storage.models import UsageLog

    key = register(client)
    account = accounts.lookup(key)
    with session_scope() as s:
        for _ in range(5):
            s.add(UsageLog(
                user_id=account.id, endpoint_called="/api/company", month="1999-01"
            ))
    assert accounts.calls_this_month(account.id) == 0


def test_going_pro_raises_the_ceiling(client):
    from src import accounts

    key = register(client)
    account = accounts.lookup(key)
    for _ in range(3):
        accounts.record_call(account, "/api/company")

    h = {"X-API-Key": key}
    assert client.get("/api/company/JPM", headers=h).status_code == 429
    assert grant(client, "grant_pro").status_code == 200
    # Same spent calls, larger allowance: no longer over the line. 404 because
    # this database has no fundamentals in it -- what matters is that it is
    # past the gate.
    assert client.get("/api/company/JPM", headers=h).status_code == 404
    assert client.get("/api/user/status", headers=h).json()["calls_limit"] == 50


# ---------------------------------------------------------------------------
# The paid download
# ---------------------------------------------------------------------------

def test_the_download_is_402_until_it_is_paid_for(client):
    key = register(client)
    r = client.get("/api/download-dataset", headers={"X-API-Key": key})
    assert r.status_code == 402
    # The refusal has to say how to fix it, and to whom.
    assert "owner@example.com" in r.json()["detail"]


def test_granting_the_download_opens_it_and_revoking_shuts_it(client):
    key = register(client)
    h = {"X-API-Key": key}

    assert grant(client, "grant_download").status_code == 200
    assert client.get("/api/user/status", headers=h).json()["has_paid_download"] is True

    r = client.get("/api/download-dataset", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.splitlines()[0].startswith("ticker,metric,value")

    assert grant(client, "revoke_download").status_code == 200
    assert client.get("/api/download-dataset", headers=h).status_code == 402


def test_the_download_accepts_the_key_in_the_query_string(client):
    """Only this route does, and only so a browser can stream it to disk from a
    plain link instead of buffering a gigabyte in a tab."""
    key = register(client)
    grant(client, "grant_download")
    assert client.get(f"/api/download-dataset?api_key={key}").status_code == 200
    # ...and it is still refused elsewhere.
    assert client.get(f"/api/user/status?api_key={key}").status_code == 401


# ---------------------------------------------------------------------------
# The grant switch
# ---------------------------------------------------------------------------

def test_grant_access_needs_the_secret(client):
    register(client)
    assert client.post(
        "/admin/grant-access",
        json={"email": "buyer@example.com", "action": "grant_pro"},
    ).status_code == 403
    assert grant(client, "grant_pro", secret="wrong-secret").status_code == 403


def test_grant_access_fails_closed_when_no_secret_is_configured(client, monkeypatch):
    """An unset ADMIN_SECRET must kill the endpoint, not open it. If an empty
    configured secret were compared against an empty header they would match,
    and one forgotten environment variable would publish the grant switch."""
    from src.config.settings import get_settings

    monkeypatch.setenv("ADMIN_SECRET", "")
    get_settings.cache_clear()
    register(client)
    r = client.post(
        "/admin/grant-access",
        json={"email": "buyer@example.com", "action": "grant_pro"},
        headers={"X-Admin-Secret": ""},
    )
    assert r.status_code == 503


def test_grant_access_rejects_an_unknown_action_and_an_unknown_user(client):
    register(client)
    assert grant(client, "delete_everything").status_code == 400
    assert grant(client, "grant_pro", email="ghost@example.com").status_code == 404


def test_grant_access_is_idempotent(client):
    register(client)
    assert grant(client, "grant_download").status_code == 200
    second = grant(client, "grant_download")
    assert second.status_code == 200
    assert second.json()["user"]["has_paid_download"] is True


def test_every_grant_and_every_refusal_is_recorded(client):
    """The audit table is the only durable record that access was given in
    exchange for money -- container logs on this platform rotate away."""
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import AdminAction

    register(client)
    grant(client, "grant_pro")
    grant(client, "grant_pro", secret="wrong-secret")

    with session_scope() as s:
        rows = list(s.execute(select(AdminAction)).scalars())
    assert any(r.ok and r.action == "grant_pro" for r in rows)
    assert any(not r.ok for r in rows)


# ---------------------------------------------------------------------------
# The dashboard page
# ---------------------------------------------------------------------------

def test_the_dashboard_renders_with_the_configured_prices(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "owner@example.com" in r.text
    assert "$29" in r.text and "$49" in r.text
    assert "/static/dashboard.js" in r.text


def test_the_home_page_carries_the_accuracy_banner(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "78.6%" in r.text and "99.9%" in r.text
    assert 'href="/dashboard"' in r.text


def test_the_dashboard_does_not_break_out_of_its_script_tag(client, monkeypatch):
    """The admin address is interpolated into a <script> literal. A plain
    json.dumps leaves `</script>` intact, which would end the block early and
    spill the rest of the page into markup."""
    from src.report.dashboard_page import render_dashboard

    html = render_dashboard(admin_email='</script><script>alert(1)</script>')
    assert "</script><script>alert(1)" not in html
