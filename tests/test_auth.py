"""Magic-link login, sessions, and key rotation.

The whole feature is a credential exchange -- an email address becomes a cookie
becomes an API key -- so nearly everything asserted here is a boundary rather
than a happy path: what a prefetcher cannot spend, what a stale cookie cannot
open, what a leaked key cannot rotate, and what an unset secret must refuse.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

SESSION_SECRET = "test-session-secret-not-for-real-use"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "auth.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("BASE_URL", "https://example.test")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "5")
    monkeypatch.setenv("DEMO_API_KEY", "")

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

    # No real mail: the token is read out of the database instead.
    monkeypatch.setattr("src.mailer.send_magic_link", lambda *a, **k: True)

    # TestClient sends cookies over http; the session cookie is Secure.
    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def token_for(email: str) -> str:
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import MagicLink

    with session_scope() as s:
        row = s.execute(
            select(MagicLink)
            .where(MagicLink.email == email)
            .order_by(MagicLink.created_at.desc())
        ).scalars().first()
        return row.token if row else ""


def sign_in(client, email="user@example.com") -> str:
    r = client.post("/api/auth/magic-link", json={"email": email, "accept_terms": True})
    assert r.status_code == 200, r.text
    tok = token_for(email)
    assert tok
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 200
    return tok


# ---------------------------------------------------------------------------
# Requesting a link
# ---------------------------------------------------------------------------

def test_the_reply_is_the_same_for_known_and_unknown_addresses(client):
    """Otherwise the login box is an account-enumeration oracle."""
    client.post("/api/auth/register", json={"email": "known@example.com", "accept_terms": True})
    a = client.post("/api/auth/magic-link", json={"email": "known@example.com", "accept_terms": True})
    b = client.post("/api/auth/magic-link", json={"email": "stranger@example.com", "accept_terms": True})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_one_address_cannot_be_mailed_repeatedly(client):
    """A login link goes to an inbox the requester need not own."""
    for _ in range(4):
        assert client.post(
            "/api/auth/magic-link", json={"email": "target@example.com", "accept_terms": True}
        ).status_code == 200
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import MagicLink

    with session_scope() as s:
        n = s.execute(select(func.count()).select_from(MagicLink)).scalar_one()
    assert n == 1, "the cooldown must stop the second and later sends"


def test_the_demo_account_cannot_be_logged_into(client, monkeypatch):
    """It is a shared key with no inbox. A login would let anyone reach
    regenerate-key on it."""
    from src.config.settings import get_settings
    from src.demo import DEMO_EMAIL

    monkeypatch.setenv("DEMO_API_KEY", "demo-key-xyz")
    get_settings.cache_clear()
    assert client.post(
        "/api/auth/magic-link", json={"email": DEMO_EMAIL, "accept_terms": True}
    ).status_code == 200          # uniform reply...
    assert token_for(DEMO_EMAIL) == ""   # ...but no link was ever issued


# ---------------------------------------------------------------------------
# Spending a link
# ---------------------------------------------------------------------------

def test_a_link_survives_being_looked_at(client):
    """Mail scanners and prefetchers GET every URL in a message. If the GET
    consumed the token the recipient's own click would always arrive second, to
    an already-used link."""
    client.post("/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True})
    tok = token_for("user@example.com")

    for _ in range(3):   # three prefetchers
        assert client.get(f"/auth/verify?token={tok}").status_code == 200
    # The real click still works.
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 200


def test_a_link_works_exactly_once(client):
    client.post("/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True})
    tok = token_for("user@example.com")
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 200
    again = client.post("/api/auth/verify", json={"token": tok})
    assert again.status_code == 401
    assert "expired" in again.json()["detail"].lower()


def test_an_expired_link_is_refused(client):
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import MagicLink

    client.post("/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True})
    tok = token_for("user@example.com")
    with session_scope() as s:
        row = s.execute(
            select(MagicLink).where(MagicLink.token == tok)
        ).scalar_one()
        row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 401
    assert client.get(f"/auth/verify?token={tok}").status_code == 410


def test_a_garbage_token_is_refused(client):
    assert client.post(
        "/api/auth/verify", json={"token": "not-a-real-token"}
    ).status_code == 401


def test_verifying_creates_the_account_on_first_login(client):
    """A verified link proves the address, which is the bar registration asks
    for -- so arriving without an account is a signup, not an error."""
    from src import accounts

    assert accounts.by_email("brand.new@example.com") is None
    sign_in(client, "brand.new@example.com")
    account = accounts.by_email("brand.new@example.com")
    assert account is not None and account.tier == "free"


def test_the_no_javascript_form_post_also_signs_in(client):
    client.post("/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True})
    tok = token_for("user@example.com")
    r = client.post("/auth/verify", data={"token": tok}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert client.get("/api/auth/me").status_code == 200


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------

def test_the_session_cookie_is_locked_down(client):
    """This cookie can be exchanged for the account's API key at /api/auth/me,
    so it is a credential and has to be flagged like one."""
    client.post("/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True})
    r = client.post("/api/auth/verify", json={"token": token_for("user@example.com")})
    raw = r.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "secure" in raw
    assert "samesite=lax" in raw


def test_me_is_shut_without_a_session(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_the_account_but_never_the_key(client):
    """It shows WHICH key, not the key. The column is a SHA-256 digest, so
    there is nothing here that could be handed back even if it were wanted --
    and the point is that the session cookie is no longer equivalent to the
    credential it used to be exchangeable for."""
    sign_in(client)
    body = client.get("/api/auth/me").json()
    assert body["email"] == "user@example.com"
    assert body["tier"] == "free"
    assert body["calls_limit"] == 5
    assert "api_key" not in body
    assert len(body["api_key_prefix"]) == 8
    assert "api_key_last_used" in body


def test_a_forged_cookie_is_simply_not_signed_in(client):
    from src.auth import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, "totally.forged.value")
    assert client.get("/api/auth/me").status_code == 401


def test_logout_clears_the_session(client):
    sign_in(client)
    assert client.get("/api/auth/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_the_session_reflects_a_grant_immediately(client):
    """The cookie holds an address and nothing else, so tier is read fresh --
    an upgrade lands on the next page load, not when the cookie expires."""
    import os

    sign_in(client)
    os.environ["ADMIN_SECRET"] = "s3cret-for-this-test"
    from src.config.settings import get_settings

    get_settings.cache_clear()
    assert client.post(
        "/admin/grant-access",
        json={"email": "user@example.com", "action": "grant_pro"},
        headers={"X-Admin-Secret": "s3cret-for-this-test"},
    ).status_code == 200
    assert client.get("/api/auth/me").json()["tier"] == "pro"
    del os.environ["ADMIN_SECRET"]
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Rotating the key
# ---------------------------------------------------------------------------

def test_regenerate_replaces_the_key_and_kills_the_old_one(client):
    sign_in(client)
    # Two rotations rather than a read, because the key cannot be read back.
    old = client.post("/api/auth/regenerate-key").json()["api_key"]
    new = client.post("/api/auth/regenerate-key").json()["api_key"]
    assert new != old
    assert client.get(
        "/api/user/status", headers={"X-API-Key": old}
    ).status_code == 401
    assert client.get(
        "/api/user/status", headers={"X-API-Key": new}
    ).status_code == 200


def test_a_leaked_key_cannot_rotate_itself(client):
    """The reason to press regenerate is that the key leaked. A key that can
    rotate itself lets whoever holds it lock the owner out."""
    sign_in(client)
    key = client.post("/api/auth/regenerate-key").json()["api_key"]
    client.post("/api/auth/logout")
    r = client.post("/api/auth/regenerate-key", headers={"X-API-Key": key})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_login_is_dead_not_open_without_a_session_secret(client, monkeypatch):
    """An empty signing key would mint cookies anybody could forge, and this
    cookie can be traded for the account's API key."""
    from src.config.settings import get_settings

    monkeypatch.setenv("SESSION_SECRET", "")
    get_settings.cache_clear()

    assert client.post(
        "/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True}
    ).status_code == 503
    assert client.post(
        "/api/auth/verify", json={"token": "anything"}
    ).status_code == 503
    assert client.get("/api/auth/me").status_code == 401
    # The page says so rather than offering a dead form.
    assert "not configured" in client.get("/login").text


