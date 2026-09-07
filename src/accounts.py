"""API accounts: who may call, how much, and what they have paid for.

There is no payment processor in this service, on purpose. Money arrives out of
band -- an e-transfer or a PayPal notice in an inbox -- and access is granted by
one authenticated POST to `/admin/grant-access`. That is the whole billing
system. It has no webhooks to verify, no card data to hold, and no third party
that can lock the account; the cost is that a purchase is not instant, which is
stated plainly to the buyer rather than hidden.

Two things are metered and they are deliberately different:

* Calls are metered per calendar month against the caller's tier. Exceeding the
  allowance is a 429 -- "come back next month or upgrade" -- not a 402, because
  the free tier is not a payment failure.
* The full-dataset download is metered by a boolean, not a counter. It is a
  one-time purchase of a file, so the only question is whether it was paid for,
  and the answer to an unpaid request is 402 with instructions.

Everything here is synchronous SQLAlchemy, matching the rest of the codebase.
FastAPI runs a `def` endpoint (and a `def` dependency) in a worker thread, so a
blocking query here does not block the event loop.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from dataclasses import dataclass
from typing import Literal

import structlog
from fastapi import Header, HTTPException, Request
from sqlalchemy import func, select

from src.config.settings import get_settings
from src.storage.db import session_scope
from src.storage.models import AdminAction, ApiUser, UsageLog, current_month

log = structlog.get_logger(__name__)

Tier = Literal["free", "pro"]
AdminActionName = Literal[
    "grant_download", "grant_pro", "revoke_download", "revoke_pro"
]
VALID_ACTIONS: tuple[str, ...] = (
    "grant_download", "grant_pro", "revoke_download", "revoke_pro",
)

# Deliberately permissive. This is not identity verification -- nobody is
# emailed to confirm anything -- it is a check that the string is shaped like an
# address, so a typo is caught at registration instead of surfacing as an
# unreachable buyer weeks later. A stricter regex would reject valid addresses
# and buy nothing, and `pydantic.EmailStr` would add a dependency
# (email-validator) that is not in requirements.txt.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# Header names, in one place, so the API and the dashboard cannot disagree.
API_KEY_HEADER = "X-API-Key"
ADMIN_SECRET_HEADER = "X-Admin-Secret"


class EmailTaken(Exception):
    """That address already has a key. Registering again must not reveal it."""


@dataclass(frozen=True)
class Account:
    """A caller, read out of the database and detached from the session.

    A plain snapshot rather than a live ORM object because it is passed out of
    the `session_scope` that loaded it. Returning the mapped instance would work
    today (the session factory sets `expire_on_commit=False`) and break the
    first time somebody touches an unloaded attribute after the session closed
    -- a failure that shows up in production, not in a test.
    """

    id: str
    email: str
    api_key: str
    # The tier that is IN FORCE, not the column. A subscription whose date has
    # passed reads "free" here, so every caller gets the right answer without
    # remembering to check the expiry itself -- which is the sort of thing one
    # caller always forgets.
    tier: str
    has_paid_download: bool
    pro_expires_at: dt.datetime | None = None
    # True when the column says pro but the date has passed. Distinct from
    # plain "free" so the dashboard can say "expired 2 days ago" rather than
    # silently downgrading and leaving somebody to wonder what happened.
    lapsed: bool = False
    has_password: bool = False

    @property
    def call_limit(self) -> int:
        return tier_limit(self.tier)

    @property
    def days_remaining(self) -> int | None:
        """Whole days of Pro left. None means it never expires."""
        if self.pro_expires_at is None:
            return None
        delta = _aware(self.pro_expires_at) - dt.datetime.now(dt.UTC)
        return max(0, delta.days)


def tier_limit(tier: str) -> int:
    s = get_settings()
    return s.pro_tier_monthly_calls if tier == "pro" else s.free_tier_monthly_calls


def normalise_email(email: str) -> str:
    """Lowercased and trimmed. Two people typing the same address in different
    cases are one account, not two free allowances."""
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def _aware(when: dt.datetime) -> dt.datetime:
    """Naive timestamps come back from some backends; compare on one footing."""
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def effective_tier(row: ApiUser, now: dt.datetime | None = None) -> str:
    """The tier actually in force, expiry included.

    A NULL `pro_expires_at` means NEVER EXPIRES, not "already expired". That is
    the only safe reading: every Pro account that existed before the column did
    has NULL, and the other interpretation would have demoted all of them the
    moment this deployed. It also gives comped accounts a natural spelling.
    """
    if row.subscription_tier != "pro":
        return "free"
    if row.pro_expires_at is None:
        return "pro"
    return "pro" if _aware(row.pro_expires_at) > (now or dt.datetime.now(dt.UTC)) else "free"


def _snapshot(row: ApiUser) -> Account:
    live = effective_tier(row)
    return Account(
        id=row.id,
        email=row.email,
        api_key=row.api_key,
        tier=live,
        has_paid_download=bool(row.has_paid_download),
        pro_expires_at=row.pro_expires_at,
        lapsed=row.subscription_tier == "pro" and live == "free",
        has_password=bool(row.password_hash),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(email: str) -> Account:
    """Create one account and return it. Raises `EmailTaken` if it exists.

    It must NOT return the existing key for an address already registered.
    Doing so would turn this open, unauthenticated endpoint into a key-recovery
    oracle: anyone who guessed a customer's address could type it here and be
    handed that customer's paid key.
    """
    address = normalise_email(email)
    with session_scope() as session:
        existing = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if existing is not None:
            raise EmailTaken(address)
        user = ApiUser(
            email=address,
            api_key=generate_api_key(),
            subscription_tier="free",
            has_paid_download=False,
        )
        session.add(user)
        session.flush()  # populate defaults (id) before the snapshot
        return _snapshot(user)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def is_demo(account: Account) -> bool:
    """The shared demo account, which is barred from the ordinary keyed API."""
    from src.demo import DEMO_EMAIL

    return account.email == DEMO_EMAIL


# Per-address cooldown for key recovery, held in process memory. Unlike the call
# meter this does NOT need to survive a restart: the worst a bounce can buy an
# abuser is one extra email per container, which is noise next to the global
# hourly gate in front of it. A table for that would be storage spent on nothing.
_resend_seen: dict[str, float] = {}


def resend_cooldown_remaining(email: str) -> int:
    """Seconds until this address may be mailed again. 0 if it may be now."""
    import time

    window = get_settings().resend_cooldown_s
    if window <= 0:
        return 0
    last = _resend_seen.get(normalise_email(email))
    if last is None:
        return 0
    left = window - (time.monotonic() - last)
    return int(left) if left > 0 else 0


def mark_resent(email: str) -> None:
    import time

    _resend_seen[normalise_email(email)] = time.monotonic()


def reset_resend_cooldowns() -> None:
    """Test helper: forget every cooldown."""
    _resend_seen.clear()


def fingerprint(key: str | None) -> str:
    """Enough of a key to identify it in a log, never enough to use it.

    A rejected key is the one thing here that actually needs debugging -- "is
    the caller sending the key I think they are" cannot be answered from a bare
    401 -- and the obvious fix, logging the key, writes a live credential into a
    log aggregator that outlives the incident. The prefix plus the length settles
    every real question (truncated? whitespace? URL-encoded? a different key
    entirely?) and grants nobody access.
    """
    if key is None:
        return "<absent>"
    if not key:
        return "<empty>"
    return f"{key[:6]}...len={len(key)}"


def lookup(api_key: str) -> Account | None:
    key = (api_key or "").strip()
    if not key:
        return None
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.api_key == key)
        ).scalar_one_or_none()
        return _snapshot(row) if row is not None else None


def by_email(email: str) -> Account | None:
    """The account for an address, or None. Used only by key recovery, which
    mails the result rather than returning it."""
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == normalise_email(email))
        ).scalar_one_or_none()
        return _snapshot(row) if row is not None else None


def get_current_user(
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> Account:
    """FastAPI dependency: the caller behind `X-API-Key`, or 401.

    Sync on purpose -- FastAPI runs it in a worker thread, so the blocking
    lookup never touches the event loop.
    """
    account = lookup(x_api_key or "")
    if account is not None and is_demo(account):
        # The demo key is a real key that the home page's demo route uses on
        # every visitor's behalf. If it ever escapes -- and a key used that
        # often eventually does -- it must not also be a working key for the
        # ordinary API, where there is no per-address ceiling to stop it.
        log.warning("demo_key_used_on_api", path="header-auth")
        raise HTTPException(
            status_code=403,
            detail=(
                "That is the public demo key. It works only on /api/demo/"
                "{ticker}. Get your own free key at /dashboard."
            ),
        )
    if account is None:
        log.warning(
            "api_key_rejected", source="header", key=fingerprint(x_api_key)
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid or missing API key. Send it as an X-API-Key header. "
                "Get one free at /dashboard."
            ),
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return account


def get_current_user_flexible(
    request: Request,
    x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER),
) -> Account:
    """As above, but also accepts `?api_key=` in the query string.

    Only the dataset download uses this. That response is a multi-hundred-
    megabyte stream, and a browser can only save it straight to disk from a
    plain link -- a `fetch()` carrying a header has to buffer the whole file in
    the tab's memory first, which is how you crash a phone. A link cannot carry
    a header, so the key has to ride in the URL.

    The cost is real and worth naming: a URL is written to browser history and
    to any proxy log on the path. It is accepted here because this key grants
    read access to filings that are already public, and it is NOT accepted on
    any other route.
    """
    from_query = request.query_params.get("api_key")
    key = x_api_key or from_query or ""
    account = lookup(key)
    if account is None:
        # Which of the two channels the key arrived on is the first thing worth
        # knowing when a download is refused, because they fail for different
        # reasons: a header gets dropped by a proxy, a query parameter gets
        # truncated or double-encoded by whatever built the URL.
        log.warning(
            "api_key_rejected",
            source="header" if x_api_key else ("query" if from_query else "none"),
            key=fingerprint(x_api_key or from_query),
            path=request.url.path,
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "Invalid or missing API key. Send it as an X-API-Key header, "
                "or as ?api_key= on this endpoint only."
            ),
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return account


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------

def calls_this_month(user_id: str, month: str | None = None) -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(UsageLog)
                .where(UsageLog.user_id == user_id)
                .where(UsageLog.month == (month or current_month()))
            ).scalar_one()
        )


def enforce_monthly_limit(account: Account) -> int:
    """Raise 429 if this month's allowance is spent. Returns calls used.

    Checked BEFORE the work is done and recorded after it succeeds, so a call
    that 404s or errors is not billed. The gap between the two is a race a
    caller running many parallel requests could use to overshoot by a handful.
    That is accepted: the alternative is a lock around every read, for a quota
    whose purpose is to stop bulk scraping rather than to bill by the unit.
    """
    limit = account.call_limit
    used = calls_this_month(account.id)
    if limit and used >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Monthly limit reached: {used}/{limit} calls on the "
                f"{account.tier} tier. It resets on the 1st (UTC). "
                "Upgrade for a larger allowance -- see /dashboard."
            ),
        )
    return used


def record_call(account: Account, endpoint: str) -> None:
    """Bill one metered call. Never raises -- a lost meter row beats a 500.

    Only metered endpoints are written here, so `count(*)` for the month IS the
    usage figure with no filtering. `/api/user/status` and the download are
    deliberately absent: checking your own balance must not spend it, and the
    download is gated by payment rather than by the counter.
    """
    try:
        with session_scope() as session:
            session.add(
                UsageLog(
                    user_id=account.id,
                    endpoint_called=endpoint[:128],
                    month=current_month(),
                )
            )
    except Exception as exc:  # noqa: BLE001 - metering must not break the API
        log.warning("usage_record_failed", user=account.id, error=str(exc)[:200])


def require_paid_download(account: Account) -> None:
    if account.has_paid_download:
        return
    s = get_settings()
    where = s.admin_email or "the site owner"
    raise HTTPException(
        status_code=402,
        detail=(
            f"Payment required. The full dataset is a one-time "
            f"${s.dataset_price_usd}. Contact {where} to purchase, quoting "
            "your API key, and it is unlocked by hand."
        ),
    )


# ---------------------------------------------------------------------------
# Admin: the manual grant switch
# ---------------------------------------------------------------------------

def verify_admin_secret(supplied: str | None, ip_hash: str | None = None) -> None:
    """Gate for the grant endpoint. Fails CLOSED when unconfigured.

    An unset `ADMIN_SECRET` returns 503, not 200: if an empty configured secret
    were compared against an empty header they would match, and forgetting the
    variable on one redeploy would publish the grant switch to the internet.
    Comparison is `compare_digest`, so a wrong secret takes the same time to
    reject however many leading characters it got right.
    """
    s = get_settings()
    if not s.admin_secret:
        log.error("admin_secret_unset")
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin access is not configured on this deployment "
                "(ADMIN_SECRET is unset), so nothing can be granted."
            ),
        )
    if not supplied or not secrets.compare_digest(supplied, s.admin_secret):
        _write_admin_action(
            email="-", action="auth", ok=False,
            ip_hash=ip_hash, detail="bad or missing X-Admin-Secret",
        )
        log.warning("admin_secret_rejected", ip_hash=ip_hash)
        raise HTTPException(status_code=403, detail="Forbidden.")


def _write_admin_action(
    *, email: str, action: str, ok: bool,
    ip_hash: str | None = None, detail: str | None = None,
) -> None:
    try:
        with session_scope() as session:
            session.add(
                AdminAction(
                    email=email[:254], action=action[:32], ok=ok,
                    actor_ip_hash=ip_hash,
                    detail=(detail or None) and detail[:300],
                )
            )
    except Exception as exc:  # noqa: BLE001 - the audit row must not break the grant
        log.warning("admin_action_log_failed", error=str(exc)[:200])


def apply_admin_action(
    email: str, action: str, ip_hash: str | None = None
) -> Account:
    """Grant or revoke, and record that it happened. Raises 404/400 on bad input.

    Idempotent by construction: granting what is already granted is a no-op that
    still returns the user, so re-running the curl after a flaky connection is
    safe rather than something the operator has to remember not to do.
    """
    address = normalise_email(email)
    if action not in VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action {action!r}. One of: {', '.join(VALID_ACTIONS)}.",
        )
    with session_scope() as session:
        user = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if user is None:
            _write_admin_action(
                email=address, action=action, ok=False,
                ip_hash=ip_hash, detail="no such user",
            )
            raise HTTPException(
                status_code=404,
                detail=f"No account for {address}. They must register first.",
            )
        _now = dt.datetime.now(dt.UTC)
        before = (user.subscription_tier, bool(user.has_paid_download))
        if action == "grant_download":
            user.has_paid_download = True
        elif action == "revoke_download":
            user.has_paid_download = False
        elif action == "grant_pro":
            # EXTEND, never reset. A customer who renews three days early would
            # otherwise lose those three days -- the one billing bug a paying
            # customer notices and remembers. max() makes an early renewal add
            # to what is left and a late one start from today.
            period = dt.timedelta(days=get_settings().pro_period_days)
            current = (
                _aware(user.pro_expires_at) if user.pro_expires_at else _now
            )
            user.subscription_tier = "pro"
            user.pro_expires_at = max(current, _now) + period
            # A fresh period deserves a fresh warning.
            user.pro_reminder_sent_at = None
        elif action == "revoke_pro":
            # Backdated rather than nulled: NULL means "never expires", so
            # setting it to None here would UPGRADE them to a comped account.
            user.subscription_tier = "free"
            user.pro_expires_at = _now - dt.timedelta(days=1)
        user.updated_at = _now
        session.flush()
        after = (user.subscription_tier, bool(user.has_paid_download))
        snapshot = _snapshot(user)

    _write_admin_action(
        email=address, action=action, ok=True, ip_hash=ip_hash,
        detail=f"tier {before[0]}->{after[0]}, download {before[1]}->{after[1]}",
    )
    log.info(
        "admin_access_changed",
        email=address, action=action, tier=after[0], download=after[1],
    )
    return snapshot


def status_payload(account: Account) -> dict:
    """What `/api/user/status` returns, and what the dashboard renders."""
    used = calls_this_month(account.id)
    return {
        "email": account.email,
        "tier": account.tier,
        "has_paid_download": account.has_paid_download,
        "calls_used_this_month": used,
        "calls_limit": account.call_limit,
        "calls_remaining": max(0, account.call_limit - used),
        "month": current_month(),
        "expires_at": (
            _aware(account.pro_expires_at).isoformat()
            if account.pro_expires_at
            else None
        ),
        "days_remaining": account.days_remaining,
        "lapsed": account.lapsed,
        "has_password": account.has_password,
    }


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def subscriptions() -> list[dict]:
    """Every account whose column says pro, soonest expiry first.

    Includes the lapsed ones. A subscription that ran out yesterday is exactly
    what the operator needs to see -- filtering to the still-valid ones would
    hide the renewals that have already been missed.
    """
    now = dt.datetime.now(dt.UTC)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(ApiUser).where(ApiUser.subscription_tier == "pro")
            ).scalars()
        )
    out = []
    for row in rows:
        expires = _aware(row.pro_expires_at) if row.pro_expires_at else None
        out.append({
            "email": row.email,
            "expires_at": expires.isoformat() if expires else None,
            "days_remaining": (
                "never" if expires is None else max(0, (expires - now).days)
            ),
            "lapsed": bool(expires and expires <= now),
            "reminder_sent_at": (
                _aware(row.pro_reminder_sent_at).isoformat()
                if row.pro_reminder_sent_at
                else None
            ),
        })
    # None sorts last: a comped account never needs attention, so it belongs at
    # the bottom of a list whose whole purpose is "what needs doing next".
    out.sort(key=lambda r: (r["expires_at"] is None, r["expires_at"] or ""))
    return out


def expiring_soon(within_days: int) -> list[dict]:
    """Pro accounts running out inside `within_days` that nobody has been
    warned about yet. Marking them warned is a separate step, so a send that
    fails does not silently consume the one reminder."""
    now = dt.datetime.now(dt.UTC)
    horizon = now + dt.timedelta(days=within_days)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(ApiUser)
                .where(ApiUser.subscription_tier == "pro")
                .where(ApiUser.pro_expires_at.is_not(None))
                .where(ApiUser.pro_reminder_sent_at.is_(None))
            ).scalars()
        )
    due = [
        {
            "email": r.email,
            "expires_at": _aware(r.pro_expires_at),
            "days_remaining": max(0, (_aware(r.pro_expires_at) - now).days),
        }
        for r in rows
        if now < _aware(r.pro_expires_at) <= horizon
    ]
    due.sort(key=lambda r: r["expires_at"])
    return due


def mark_reminded(emails: list[str]) -> int:
    """Record that the operator has been warned about these subscriptions.

    Called only after the mail is away. The order matters: marking first and
    sending second would lose the warning entirely whenever the relay is down.
    """
    if not emails:
        return 0
    now = dt.datetime.now(dt.UTC)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(ApiUser).where(ApiUser.email.in_(emails))
            ).scalars()
        )
        for row in rows:
            row.pro_reminder_sent_at = now
        return len(rows)
