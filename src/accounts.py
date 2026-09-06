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
    tier: str
    has_paid_download: bool

    @property
    def call_limit(self) -> int:
        return tier_limit(self.tier)


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


def _snapshot(row: ApiUser) -> Account:
    return Account(
        id=row.id,
        email=row.email,
        api_key=row.api_key,
        tier=row.subscription_tier,
        has_paid_download=bool(row.has_paid_download),
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

def lookup(api_key: str) -> Account | None:
    key = (api_key or "").strip()
    if not key:
        return None
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.api_key == key)
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
    if account is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing or unknown API key. Send it as an X-API-Key header. "
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
    key = x_api_key or request.query_params.get("api_key") or ""
    account = lookup(key)
    if account is None:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing or unknown API key. Send it as an X-API-Key header, "
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
        before = (user.subscription_tier, bool(user.has_paid_download))
        if action == "grant_download":
            user.has_paid_download = True
        elif action == "revoke_download":
            user.has_paid_download = False
        elif action == "grant_pro":
            user.subscription_tier = "pro"
        elif action == "revoke_pro":
            user.subscription_tier = "free"
        user.updated_at = dt.datetime.now(dt.UTC)
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
    }
