"""Pro expiry, the renewal reminder, and password login.

Two features that both decide who gets what, so the tests are mostly about the
answers that must NOT differ: a lapsed subscription reading as free, a wrong
password reading exactly like an unknown address, and a renewal never costing
somebody the days they already paid for.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

ADMIN_SECRET = "test-admin-secret-do-not-use"
SESSION_SECRET = "test-session-secret-not-for-real-use"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "subs.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "10")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "10000")
    monkeypatch.setenv("PRO_PERIOD_DAYS", "31")
    monkeypatch.setenv("PRO_REMINDER_DAYS", "3")
    monkeypatch.setenv("DEMO_API_KEY", "")
    # Cheapest bcrypt the library allows: these tests hash a lot and the work
    # factor is what is being paid for, not what is being tested.
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src import auth
    from src.api import _magic_link_gate, _register_gate, app

    _register_gate.reset()
    _magic_link_gate.reset()
    auth.reset_cooldowns()
    auth.reset_login_attempts()
    monkeypatch.setattr("src.mailer.send_magic_link", lambda *a, **k: True)

    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def row(email: str):
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    with session_scope() as s:
        r = s.execute(select(ApiUser).where(ApiUser.email == email)).scalar_one()
        s.expunge(r)
        return r


def set_expiry(email: str, when: dt.datetime | None):
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    with session_scope() as s:
        r = s.execute(select(ApiUser).where(ApiUser.email == email)).scalar_one()
        r.pro_expires_at = when


def grant(client, action, email, secret=ADMIN_SECRET):
    return client.post(
        "/admin/grant-access",
        json={"email": email, "action": action},
        headers={"X-Admin-Secret": secret},
    )


def sign_in(client, email):
    from src import auth
    from src.storage.db import session_scope
    from src.storage.models import MagicLink

    client.post("/api/auth/magic-link", json={"email": email, "accept_terms": True})
    with session_scope() as s:
        tok = s.execute(
            select(MagicLink).where(MagicLink.email == email)
            .order_by(MagicLink.created_at.desc())
        ).scalars().first().token
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 200
    assert auth is not None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_a_null_expiry_means_comped_not_expired(client):
    """Every Pro account that existed before this column did has NULL. Reading
    that as "already expired" would have demoted all of them on deploy."""
    from src import accounts

    client.post("/api/auth/register", json={"email": "comped@example.com", "accept_terms": True})
    grant(client, "grant_pro", "comped@example.com")
    set_expiry("comped@example.com", None)

    account = accounts.by_email("comped@example.com")
    assert account.tier == "pro"
    assert account.lapsed is False
    assert account.days_remaining is None
    assert account.call_limit == 10_000


def test_a_lapsed_subscription_falls_back_to_free(client):
    from src import accounts

    client.post("/api/auth/register", json={"email": "lapsed@example.com", "accept_terms": True})
    grant(client, "grant_pro", "lapsed@example.com")
    set_expiry("lapsed@example.com", dt.datetime.now(dt.UTC) - dt.timedelta(days=2))

    account = accounts.by_email("lapsed@example.com")
    assert account.tier == "free"
    assert account.lapsed is True          # distinct from "never had Pro"
    assert account.call_limit == 10
    # The column is untouched: this is a lapse, not a downgrade.
    assert row("lapsed@example.com").subscription_tier == "pro"


def test_renewing_early_extends_rather_than_resets(client):
    """The one billing bug a paying customer notices: renewing three days early
    and losing those three days."""
    client.post("/api/auth/register", json={"email": "renew@example.com", "accept_terms": True})
    grant(client, "grant_pro", "renew@example.com")
    first = row("renew@example.com").pro_expires_at
    grant(client, "grant_pro", "renew@example.com")
    second = row("renew@example.com").pro_expires_at
    assert (second - first).days == 31


def test_renewing_late_starts_from_today(client):
    client.post("/api/auth/register", json={"email": "late@example.com", "accept_terms": True})
    grant(client, "grant_pro", "late@example.com")
    set_expiry("late@example.com", dt.datetime.now(dt.UTC) - dt.timedelta(days=40))
    grant(client, "grant_pro", "late@example.com")
    left = (
        row("late@example.com").pro_expires_at.replace(tzinfo=dt.UTC)
        - dt.datetime.now(dt.UTC)
    ).days
    assert 29 <= left <= 31, "a late renewal must not be eaten by the gap"


def test_revoke_backdates_rather_than_nulling(client):
    """NULL means "never expires", so nulling on revoke would UPGRADE them."""
    from src import accounts

    client.post("/api/auth/register", json={"email": "rev@example.com", "accept_terms": True})
    grant(client, "grant_pro", "rev@example.com")
    grant(client, "revoke_pro", "rev@example.com")
    assert row("rev@example.com").pro_expires_at is not None
    assert accounts.by_email("rev@example.com").tier == "free"


def test_status_reports_the_expiry(client):
    key = client.post(
        "/api/auth/register", json={"email": "st@example.com", "accept_terms": True}
    ).json()["api_key"]
    grant(client, "grant_pro", "st@example.com")
    body = client.get("/api/user/status", headers={"X-API-Key": key}).json()
    assert body["tier"] == "pro"
    assert body["calls_limit"] == 10_000
    assert body["days_remaining"] in (30, 31)
    assert body["expires_at"] is not None
    assert body["lapsed"] is False


# ---------------------------------------------------------------------------
# The subscription list and the reminder
# ---------------------------------------------------------------------------

def test_subscriptions_lists_pro_users_soonest_first(client):
    for name, days in (("a", 20), ("b", 2), ("c", 40)):
        client.post("/api/auth/register", json={"email": f"{name}@example.com", "accept_terms": True})
        grant(client, "grant_pro", f"{name}@example.com")
        set_expiry(f"{name}@example.com", dt.datetime.now(dt.UTC) + dt.timedelta(days=days))

    r = client.get("/admin/subscriptions", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r.status_code == 200
    emails = [s["email"] for s in r.json()["subscriptions"]]
    assert emails == ["b@example.com", "a@example.com", "c@example.com"]
    assert r.json()["count"] == 3


def test_subscriptions_needs_the_admin_secret(client):
    assert client.get("/admin/subscriptions").status_code == 403
    assert client.get(
        "/admin/subscriptions", headers={"X-Admin-Secret": "wrong"}
    ).status_code == 403


def test_lapsed_subscriptions_stay_in_the_list(client):
    """One that ran out yesterday is the most urgent row, not a row to hide."""
    client.post("/api/auth/register", json={"email": "old@example.com", "accept_terms": True})
    grant(client, "grant_pro", "old@example.com")
    set_expiry("old@example.com", dt.datetime.now(dt.UTC) - dt.timedelta(days=1))
    body = client.get(
        "/admin/subscriptions", headers={"X-Admin-Secret": ADMIN_SECRET}
    ).json()
    assert body["lapsed"] == 1
    assert body["subscriptions"][0]["days_remaining"] == 0


def test_the_reminder_fires_once_per_period(client):
    from src import accounts

    client.post("/api/auth/register", json={"email": "soon@example.com", "accept_terms": True})
    grant(client, "grant_pro", "soon@example.com")
    set_expiry("soon@example.com", dt.datetime.now(dt.UTC) + dt.timedelta(days=2))

    assert [d["email"] for d in accounts.expiring_soon(3)] == ["soon@example.com"]
    accounts.mark_reminded(["soon@example.com"])
    assert accounts.expiring_soon(3) == [], "one warning per period, not per run"

    # ...and a renewal re-arms it.
    grant(client, "grant_pro", "soon@example.com")
    assert row("soon@example.com").pro_reminder_sent_at is None


def test_a_subscription_outside_the_window_is_not_reported(client):
    from src import accounts

    client.post("/api/auth/register", json={"email": "far@example.com", "accept_terms": True})
    grant(client, "grant_pro", "far@example.com")
    set_expiry("far@example.com", dt.datetime.now(dt.UTC) + dt.timedelta(days=20))
    assert accounts.expiring_soon(3) == []


def test_the_reminder_job_is_off_not_broken_when_mail_is_unset(client, monkeypatch):
    """/admin renders JobState, so "switched off" must read differently from
    "failing every twelve hours"."""
    from src.config.settings import get_settings
    from src.scheduler import subscriptions_status

    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    get_settings.cache_clear()
    state = subscriptions_status(None, dt.date.today())
    assert state["due"] is False
    assert "not configured" in state["detail"]


def test_the_reminder_marks_only_after_a_successful_send(client, monkeypatch):
    """Marking first would lose the warning on any day the relay is down, and
    there is exactly one warning per period."""
    import asyncio

    from src import accounts
    from src.scheduler import _run_subscriptions

    client.post("/api/auth/register", json={"email": "fail@example.com", "accept_terms": True})
    grant(client, "grant_pro", "fail@example.com")
    set_expiry("fail@example.com", dt.datetime.now(dt.UTC) + dt.timedelta(days=1))

    monkeypatch.setattr("src.mailer.send_expiry_reminder", lambda *a, **k: False)
    outcome = asyncio.run(_run_subscriptions({}))
    assert outcome.status == "error"
    assert row("fail@example.com").pro_reminder_sent_at is None
    assert accounts.expiring_soon(3), "still due, so it will be retried"

    monkeypatch.setattr("src.mailer.send_expiry_reminder", lambda *a, **k: True)
    outcome = asyncio.run(_run_subscriptions({}))
    assert outcome.status == "ok" and outcome.rows == 1
    assert row("fail@example.com").pro_reminder_sent_at is not None


def test_the_reminder_is_one_email_for_everybody(client, monkeypatch):
    """Not one per subscriber -- that turns a two-minute job into an inbox."""
    import asyncio

    from src.scheduler import _run_subscriptions

    for n in ("x", "y", "z"):
        client.post("/api/auth/register", json={"email": f"{n}@example.com", "accept_terms": True})
        grant(client, "grant_pro", f"{n}@example.com")
        set_expiry(f"{n}@example.com", dt.datetime.now(dt.UTC) + dt.timedelta(days=1))

    sends = []
    monkeypatch.setattr(
        "src.mailer.send_expiry_reminder",
        lambda to, due: sends.append((to, len(due))) or True,
    )
    asyncio.run(_run_subscriptions({}))
    assert sends == [("owner@example.com", 3)]


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def test_register_with_a_password_signs_you_in(client):
    r = client.post(
        "/api/auth/register-password",
        json={"email": "pw@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    assert r.status_code == 201
    assert client.get("/api/auth/me").json()["has_password"] is True


def test_password_login_works(client):
    client.post(
        "/api/auth/register-password",
        json={"email": "pw@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    client.post("/api/auth/logout")
    r = client.post(
        "/api/auth/login",
        json={"email": "pw@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    assert r.status_code == 200
    assert client.get("/api/auth/me").json()["email"] == "pw@example.com"


def test_every_login_failure_looks_identical(client):
    """Wrong password, unknown address, and a magic-link-only account must be
    indistinguishable -- otherwise the error message is an account oracle."""
    client.post(
        "/api/auth/register-password",
        json={"email": "has@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    client.post("/api/auth/logout")
    client.post("/api/auth/register", json={"email": "nopw@example.com", "accept_terms": True})

    replies = [
        client.post("/api/auth/login",
                    json={"email": "has@example.com", "password": "wrong", "accept_terms": True}),
        client.post("/api/auth/login",
                    json={"email": "ghost@example.com", "password": "wrong", "accept_terms": True}),
        client.post("/api/auth/login",
                    json={"email": "nopw@example.com", "password": "wrong", "accept_terms": True}),
    ]
    assert {r.status_code for r in replies} == {401}
    assert len({r.text for r in replies}) == 1, "the bodies must not differ"


def test_registering_a_password_on_a_taken_address_is_refused(client):
    """Otherwise this endpoint hands over any account whose email is known."""
    client.post("/api/auth/register", json={"email": "taken@example.com", "accept_terms": True})
    r = client.post(
        "/api/auth/register-password",
        json={"email": "taken@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    assert r.status_code == 409
    assert row("taken@example.com").password_hash is None


def test_login_attempts_are_capped_per_address(client, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_HOUR", "3")
    get_settings.cache_clear()
    client.post(
        "/api/auth/register-password",
        json={"email": "brute@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    client.post("/api/auth/logout")
    for _ in range(3):
        assert client.post(
            "/api/auth/login",
            json={"email": "brute@example.com", "password": "no", "accept_terms": True},
        ).status_code == 401
    r = client.post(
        "/api/auth/login", json={"email": "brute@example.com", "password": "no", "accept_terms": True}
    )
    assert r.status_code == 429
    # The real password is refused too while the cap holds -- otherwise the cap
    # is only a speed bump for somebody who guesses right on attempt four.
    assert client.post(
        "/api/auth/login",
        json={"email": "brute@example.com", "password": "correct horse battery", "accept_terms": True},
    ).status_code == 429


def test_a_successful_login_clears_the_counter(client):
    from src import auth

    client.post(
        "/api/auth/register-password",
        json={"email": "clr@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"email": "clr@example.com", "password": "no", "accept_terms": True})
    client.post(
        "/api/auth/login",
        json={"email": "clr@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    assert auth.login_attempts_remaining("clr@example.com") == 5


def test_short_and_overlong_passwords_are_refused(client):
    short = client.post(
        "/api/auth/register-password",
        json={"email": "s@example.com", "password": "abc", "accept_terms": True},
    )
    assert short.status_code == 422
    # bcrypt truncates silently at 72 bytes; two passwords sharing a prefix
    # would otherwise be interchangeable and nobody would ever know.
    long = client.post(
        "/api/auth/register-password",
        json={"email": "l@example.com", "password": "x" * 100, "accept_terms": True},
    )
    assert long.status_code == 422
    assert "72" in long.json()["detail"]


def test_setting_a_first_password_needs_only_the_session(client):
    sign_in(client, "link@example.com")
    assert client.get("/api/auth/me").json()["has_password"] is False
    r = client.post("/api/auth/set-password", json={"password": "correct horse battery"})
    assert r.status_code == 200
    assert client.get("/api/auth/me").json()["has_password"] is True


def test_changing_a_password_needs_the_current_one(client):
    """A session can be left open on a shared machine, and a password change is
    the one action that locks the real owner out."""
    client.post(
        "/api/auth/register-password",
        json={"email": "ch@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    bad = client.post(
        "/api/auth/change-password",
        json={"current_password": "wrong", "new_password": "another good one"},
    )
    assert bad.status_code == 401
    ok = client.post(
        "/api/auth/change-password",
        json={"current_password": "correct horse battery",
              "new_password": "another good one"},
    )
    assert ok.status_code == 200
    client.post("/api/auth/logout")
    assert client.post(
        "/api/auth/login",
        json={"email": "ch@example.com", "password": "another good one", "accept_terms": True},
    ).status_code == 200


def test_password_endpoints_are_shut_without_a_session(client):
    assert client.post(
        "/api/auth/set-password", json={"password": "correct horse battery"}
    ).status_code == 401
    assert client.post(
        "/api/auth/change-password",
        json={"current_password": "a", "new_password": "correct horse battery"},
    ).status_code == 401


def test_forgot_password_sends_a_magic_link(client):
    """Recovery reuses the link flow -- one token system, not two."""
    from src.storage.db import session_scope
    from src.storage.models import MagicLink

    client.post(
        "/api/auth/register-password",
        json={"email": "forgot@example.com", "password": "correct horse battery", "accept_terms": True},
    )
    client.post("/api/auth/logout")
    r = client.post("/api/auth/forgot-password", json={"email": "forgot@example.com", "accept_terms": True})
    assert r.status_code == 200
    with session_scope() as s:
        assert s.execute(
            select(MagicLink).where(MagicLink.email == "forgot@example.com")
        ).scalars().first() is not None


def test_magic_link_users_keep_working(client):
    """Passwords are an alternative, never a requirement."""
    sign_in(client, "linkonly@example.com")
    body = client.get("/api/auth/me").json()
    assert body["email"] == "linkonly@example.com"
    assert body["has_password"] is False


# ---------------------------------------------------------------------------
# UX
# ---------------------------------------------------------------------------

def test_the_homepage_plan_buttons_go_to_sign_in(client):
    """The dashboard shows an account; somebody without one needs the step
    before that."""
    html = client.get("/").text
    pricing = html[html.index('id="pricing"'):]
    pricing = pricing[: pricing.index("</section>")]
    assert 'class="plan-cta" href="/login"' in pricing
    assert 'class="plan-cta" href="/dashboard"' not in pricing


def test_the_dashboard_carries_the_new_sections(client):
    html = client.get("/dashboard").text
    for marker in ("quickstart", "compare", "pw-box", "grant-banner",
                   "plan-summary", "tablewrap"):
        assert marker in html, marker


def test_the_comparison_table_reads_from_settings(client):
    """The table compares the DATASET against the API, not Free against Pro.

    Free-vs-Pro was the wrong axis: it priced two tiers of the same product
    and left the reader believing the $79.99 file and the $49 subscription
    were the same thing bought two ways. The row that matters is freshness.
    """
    html = client.get("/dashboard").text
    table = html[html.index('class="compare'):]
    table = table[: table.index("</table>")]

    assert "$79.99" in table and "$49" in table
    assert "10k" in table
    assert "Static (as of download date)" in table
    assert "Live (updates daily)" in table


def test_the_login_page_offers_both_ways_in(client):
    html = client.get("/login").text
    assert 'data-logintab="link"' in html
    assert 'data-logintab="password"' in html
    assert 'id="forgot-btn"' in html
