"""The account endpoints, over HTTP, as a caller actually reaches them.

This file was 0 bytes. `test_accounts.py` covers the account MODULE; nothing
covered the twelve `/api/auth/*` routes in front of it, which is where the
security-relevant decisions live -- what a stranger is allowed to learn, what a
key may do, and what stops a script.

Three properties get most of the attention here, because each one is a way the
system leaks or gets abused rather than merely a way it breaks:

* **No route may be an account oracle.** Registration, key recovery and magic
  links all answer the same way whether or not the address exists. A different
  reply for a registered address turns an open endpoint into a way to test
  whether somebody is a customer.
* **A key is not a session.** The API key reads balance sheets. It must not
  rotate itself, and it must not reach anything that changes the account --
  the reason to press "regenerate" is that the key has leaked, and a leaked key
  that can rotate itself locks the owner out.
* **Every open POST is bounded**, per-address and globally, because the
  per-address cap does not bind a caller who varies the address.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
SESSION_SECRET = "test-session-secret-not-for-real-use"
PASSWORD = "correct horse battery staple"
EMAIL = "reader@example.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """https, because the session cookie is `Secure`.

    Over plain http httpx drops it silently and every signed-in assertion in
    this file would read as "not signed in" -- which looks like a broken
    endpoint and is a cookie that was never stored.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'accounts.db'}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    # A value, not blank: the magic-link routes answer 503 when mail is
    # unconfigured, and what these tests assert is the ROUTE behaviour, not
    # whether a relay exists. Nothing is actually sent -- `_client()` never
    # resolves an inbox against a fake key.
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "5")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "5000")
    # Four rounds, not twelve. These tests spend a lot of bcrypt and the work
    # factor is not what any of them are asserting.
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("REGISTER_RATE_PER_HOUR", "100")
    monkeypatch.setenv("LOGIN_RATE_PER_HOUR", "100")
    monkeypatch.setenv("MAGIC_LINK_RATE_PER_HOUR", "100")
    monkeypatch.setenv("RESEND_RATE_PER_HOUR", "100")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import (
        _login_gate,
        _magic_link_gate,
        _register_gate,
        _resend_gate,
        app,
    )

    for gate in (_register_gate, _login_gate, _magic_link_gate, _resend_gate):
        gate.reset()

    # Magic-link cooldowns are per-address module state and outlive the
    # database, so without this an address used by an earlier test in the same
    # session is silently on cooldown here and `create_link` returns None.
    from src import auth

    auth.reset_cooldowns()

    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def register(client, email=EMAIL):
    r = client.post(
        "/api/auth/register", json={"email": email, "accept_terms": True}
    )
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def sign_up_with_password(client, email=EMAIL, password=PASSWORD):
    r = client.post(
        "/api/auth/register-password",
        json={"email": email, "password": password, "accept_terms": True},
    )
    assert r.status_code == 201, r.text
    return r


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registration_creates_an_account_and_returns_a_key(client):
    key = register(client)
    assert len(key) > 20, "a key short enough to guess is not a key"

    body = client.get("/api/user/status", headers={"X-API-Key": key}).json()
    assert body["email"] == EMAIL
    assert body["tier"] == "free"
    assert body["calls_limit"] == 5


def test_registering_a_taken_address_never_returns_the_existing_key(client):
    """The whole security model of an open registration endpoint.

    Returning the existing key would make this a key-recovery oracle: anyone
    who guessed a customer's address would be handed that customer's paid key.
    """
    first = register(client)
    r = client.post(
        "/api/auth/register", json={"email": EMAIL, "accept_terms": True}
    )
    assert r.status_code == 409, r.text
    assert first not in r.text
    assert "api_key" not in r.json()


def test_a_malformed_address_is_refused_before_anything_is_created(client):
    for bad in ("not-an-address", "@example.com", "a@b", "", " "):
        r = client.post(
            "/api/auth/register", json={"email": bad, "accept_terms": True}
        )
        assert r.status_code in (400, 422), f"{bad!r} was accepted: {r.text}"


