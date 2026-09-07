"""Dashboard login: a magic link or a password, and a signed cookie either way.

Magic links came first and remain the path that needs nothing set up. Passwords
were added because signing in should not require going to another application
and back, which is a real cost every time and not a theoretical one.

They share one session and one reset story. There is no password-reset token:
"forgot password" sends a magic link, and setting a new password is something
you do once signed in. One token system with one expiry beats two that have to
be kept in agreement.

A password is optional forever. `password_hash` is nullable and nothing here
requires it, so an account that only ever uses links stays fully usable.

Three separate authentications now exist and they are deliberately not the same:

* **The API key** authenticates machines, on `X-API-Key`, per request. Unchanged.
* **The session cookie** authenticates a person looking at `/dashboard`. It is
  what makes the dashboard able to show you your own key instead of asking you
  to paste it.

The cookie carries only an email address, signed. Everything else is read from
`api_users` at request time, so a tier change or a revoked key takes effect on
the next page load rather than whenever the cookie happens to expire.

`SESSION_SECRET` unset means login is DEAD, not open -- an empty signing key
would produce cookies anybody could forge, and this cookie can be exchanged for
the account's API key.
"""

from __future__ import annotations

import datetime as dt
import secrets
import time

import structlog
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, select

from src.config.settings import get_settings
from src.storage.db import session_scope
from src.storage.models import ApiUser, MagicLink

log = structlog.get_logger(__name__)

COOKIE_NAME = "toscale_session"
_SALT = "to-scale-session-v1"

# Per-address cooldown, in process memory. Unlike the call meter this need not
# survive a restart: the worst a bounce buys is one extra email per container,
# which is noise beside the global hourly gate in front of it.
_last_sent: dict[str, float] = {}


class LoginDisabled(HTTPException):
    """SESSION_SECRET is unset, so nothing can be signed or trusted."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=(
                "Login is not configured on this deployment (SESSION_SECRET is "
                "unset). Your API key still works on every /api route."
            ),
        )


def is_enabled() -> bool:
    return bool(get_settings().session_secret)


def _serializer() -> URLSafeTimedSerializer:
    s = get_settings()
    if not s.session_secret:
        raise LoginDisabled()
    return URLSafeTimedSerializer(s.session_secret, salt=_SALT)


# ---------------------------------------------------------------------------
# The cookie
# ---------------------------------------------------------------------------

def issue_session(response: Response, email: str) -> None:
    """Sign the address into a cookie and attach it to `response`.

    Secure and HttpOnly are not optional here: this cookie can be exchanged for
    the account's API key at /api/auth/me, so it is a credential in its own
    right. SameSite=Lax rather than Strict so that arriving from the emailed
    link -- a cross-site navigation -- still carries it.
    """
    s = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        _serializer().dumps(email),
        max_age=s.session_max_age_s,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def session_email(request: Request) -> str | None:
    """The signed-in address, or None. Never raises on a bad cookie.

    A tampered or expired cookie is simply "not signed in": there is nothing
    useful to tell the holder of a forged cookie, and raising here would turn
    every stale tab into a 500.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or not is_enabled():
        return None
    try:
        return _serializer().loads(raw, max_age=get_settings().session_max_age_s)
    except (BadSignature, SignatureExpired):
        return None
    except Exception as exc:  # noqa: BLE001 - a broken cookie is not a crash
        log.warning("session_decode_failed", error=str(exc)[:120])
        return None


def current_account(request: Request):
    """The signed-in account, or None. Read fresh from the database each time.

    The cookie holds an address and nothing else on purpose: tier, entitlement
    and key are looked up per request, so a grant or a revoke lands on the next
    page load instead of whenever a month-old cookie happens to lapse.
    """
    from src import accounts

    email = session_email(request)
    if not email:
        return None
    return accounts.by_email(email)


def require_account(request: Request):
    account = current_account(request)
    if account is None:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return account


# ---------------------------------------------------------------------------
# Magic links
# ---------------------------------------------------------------------------

def cooldown_remaining(email: str) -> int:
    from src import accounts

    window = get_settings().magic_link_cooldown_s
    if window <= 0:
        return 0
    last = _last_sent.get(accounts.normalise_email(email))
    if last is None:
        return 0
    left = window - (time.monotonic() - last)
    return int(left) if left > 0 else 0


def reset_cooldowns() -> None:
    """Test helper."""
    _last_sent.clear()


