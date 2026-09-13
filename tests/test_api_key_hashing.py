"""API keys at rest: a digest, a prefix, and nothing that can be read back.

The thing under test is a NEGATIVE -- that the plaintext key is not in the
database and not in any response after the one that issued it. A negative is
easy to believe and hard to notice breaking, so each test here goes at the
storage or the payload directly rather than asserting that some helper was
called.

The earlier design stored the key in the clear and argued that it only guards
public SEC data. That argument weighed the data and not the leak: the same row
carries an email address, and the key is what spends somebody's paid quota.
"""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from src.api import app
from src.config.settings import get_settings
from src.storage.db import reset_engine_cache, session_scope
from src.storage.models import ApiUser

EMAIL = "hashing@example.com"
PASSWORD = "correct horse battery staple"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """https, because the session cookie is `Secure` and httpx drops it over
    plain http -- which would read here as "not signed in"."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'keys.db'}")
    monkeypatch.setenv("ADMIN_SECRET", "test-admin-secret-do-not-use")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-not-for-real-use")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "50")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    for gate in ("REGISTER", "LOGIN", "MAGIC_LINK", "RESEND"):
        monkeypatch.setenv(f"{gate}_RATE_PER_HOUR", "100")

    from src.storage.db import init_db

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import (
        _login_gate,
        _magic_link_gate,
        _register_gate,
        _resend_gate,
    )

    for gate in (_register_gate, _login_gate, _magic_link_gate, _resend_gate):
        gate.reset()

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


def stored_key_column(email=EMAIL) -> str:
    """The raw column value, read with SQL rather than through the ORM.

    Deliberately a SELECT and not `accounts.lookup`: the claim is about what
    is on disk, and a helper that hashes on the way in would make the test
    pass whatever the column held.
    """
    with session_scope() as s:
        return s.execute(
            text("SELECT api_key FROM api_users WHERE email = :e"), {"e": email}
        ).scalar_one()


# ---------------------------------------------------------------------------
# At rest
# ---------------------------------------------------------------------------

def test_api_key_stored_hashed(client):
    """The column holds the digest of the key, and never the key."""
    key = register(client)
    stored = stored_key_column()

    assert stored != key, "the key is in the database in the clear"
    assert key not in stored
    assert stored == hashlib.sha256(key.encode()).hexdigest()
    assert len(stored) == 64


def test_no_row_anywhere_contains_the_plaintext_key(client):
    """Not just the one column. A key copied into a second column, or left in
    the prefix, would satisfy the test above and still be a leak."""
    key = register(client)
    with session_scope() as s:
        row = s.execute(select(ApiUser).where(ApiUser.email == EMAIL)).scalar_one()
        values = [
            str(getattr(row, c.name)) for c in ApiUser.__table__.columns
        ]
    assert key not in values
    for value in values:
        assert key not in value, "the whole key is stored somewhere"


def test_the_prefix_is_stored_and_is_only_the_prefix(client):
    key = register(client)
    with session_scope() as s:
        row = s.execute(select(ApiUser).where(ApiUser.email == EMAIL)).scalar_one()
    assert row.api_key_prefix == key[:8]
    assert len(row.api_key_prefix) == 8, "a longer prefix is a shorter secret"


# ---------------------------------------------------------------------------
# What the browser is told
# ---------------------------------------------------------------------------

def test_api_key_prefix_shown(client):
    """`/api/auth/me` shows WHICH key, never the key."""
    r = client.post(
        "/api/auth/register-password",
        json={"email": EMAIL, "password": PASSWORD, "accept_terms": True},
    )
    assert r.status_code == 201, r.text
    key = r.json()["api_key"]

    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    payload = me.json()

    assert "api_key" not in payload, "the key is back in the session payload"
    assert key not in me.text
    assert payload["api_key_prefix"] == key[:8]
    # Present even when nothing has used the key: "never" is an answer, and a
    # missing field would read to the dashboard as a failed lookup.
    assert "api_key_last_used" in payload
    assert payload["api_key_last_used"] is None


def test_the_last_used_date_follows_real_calls(client):
    """"Last used" is what replaces the key on the dashboard, so it has to be
    real. It is read from the usage meter rather than written on every request:
    a `last_used_at` column would be one write per API call to answer a
    question asked once per page load."""
    from src import accounts

    key = client.post(
        "/api/auth/register-password",
        json={"email": EMAIL, "password": PASSWORD, "accept_terms": True},
    ).json()["api_key"]

    assert client.get("/api/auth/me").json()["api_key_last_used"] is None

    # `/api/user/status` is deliberately unmetered, so record a call the way a
    # real metered endpoint does rather than depending on seeded company data.
    account = accounts.lookup(key)
    accounts.record_call(account, "/api/company/JPM")

    assert client.get("/api/auth/me").json()["api_key_last_used"] is not None


def test_the_key_is_shown_once_and_says_so(client):
    """The old copy promised the opposite of what the code did."""
    r = client.post(
        "/api/auth/register", json={"email": EMAIL, "accept_terms": True}
    )
    note = r.json()["note"]
    assert "not be shown again" in note
    assert "regenerate" in note.lower()
    assert "not re-issued" not in note, "the copy that was false"


# ---------------------------------------------------------------------------
# It still has to work
# ---------------------------------------------------------------------------

def test_api_key_auth_still_works(client):
    """The whole point of a digest is that the key keeps authenticating."""
    key = register(client)
    r = client.get("/api/user/status", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == EMAIL

    # And the stored digest is not itself a usable credential -- which is the
    # failure mode of "hash it" done wrong.
    assert client.get(
        "/api/user/status", headers={"X-API-Key": stored_key_column()}
    ).status_code == 401


def test_a_key_with_stray_whitespace_still_authenticates(client):
    """Hashing makes this a real hazard: the digest of " k" and of "k" differ,
    so a key pasted with a trailing newline would 401 where it used to pass."""
    key = register(client)
    assert client.get(
        "/api/user/status", headers={"X-API-Key": f"  {key}  "}
    ).status_code == 200


def test_regenerate_invalidates_old(client):
    r = client.post(
        "/api/auth/register-password",
        json={"email": EMAIL, "password": PASSWORD, "accept_terms": True},
    )
    old = r.json()["api_key"]
    assert client.get(
        "/api/user/status", headers={"X-API-Key": old}
    ).status_code == 200

    new = client.post("/api/auth/regenerate-key").json()["api_key"]
    assert new != old

    assert client.get(
        "/api/user/status", headers={"X-API-Key": old}
    ).status_code == 401, "the old key outlived its replacement"
    assert client.get(
        "/api/user/status", headers={"X-API-Key": new}
    ).status_code == 200
    assert stored_key_column() == hashlib.sha256(new.encode()).hexdigest()
    with session_scope() as s:
        row = s.execute(select(ApiUser).where(ApiUser.email == EMAIL)).scalar_one()
    assert row.api_key_prefix == new[:8], "the dashboard would show the dead key"


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------

def test_the_migration_hashes_a_key_left_in_the_clear(client):
    """The upgrade path for rows written before this change.

    Written as a real plaintext row put back into the table, because that is
    exactly what production held and a mocked one proves nothing about the
    SQL.
    """
    from src.storage.db import get_engine, hash_plaintext_api_keys

    key = register(client)
    with session_scope() as s:
        s.execute(
            text("UPDATE api_users SET api_key = :k, api_key_prefix = NULL "
                 "WHERE email = :e"),
            {"k": key, "e": EMAIL},
        )
    assert stored_key_column() == key, "the fixture did not take"
    assert client.get(
        "/api/user/status", headers={"X-API-Key": key}
    ).status_code == 401, "a plaintext row must not authenticate after the change"

    changed = hash_plaintext_api_keys(get_engine())

    assert changed == 1
    assert stored_key_column() == hashlib.sha256(key.encode()).hexdigest()
    assert client.get(
        "/api/user/status", headers={"X-API-Key": key}
    ).status_code == 200, "the migration must not lock anybody out"


def test_the_migration_is_idempotent(client):
    """It runs on every boot. A second pass must not hash the digest again --
    that would silently destroy every key on the second deploy."""
    from src.storage.db import get_engine, hash_plaintext_api_keys

    key = register(client)
    first = stored_key_column()

    assert hash_plaintext_api_keys(get_engine()) == 0
    assert hash_plaintext_api_keys(get_engine()) == 0
    assert stored_key_column() == first
    assert client.get(
        "/api/user/status", headers={"X-API-Key": key}
    ).status_code == 200


def test_looks_hashed_cannot_mistake_a_live_key_for_a_digest(client):
    """The migration's entire safety rests on this predicate."""
    from src import accounts

    for _ in range(200):
        issued = accounts.generate_api_key()
        assert not accounts.looks_hashed(issued)
        assert accounts.looks_hashed(accounts.hash_api_key(issued))

    assert not accounts.looks_hashed("")
    assert not accounts.looks_hashed("f" * 63)
    assert not accounts.looks_hashed("g" * 64)
    assert accounts.looks_hashed("A" * 64 if False else "a" * 64)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def test_recovery_cannot_mail_a_key_it_does_not_have(client, monkeypatch):
    """`/api/auth/resend-key` sends a sign-in link now. It has no choice: there
    is no key to send. What it must NOT do is rotate the key, which would let
    anyone who knows an address break that account's integration on demand."""
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    get_settings.cache_clear()

    sent: list[tuple] = []
    monkeypatch.setattr(
        "src.mailer.send_recovery_link",
        lambda e, u, t=15: sent.append((e, u, t)) or True,
    )

    key = register(client)
    r = client.post("/api/auth/resend-key", json={"email": EMAIL})

    assert r.status_code == 200, r.text
    assert key not in r.text
    assert len(sent) == 1
    assert sent[0][0] == EMAIL
    assert key not in sent[0][1], "the link must not smuggle the key"
    assert client.get(
        "/api/user/status", headers={"X-API-Key": key}
    ).status_code == 200, "recovery must not revoke the key it cannot send"


def test_the_recovery_mail_body_carries_a_link_and_never_a_key():
    from src.mailer import _recovery_body

    body = _recovery_body("https://balanceproof.dev/auth/verify?token=abc", 15, "o@e.com")
    assert "https://balanceproof.dev/auth/verify?token=abc" in body
    assert "X-API-Key" not in body, "the old body pasted a live credential"
    assert "hashed" in body
    assert "Regenerate" in body