def test_registration_is_capped_globally(client, monkeypatch):
    """Each key carries a free monthly allowance, so the per-key limit is only
    worth as much as a key costs to obtain. This is what makes it cost."""
    from src.api import _register_gate

    _register_gate.reset()
    monkeypatch.setenv("REGISTER_RATE_PER_HOUR", "2")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    codes = [
        client.post(
            "/api/auth/register",
            json={"email": f"n{i}@example.com", "accept_terms": True},
        ).status_code
        for i in range(4)
    ]
    assert codes[:2] == [201, 201], codes
    assert codes[2] == 429, codes
    assert codes[3] == 429, codes

    _register_gate.reset()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def test_signing_up_with_a_password_signs_you_in(client):
    sign_up_with_password(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["email"] == EMAIL
    assert me.json()["has_password"] is True


def test_the_right_password_signs_you_in_and_the_wrong_one_does_not(client):
    sign_up_with_password(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401

    bad = client.post(
        "/api/auth/login", json={"email": EMAIL, "password": "not the password"}
    )
    assert bad.status_code == 401, bad.text
    assert client.get("/api/auth/me").status_code == 401

    good = client.post(
        "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
    )
    assert good.status_code == 200, good.text
    assert client.get("/api/auth/me").json()["email"] == EMAIL


def test_every_login_failure_reads_the_same(client):
    """Wrong password, unknown address, and an account that only ever used a
    magic link must be indistinguishable, or the error message is an oracle."""
    sign_up_with_password(client, "has-one@example.com")
    register(client, "link-only@example.com")
    client.post("/api/auth/logout")

    replies = {
        client.post(
            "/api/auth/login", json={"email": e, "password": "wrong password"}
        ).text
        for e in ("has-one@example.com", "link-only@example.com", "ghost@example.com")
    }
    assert len(replies) == 1, f"the three cases are distinguishable: {replies}"


def test_password_attempts_are_capped_per_address(client, monkeypatch):
    """Counted per ADDRESS rather than per IP: stuffing one account comes from
    many addresses, and an IP cap locks out everyone behind one office."""
    monkeypatch.setenv("LOGIN_ATTEMPTS_PER_HOUR", "3")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    sign_up_with_password(client)
    client.post("/api/auth/logout")

    codes = [
        client.post(
            "/api/auth/login", json={"email": EMAIL, "password": "wrong"}
        ).status_code
        for _ in range(5)
    ]
    assert 429 in codes, codes
    # And the correct password is refused too while the account is locked --
    # otherwise the cap is only an inconvenience to a script.
    assert client.post(
        "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 429

    get_settings.cache_clear()


def test_changing_a_password_requires_the_current_one(client):
    sign_up_with_password(client)

    wrong = client.post(
        "/api/auth/change-password",
        json={"current_password": "not it", "new_password": "a whole new thing"},
    )
    assert wrong.status_code in (401, 403), wrong.text

    client.post("/api/auth/logout")
    assert client.post(
        "/api/auth/login", json={"email": EMAIL, "password": PASSWORD}
    ).status_code == 200, "the old password must still work after a failed change"


# ---------------------------------------------------------------------------
# Magic links
# ---------------------------------------------------------------------------

def test_a_magic_link_answers_the_same_for_a_stranger_as_for_a_customer(client):
    """A different reply for a registered address is an account oracle on an
    endpoint anybody can post to."""
    register(client)

    known = client.post("/api/auth/magic-link", json={"email": EMAIL})
    unknown = client.post(
        "/api/auth/magic-link", json={"email": "nobody@example.com"}
    )
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


def test_a_magic_link_token_signs_you_in_exactly_once(client):
    """The token is spent on POST /api/auth/verify, not on the GET the emailed
    link lands on -- mail scanners issue a GET on every URL in a message, so a
    GET that consumed it would mean the user's own click always arrived second.
    """
    from src import auth

    register(client)
    token = auth.create_link(EMAIL, accept_terms=True)
    assert token, "a fresh address should not be on cooldown"
    assert auth.peek_token(token) == EMAIL, "peeking must not spend it"

    first = client.post("/api/auth/verify", json={"token": token})
    assert first.status_code == 200, first.text
    assert client.get("/api/auth/me").json()["email"] == EMAIL

    client.post("/api/auth/logout")
    again = client.post("/api/auth/verify", json={"token": token})
    assert again.status_code == 401, "a spent token must not sign anybody in twice"


def test_a_forged_token_is_refused(client):
    register(client)
    r = client.post("/api/auth/verify", json={"token": "not-a-real-token"})
    assert r.status_code == 401, r.text
    assert client.get("/api/auth/me").status_code == 401


def test_forgotten_passwords_go_through_the_same_machinery(client):
    """One token type, one expiry, one set of edge cases -- and therefore no
    separate reset page that has to be secured all over again."""
    sign_up_with_password(client)
    client.post("/api/auth/logout")

    r = client.post("/api/auth/forgot-password", json={"email": EMAIL})
    assert r.status_code == 200, r.text
    unknown = client.post(
        "/api/auth/forgot-password", json={"email": "ghost@example.com"}
    )
    assert unknown.json() == r.json(), "and it must not be an oracle either"


# ---------------------------------------------------------------------------
# Keys, and what a key may not do
# ---------------------------------------------------------------------------

def test_the_api_key_cannot_rotate_itself(client):
    """The reason to press regenerate is that the key has leaked. A leaked key
    that can rotate itself locks the owner out of their own account."""
    key = register(client)

    r = client.post("/api/auth/regenerate-key", headers={"X-API-Key": key})
    assert r.status_code == 401, r.text
    assert client.get(
        "/api/user/status", headers={"X-API-Key": key}
    ).status_code == 200, "and the original key must still work"


def test_a_signed_in_reader_can_rotate_their_key_and_the_old_one_dies(client):
    # From the signup response, which is the only place the key is ever shown.
    # `/api/auth/me` cannot hand it over: the database holds a digest.
    old = sign_up_with_password(client).json()["api_key"]

    r = client.post("/api/auth/regenerate-key")
    assert r.status_code == 200, r.text
    new = r.json()["api_key"]
    assert new != old

    assert client.get(
        "/api/user/status", headers={"X-API-Key": old}
    ).status_code == 401, "the leaked key must stop working"
    assert client.get(
        "/api/user/status", headers={"X-API-Key": new}
    ).status_code == 200


def test_an_unknown_or_missing_key_is_refused(client):
    assert client.get("/api/user/status").status_code == 401
    assert client.get(
        "/api/user/status", headers={"X-API-Key": "nonsense"}
    ).status_code == 401


def test_the_meter_can_be_read_without_moving_it(client):
    """A meter you cannot read without spending a call is not a meter."""
    key = register(client)
    headers = {"X-API-Key": key}

    for _ in range(3):
        assert client.get("/api/user/status", headers=headers).status_code == 200
    assert client.get(
        "/api/user/status", headers=headers
    ).json()["calls_used_this_month"] == 0


def test_signing_out_ends_the_session_but_not_the_key(client):
    key = sign_up_with_password(client).json()["api_key"]

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get(
        "/api/user/status", headers={"X-API-Key": key}
    ).status_code == 200, "signing out of a browser must not revoke an API key"