def _purge_expired() -> None:
    """Drop links that expired more than a day ago.

    Done here rather than on a schedule because this is the only thing that
    creates them, so the table cannot grow without this running. A day of grace
    keeps a just-expired link around long enough to tell somebody their link
    expired instead of that it never existed.
    """
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    try:
        with session_scope() as session:
            session.execute(delete(MagicLink).where(MagicLink.expires_at < cutoff))
    except Exception as exc:  # noqa: BLE001 - housekeeping must not fail a login
        log.warning("magic_link_purge_failed", error=str(exc)[:200])


def create_link(email: str, accept_terms: bool = False) -> str | None:
    """Issue a login token for an address, or None if it is on cooldown.

    Deliberately issued for addresses that have never registered: verifying the
    link creates the account. Requiring registration first would make the flow
    "register, be told 409, then ask for a link", which is three steps to do
    what one email already does.
    """
    from src import accounts

    address = accounts.normalise_email(email)
    if cooldown_remaining(address):
        return None

    _purge_expired()
    token = secrets.token_urlsafe(32)
    expires = dt.datetime.now(dt.UTC) + dt.timedelta(
        seconds=get_settings().magic_link_ttl_s
    )
    with session_scope() as session:
        session.add(MagicLink(
            email=address, token=token, expires_at=expires,
            terms_accepted=bool(accept_terms),
        ))
    _last_sent[address] = time.monotonic()
    log.info("magic_link_created", email=address)
    return token


def link_url(token: str) -> str:
    base = get_settings().base_url.rstrip("/")
    return f"{base}/auth/verify?token={token}"


def peek_token(token: str) -> str | None:
    """The address a token belongs to, WITHOUT consuming it.

    The verify page needs to know whether a link is good before it spends it,
    because the thing that renders that page may not be the person: mail
    scanners and link prefetchers issue a GET on every URL in a message. A GET
    that consumed the token would mean the user's own click always arrived
    second, to an already-used link.
    """
    return _resolve(token, consume=False)


def consume_token(token: str) -> str | None:
    """Spend a token and return its address, or None if it is not spendable."""
    return _resolve(token, consume=True)


def token_accepted_terms(token: str) -> bool:
    """Did the person who asked for this link tick the box?"""
    with session_scope() as session:
        row = session.execute(
            select(MagicLink).where(MagicLink.token == token)
        ).scalar_one_or_none()
        return bool(row and row.terms_accepted)


def _resolve(token: str, *, consume: bool) -> str | None:
    if not token:
        return None
    now = dt.datetime.now(dt.UTC)
    with session_scope() as session:
        row = session.execute(
            select(MagicLink).where(MagicLink.token == token)
        ).scalar_one_or_none()
        if row is None:
            log.info("magic_link_unknown")
            return None
        if row.used:
            log.info("magic_link_already_used", email=row.email)
            return None
        # Stored naive on some backends; compare on the same footing.
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.UTC)
        if expires < now:
            log.info("magic_link_expired", email=row.email)
            return None
        if consume:
            # Marked used BEFORE the caller sets a cookie, so a crash between
            # the two leaves a spent link rather than a live one.
            row.used = True
            log.info("magic_link_consumed", email=row.email)
        return row.email


def account_for_login(email: str, accepted_terms: bool = False):
    """The account behind a verified link, created if this is a first login.

    A verified link is proof of the address, which is exactly the bar
    registration asks for -- so arriving here without an account is a signup,
    not an error.
    """
    from src import accounts

    address = accounts.normalise_email(email)
    existing = accounts.by_email(address)
    if existing is not None:
        return existing
    # First sign-in for this address is a signup, so it needs the same
    # acceptance a signup form would have asked for.
    if not accepted_terms:
        raise HTTPException(
            status_code=422,
            detail=(
                "Please accept the Terms of Service and Privacy Policy, then "
                "request a new link."
            ),
        )
    account = accounts.register(address)
    stamp_terms(address)
    log.info("account_created_via_magic_link", email=address)
    return account


def regenerate_key(email: str) -> str:
    """Replace an account's API key and return the new one.

    Instant revocation: the old key stops working on the next request. That is
    the point -- this is the button somebody presses because their key leaked.
    """
    from src import accounts

    address = accounts.normalise_email(email)
    new_key = accounts.generate_api_key()
    with session_scope() as session:
        user = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="No such account.")
        user.api_key = new_key
        user.updated_at = dt.datetime.now(dt.UTC)
    log.info("api_key_regenerated", email=address)
    return new_key


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
# An ALTERNATIVE to magic links, never a replacement. An account that never
# sets one stays fully usable, which is why `password_hash` is nullable and why
# nothing here ever requires it.
#
# There is deliberately no separate password-reset token. "Forgot password"
# sends a magic link, and setting a new password is something you do once
# signed in. One token system with one expiry and one set of edge cases beats
# two that must be kept in agreement.