def test_login_says_so_when_email_is_not_configured(client, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    get_settings.cache_clear()
    r = client.post("/api/auth/magic-link", json={"email": "user@example.com", "accept_terms": True})
    assert r.status_code == 503
    assert "owner@example.com" in r.json()["message"]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def test_the_login_page_renders(client):
    html = client.get("/login").text
    assert "Send magic link" in html
    assert "/static/auth.js" in html


def test_the_dashboard_renders_all_four_tabs(client):
    html = client.get("/dashboard").text
    for tab in ("search", "api", "account", "billing"):
        assert f'data-tab="{tab}"' in html
        assert f'data-panel="{tab}"' in html
    assert 'href="/login"' in html


def test_the_emailed_link_points_at_base_url(client):
    from src import auth

    assert auth.link_url("abc").startswith("https://example.test/auth/verify?token=")


def test_hidden_panels_cannot_be_unhidden_by_a_display_rule():
    """`hidden` only carries `display:none` from the UA stylesheet, so any
    author `display` rule beats it. `.tabs{display:flex}` did exactly that and
    showed the signed-in dashboard to signed-out visitors."""
    from pathlib import Path

    css = (
        Path("src/report/static/dashboard.css").read_text(encoding="utf-8")
    )
    assert "[hidden]{display:none!important}" in css


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def test_dashboard_is_greyed_out_when_signed_out(client):
    """Present, visibly unavailable, and able to say why. A disabled <a> is not
    a thing -- an anchor without href is not focusable and one with
    aria-disabled is announced as a link that lies -- so it is a button."""
    for path in ("/", "/login"):
        html = client.get(path).text
        assert 'id="nav-dash-off"' in html, path
        assert 'aria-disabled="true"' in html, path
        assert "Log in to access your dashboard" in html, path
        assert 'href="/dashboard"' not in html.split("</nav>")[0], path


def test_dashboard_is_a_real_link_once_signed_in(client):
    sign_in(client)
    nav = client.get("/").text.split("</nav>")[0]
    assert 'href="/dashboard"' in nav
    assert 'id="nav-dash-off"' not in nav


def test_the_nav_shows_who_is_signed_in(client):
    sign_in(client)
    nav = client.get("/dashboard").text.split("</nav>")[0]
    assert "user@example.com" in nav
    assert "Log out" in nav
    assert 'id="signed-out"' in nav and 'hidden' in nav


def test_the_nav_is_rendered_by_the_server_not_fetched(client):
    """A bar that says "Sign in" for half a second to somebody who is signed in
    is worse than no bar, and it would be wrong forever with scripting off."""
    sign_in(client)
    # No JS has run in this client, yet the page already knows.
    assert "user@example.com" in client.get("/").text


def test_the_active_page_is_marked(client):
    assert 'class="brand on"' in client.get("/").text
    login_nav = client.get("/login").text.split("</nav>")[0]
    assert 'class="navlink on" href="/login"' in login_nav
    sign_in(client)
    dash_nav = client.get("/dashboard").text.split("</nav>")[0]
    assert 'class="navlink on" href="/dashboard"' in dash_nav


def test_the_collapse_cannot_strand_the_menu(client):
    """The toggle ships hidden and is revealed by nav.js. Collapsing with CSS
    and opening with JS would leave the menu shut forever if the script never
    arrives -- which on a phone is exactly when it does not."""
    html = client.get("/").text
    assert 'id="nav-toggle" hidden' in html
    css = __import__("pathlib").Path(
        "src/report/static/dashboard.css"
    ).read_text(encoding="utf-8")
    assert ".nav-js .navlinks{display:none" in css


def test_logout_works_without_javascript(client):
    sign_in(client)
    assert client.get("/api/auth/me").status_code == 200
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert client.get("/api/auth/me").status_code == 401


def test_the_nav_offers_no_sign_in_when_login_is_disabled(client, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("SESSION_SECRET", "")
    get_settings.cache_clear()
    nav = client.get("/").text.split("</nav>")[0]
    assert 'href="/login"' not in nav
    # ...but the Dashboard item still says what it is.
    assert 'id="nav-dash-off"' in nav


# ---------------------------------------------------------------------------
# Terms, privacy, and the acceptance gate
# ---------------------------------------------------------------------------

def test_the_legal_pages_render(client):
    for path, marker in (("/terms", "Terms of Service"),
                         ("/privacy", "Privacy Policy")):
        r = client.get(path)
        assert r.status_code == 200, path
        assert marker in r.text
        assert "Last updated" in r.text


def test_the_privacy_policy_describes_this_service_and_not_a_template(client):
    """A policy that names data flows a service does not have is not merely
    useless -- it is a false statement to users about where their data goes.
    Four claims here are corrections to the brief, and each must survive."""
    text = client.get("/privacy").text

    # Cards go to Stripe and nowhere else, so Stripe is NAMED as a processor
    # and linked -- a policy that hid the one third party handling money would
    # be the exact false statement this test exists to catch. The second claim
    # survives beside it because both are true: nothing about a card is posted
    # to this service or stored in its database.
    assert "Stripe" in text
    assert "https://stripe.com/privacy" in text
    assert "No payment information" in text

    # We store salted digests, never addresses. That is stronger AND true.
    assert "We do not store IP addresses" in text

    # API keys are NOT encrypted, and the policy must not pretend they are.
    assert "not encrypted or hashed" in text

    # The question box is the one place user-typed text leaves the service.
    assert "OpenRouter" in text


def test_the_terms_carry_the_clauses_that_matter(client):
    import re

    # Collapsed, because these are wrapped prose and a line break inside a
    # sentence is not a change to the sentence.
    text = re.sub(r"\s+", " ", client.get("/terms").text)
    assert "Not financial advice" in text
    assert "as-is" in text
    assert "Ontario" in text
    assert "18 years old" in text
    assert "non-refundable" in text
    # Liability capped at what was actually paid, which for a free account is 0.
    assert "the amount you have paid" in text and "that amount is zero" in text


def test_an_account_cannot_be_created_without_accepting(client):
    """The checkbox is enforced on the SERVER. These endpoints are public JSON
    and anyone can post to them without ever having seen the form."""
    r = client.post("/api/auth/register", json={"email": "nope@example.com"})
    assert r.status_code == 422
    assert "Terms of Service" in r.json()["detail"]

    r = client.post(
        "/api/auth/register-password",
        json={"email": "nope2@example.com", "password": "correct horse battery"},
    )
    assert r.status_code == 422

    from src import accounts

    assert accounts.by_email("nope@example.com") is None
    assert accounts.by_email("nope2@example.com") is None


def test_a_magic_link_signup_needs_acceptance_too(client):
    """The account is created when the link is CLICKED, so acceptance has to
    travel on the token -- there is no checkbox at that moment."""
    from src import accounts

    client.post("/api/auth/magic-link", json={"email": "unticked@example.com"})
    tok = token_for("unticked@example.com")
    r = client.post("/api/auth/verify", json={"token": tok})
    assert r.status_code == 422
    assert accounts.by_email("unticked@example.com") is None

    client.post(
        "/api/auth/magic-link",
        json={"email": "ticked@example.com", "accept_terms": True},
    )
    tok = token_for("ticked@example.com")
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 200
    assert accounts.by_email("ticked@example.com") is not None


def test_signing_in_again_does_not_re_ask(client):
    """You accepted at signup. A login form demanding it again is asking for
    consent it already has."""
    from src import accounts

    client.post(
        "/api/auth/magic-link",
        json={"email": "again@example.com", "accept_terms": True},
    )
    client.post("/api/auth/verify", json={"token": token_for("again@example.com")})
    client.post("/api/auth/logout")

    # No acceptance this time; the account exists, so it signs in fine.
    client.post("/api/auth/magic-link", json={"email": "again@example.com"})
    from src import auth

    auth.reset_cooldowns()
    client.post("/api/auth/magic-link", json={"email": "again@example.com"})
    tok = token_for("again@example.com")
    assert client.post("/api/auth/verify", json={"token": tok}).status_code == 200
    assert accounts.by_email("again@example.com") is not None


def test_acceptance_is_recorded_not_just_checked(client):
    """The value of asking is being able to say afterwards that it was asked
    and answered."""
    client.post(
        "/api/auth/register-password",
        json={"email": "rec@example.com", "password": "correct horse battery",
              "accept_terms": True},
    )
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    with session_scope() as s:
        user = s.execute(
            select(ApiUser).where(ApiUser.email == "rec@example.com")
        ).scalar_one()
        assert user.terms_accepted_at is not None


def test_every_page_carries_the_footer_links(client):
    for path in ("/", "/login", "/dashboard", "/terms", "/privacy"):
        html = client.get(path).text
        assert 'href="/terms"' in html, path
        assert 'href="/privacy"' in html, path
        assert "github.com" in html, path


def test_the_homepage_shows_the_disclaimer(client):
    html = client.get("/").text
    assert "Not financial advice" in html
    assert 'class="disclaim"' in html