# bcrypt silently truncates at 72 BYTES. Silently is the problem: two different
# passwords sharing a 72-byte prefix would be interchangeable, and the longer
# one's owner would never know. Rejected explicitly instead.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_CHARS = 8

# Failed attempts per address per hour, in process memory. Unlike the call
# meter this need not survive a restart: an attacker cannot make the container
# bounce, and the window is an hour. Per ADDRESS rather than per IP because
# credential stuffing against one account arrives from many addresses, while an
# IP cap would lock out everyone behind one office.
_failed: dict[str, list[float]] = {}

# Verified against when no account exists, so a missing address costs the same
# time as a wrong password. Without it, "which of these emails is registered"
# is answerable with a stopwatch.
_DUMMY_HASH = (
    b"$2b$12$Ns8xNqXQ3rN0m8Zc3H1kIeQ0m1kzZ0m8Zc3H1kIeQ0m1kzZ0m8Zc3"
)


class PasswordRejected(HTTPException):
    def __init__(self, why: str) -> None:
        super().__init__(status_code=422, detail=why)


def check_password_shape(password: str) -> None:
    """Length only. No composition rules -- they push people toward
    `Password1!` and buy nothing a length floor does not."""
    if len(password) < MIN_PASSWORD_CHARS:
        raise PasswordRejected(
            f"Password must be at least {MIN_PASSWORD_CHARS} characters."
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordRejected(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt cannot see past that, and silently ignoring the rest "
            "would make a longer password no stronger)."
        )


def hash_password(password: str) -> str:
    import bcrypt

    rounds = get_settings().bcrypt_rounds
    return bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt(rounds=rounds)
    ).decode("ascii")


def verify_password(password: str, hashed: str | None) -> bool:
    """Constant-ish time whether or not a hash exists.

    A missing hash still runs a real bcrypt comparison against a dummy, so an
    account with no password set cannot be told apart from a wrong password by
    timing -- and neither can an address with no account at all.
    """
    import bcrypt

    candidate = (hashed or "").encode("ascii") or _DUMMY_HASH
    try:
        ok = bcrypt.checkpw(password.encode("utf-8"), candidate)
    except (ValueError, TypeError):
        return False
    return bool(ok) and bool(hashed)


def login_attempts_remaining(email: str) -> int:
    from src import accounts

    limit = get_settings().login_attempts_per_hour
    key = accounts.normalise_email(email)
    now = time.monotonic()
    hits = [t for t in _failed.get(key, []) if now - t < 3600]
    _failed[key] = hits
    return max(0, limit - len(hits))


def record_failed_login(email: str) -> None:
    from src import accounts

    key = accounts.normalise_email(email)
    _failed.setdefault(key, []).append(time.monotonic())


def reset_login_attempts(email: str | None = None) -> None:
    """Cleared on a successful login, and wholesale by the tests."""
    from src import accounts

    if email is None:
        _failed.clear()
    else:
        _failed.pop(accounts.normalise_email(email), None)


def authenticate(email: str, password: str):
    """The account behind an email/password pair, or None.

    Returns None identically for: no such address, wrong password, and an
    account that only uses magic links. The caller must not distinguish them --
    "no password set for this account" would confirm the address exists, which
    is precisely what the uniform reply everywhere else in this codebase
    refuses to do.
    """
    from src import accounts

    address = accounts.normalise_email(email)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        stored = row.password_hash if row is not None else None
        # Always runs, even with no row, so the timing does not answer
        # "is this address registered".
        ok = verify_password(password, stored)
        if not ok or row is None:
            return None
        return accounts._snapshot(row)


def set_password(email: str, password: str) -> None:
    """Set or replace the hash for an existing account."""
    from src import accounts

    check_password_shape(password)
    hashed = hash_password(password)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == accounts.normalise_email(email))
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="No such account.")
        row.password_hash = hashed
        row.updated_at = dt.datetime.now(dt.UTC)
    log.info("password_set", email=accounts.normalise_email(email))


def stamp_terms(email: str) -> None:
    """Record when this account accepted the terms."""
    from src import accounts

    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == accounts.normalise_email(email))
        ).scalar_one_or_none()
        if row is not None:
            row.terms_accepted_at = dt.datetime.now(dt.UTC)
