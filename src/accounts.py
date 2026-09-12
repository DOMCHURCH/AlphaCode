"""API accounts: who may call, how much, and what they have paid for.

Access is granted by exactly one function, `apply_admin_action`, and two things
call it: `POST /admin/grant-access`, which an operator runs by hand, and the
Stripe webhook in `src/billing.py`, which runs it when a payment settles. That
is the whole billing system. Keeping the card path and the manual path on one
switch is what makes a comp, a refund and a chargeback the same operation, and
it means nothing in this module has to know whether money was involved.

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
import hashlib
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
    # The first 8 characters of the key, not the key. Nothing can read the key
    # back out of this process: it exists only in the response that issued it.
    api_key_prefix: str
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
    # Which Stripe price they are on: "monthly" | "annual" | "". Descriptive
    # only -- nothing about access depends on it.
    pro_plan: str = ""
    # The current run of failed renewal invoices, and whether that run has
    # already cost them access. Read defensively: both columns arrive NULL on
    # every row that existed before they did.
    payment_failure_count: int = 0
    api_access_paused: bool = False
    # Whether Stripe holds a customer for this account. Not the id itself --
    # nothing in the browser needs that, and a Stripe id in a JSON payload is
    # an identifier leaking for no benefit. It answers exactly one question:
    # is there any billing here to manage.
    has_billing: bool = False
    # Set only on a key issued by hand for partner outreach (`src/seedkeys`).
    # `seed_source` is what every branch in this module tests, and it is empty
    # on every account that came through registration. Behind one of these
    # there is no account row, no email and no tier -- a tier is a thing you
    # pay for.
    seed_source: str = ""
    seed_label: str = ""
    seed_limit: int = 0

    @property
    def is_seeded(self) -> bool:
        """Whether this caller is a hand-issued outreach key.

        The one test, spelled once. `seedkeys.find` matches on the same value,
        so a row with any other source never becomes an Account at all and
        cannot arrive here wearing this one's allowance.
        """
        from src.seedkeys import SOURCE

        return self.seed_source == SOURCE

    @property
    def call_limit(self) -> int:
        """The monthly allowance in force.

        A seeded key carries its own, set when it was issued, and it stands in
        for the tier lookup entirely rather than sitting beside it. Putting it
        HERE is what keeps the rest of the change to one branch: everything
        that meters, reports or refuses already reads this property.
        """
        return self.seed_limit if self.is_seeded else tier_limit(self.tier)

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


def hash_api_key(key: str) -> str:
    """The value stored in `api_users.api_key`. One-way, and the only writer.

    Stripped first, because `lookup` strips too and a key stored with a stray
    newline would otherwise never match itself. SHA-256 and not bcrypt: the
    input is a 256-bit random token with no dictionary behind it, so a slow KDF
    would add latency to every API call and close no attack.
    """
    return hashlib.sha256((key or "").strip().encode("utf-8")).hexdigest()


def key_prefix(key: str) -> str:
    """The part of a key that is safe to show: enough to tell two keys apart.

    Eight characters of a 43-character urlsafe token. Recognising your own key
    in a dashboard needs a handful of characters; guessing the rest needs the
    other 35, which is 208 bits.
    """
    return (key or "").strip()[:8]


def looks_hashed(stored: str) -> bool:
    """Whether a stored value is already a digest rather than a live key.

    The migration's only question. Unambiguous by construction: a digest is
    exactly 64 hex characters and an issued key is 43 characters of urlsafe
    base64, which always contains at least one character outside [0-9a-f] --
    and even the vanishingly unlikely all-hex key is the wrong length.
    """
    value = (stored or "").strip()
    if len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


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
        api_key_prefix=str(row.api_key_prefix or ""),
        tier=live,
        has_paid_download=bool(row.has_paid_download),
        pro_expires_at=row.pro_expires_at,
        lapsed=row.subscription_tier == "pro" and live == "free",
        has_password=bool(row.password_hash),
        pro_plan=str(row.pro_plan or ""),
        payment_failure_count=int(row.payment_failure_count or 0),
        api_access_paused=bool(row.api_access_paused),
        has_billing=bool(row.stripe_customer_id),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(email: str) -> tuple[Account, str]:
    """Create one account. Returns the account AND the plaintext key.

    The key is returned separately and not on the `Account` because this is
    the only moment it exists: the database gets the digest, and nothing can
    recover it afterwards. A tuple rather than a field is deliberate -- it
    makes every caller name what it does with a show-once secret instead of
    carrying one around in a snapshot that gets logged and serialised.

    It must NOT return the existing key for an address already registered.
    Doing so would turn this open, unauthenticated endpoint into a key-recovery
    oracle: anyone who guessed a customer's address could type it here and be
    handed that customer's paid key.
    """
    address = normalise_email(email)
    plaintext = generate_api_key()
    with session_scope() as session:
        existing = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if existing is not None:
            raise EmailTaken(address)
        user = ApiUser(
            email=address,
            api_key=hash_api_key(plaintext),
            api_key_prefix=key_prefix(plaintext),
            subscription_tier="free",
            has_paid_download=False,
        )
        session.add(user)
        session.flush()  # populate defaults (id) before the snapshot
        return _snapshot(user), plaintext


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
    """The account holding this key, or None.

    Hashes and then compares, so this is still one indexed equality test and
    not a scan-and-verify over every row. That is the practical reason the
    digest is unsalted: a per-row salt would force exactly that scan on the
    hottest query in the service.
    """
    key = (api_key or "").strip()
    if not key:
        return None
    digest = hash_api_key(key)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.api_key == digest)
        ).scalar_one_or_none()
        if row is not None:
            return _snapshot(row)
    return _seed_snapshot(digest)


def _seed_snapshot(digest: str) -> Account | None:
    """An Account for a hand-issued key, or None.

    Reached only after the customer table has missed, so the hot path is
    exactly the one indexed equality test it was before this existed.

    A REVOKED key is not found here, which is the whole of revocation: the
    caller gets the same None an unknown key gets and the same 401 behind it,
    with no second rejection path to keep in step with the first.

    Logged at INFO on every authentication rather than on every metered call.
    An outreach key that authenticates and then 429s is still a key somebody
    is holding and using, and that is the thing worth seeing in the log.
    """
    from src import seedkeys

    seed = seedkeys.find(digest)
    if seed is None:
        return None
    log.info("seed_key_used", label=seed["label"], source=seed["source"],
             prefix=seed["prefix"])
    return Account(
        id=seed["id"],
        # No address: nobody signed up, nobody confirmed anything, and an
        # invented one would appear in the customer roster as a person.
        email="",
        api_key_prefix=seed["prefix"],
        tier="free",
        has_paid_download=False,
        seed_source=seed["source"],
        seed_label=seed["label"],
        seed_limit=seed["rate_limit"],
    )


def by_email(email: str) -> Account | None:
    """The account for an address, or None. Used only by key recovery, which
    mails the result rather than returning it."""
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == normalise_email(email))
        ).scalar_one_or_none()
        return _snapshot(row) if row is not None else None


def record_payment_failure(email: str) -> int:
    """Count one failed renewal invoice. Returns the new run length.

    Returns 0 for an address this service does not know, which the caller must
    tell apart from "first failure": a payment failing for somebody who is not
    a customer here is somebody else's charge on the same Stripe account, and
    it must not create a row.
    """
    address = normalise_email(email)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if row is None:
            return 0
        row.payment_failure_count = int(row.payment_failure_count or 0) + 1
        return int(row.payment_failure_count)


def clear_payment_failures(email: str) -> None:
    """A payment succeeded, so the run of failures is over.

    Called from the renewal path rather than from a scheduled sweep, because
    the only thing that truthfully ends a dunning run is money arriving. Also
    lifts the pause: an account whose card works again is a paying account, and
    leaving the flag set would keep them locked out of what they just bought.
    """
    address = normalise_email(email)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if row is None:
            return
        row.payment_failure_count = 0
        row.api_access_paused = False


def pause_api_access(email: str, *, paused: bool = True) -> None:
    """Withdraw (or restore) metered API access after a dunning run.

    Deliberately separate from `apply_admin_action(..., "revoke_pro")`. That
    one changes the ALLOWANCE -- it puts the account back on the free tier's
    quota, which is a number. This says the account is stopped and why, which
    is what `enforce_monthly_limit` refuses on and what a support conversation
    needs to read. A card that starts working clears both.
    """
    address = normalise_email(email)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if row is None:
            return
        row.api_access_paused = bool(paused)


def set_pro_expiry(email: str, when: dt.datetime | None) -> bool:
    """Set the Pro expiry to exactly `when`. True if a row was changed.

    An assignment, not an extension. `apply_admin_action("grant_pro")` extends
    from whichever is later, today or the current expiry, which is right when
    an operator is renewing somebody by hand and wrong here: this is called
    with Stripe's own `current_period_end`, and Stripe is the authority on when
    a subscription runs out. Extending from it would add a period to a period.

    It can therefore move an expiry BACKWARDS -- a downgrade from annual to
    monthly does exactly that -- which is the point.
    """
    address = normalise_email(email)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if row is None:
            return False
        row.pro_expires_at = when
        row.pro_reminder_sent_at = None
        return True


def set_pro_plan(email: str, plan: str) -> None:
    """Record which Stripe price this account is on. "" clears it."""
    address = normalise_email(email)
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == address)
        ).scalar_one_or_none()
        if row is None:
            return
        row.pro_plan = plan[:16] or None


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


def key_last_used_at(user_id: str) -> dt.datetime | None:
    """When this account last made a metered call, or None if it never has.

    Read out of `usage_logs` rather than kept as a `last_used_at` column on
    the account. The column would be a write on every single API request, to
    answer a question asked once per dashboard load, and the meter already
    records the same fact with an index that makes this a cheap max().
    """
    with session_scope() as session:
        return session.execute(
            select(func.max(UsageLog.called_at)).where(UsageLog.user_id == user_id)
        ).scalar_one_or_none()


def used_this_month(account: Account) -> int:
    """This month's calls, from whichever meter holds them.

    A seeded key is not in `usage_logs` and cannot be: that table's `user_id`
    is a foreign key into `api_users`, and these keys are decoupled from it on
    purpose. Its counter lives on its own row.
    """
    if account.is_seeded:
        from src import seedkeys

        return seedkeys.usage(account.id)[0]
    return calls_this_month(account.id)


def last_used(account: Account) -> dt.datetime | None:
    """When this key last made a metered call. Same split, same reason."""
    if account.is_seeded:
        from src import seedkeys

        return seedkeys.usage(account.id)[1]
    return key_last_used_at(account.id)


def enforce_monthly_limit(account: Account) -> int:
    """Raise 429 if this month's allowance is spent. Returns calls used.

    Checked BEFORE the work is done and recorded after it succeeds, so a call
    that 404s or errors is not billed. The gap between the two is a race a
    caller running many parallel requests could use to overshoot by a handful.
    That is accepted: the alternative is a lock around every read, for a quota
    whose purpose is to stop bulk scraping rather than to bill by the unit.
    """
    if account.api_access_paused:
        # 402, not 429: nothing is rate-limited here and nothing resets on the
        # 1st. The account is stopped because a payment failed repeatedly, and
        # the fix is a card, which is what the message has to say.
        raise HTTPException(
            status_code=402,
            detail=(
                "API access is paused because a subscription payment failed "
                "repeatedly. Update your card from /dashboard#billing and "
                "access resumes as soon as a payment settles."
            ),
        )
    limit = account.call_limit
    used = used_this_month(account)
    if limit and used >= limit:
        # The seeded wording drops the tier and the upgrade line. Neither is
        # true of somebody who was handed a key: there is no tier to name and
        # no dashboard they have an account on to upgrade from.
        where = (
            f"on the seeded key {account.seed_label!r}" if account.is_seeded
            else f"on the {account.tier} tier"
        )
        after = (
            "It resets on the 1st (UTC). Reply to whoever sent you this key "
            "if you need more." if account.is_seeded
            else "It resets on the 1st (UTC). Upgrade for a larger allowance "
                 "-- see /dashboard."
        )
        raise HTTPException(
            status_code=429,
            detail=f"Monthly limit reached: {used}/{limit} calls {where}. {after}",
        )
    return used


def record_call(account: Account, endpoint: str) -> None:
    """Bill one metered call. Never raises -- a lost meter row beats a 500.

    Only metered endpoints are written here, so `count(*)` for the month IS the
    usage figure with no filtering. `/api/user/status` and the download are
    deliberately absent: checking your own balance must not spend it, and the
    download is gated by payment rather than by the counter.
    """
    if account.is_seeded:
        from src import seedkeys

        seedkeys.record_call(account.id)
        return
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
    from src.config.settings import price_label

    s = get_settings()
    where = s.admin_email or "the site owner"
    # `price_label`, never an f-string on the number: the dataset is $79.99 and
    # a bare interpolation of a rounded value quotes a price Stripe does not
    # charge -- in a 402 whose whole job is to say what it costs.
    #
    # The card path exists now, so this points at the page that takes one
    # rather than at somebody's inbox. The address stays as the fallback for a
    # deployment with no Stripe configuration.
    raise HTTPException(
        status_code=402,
        detail=(
            f"Payment required. The full dataset is a one-time "
            f"{price_label(s.dataset_price_usd)}, paid by card through Stripe: "
            f"buy it at /dataset. It is a static snapshot -- for live data, "
            f"the API is /pricing. Trouble paying? Contact {where}, quoting "
            f"your API key."
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
    # `compare_digest` on str raises TypeError the moment either side is not
    # ASCII, so a non-ASCII header used to be a 500 rather than a 403 -- an
    # unhandled crash on the authentication path, reachable by anybody. Compare
    # bytes: every value is encodable, and the comparison stays constant-time.
    try:
        ok = bool(supplied) and secrets.compare_digest(
            (supplied or "").encode("utf-8"), s.admin_secret.encode("utf-8")
        )
    except (UnicodeError, TypeError):
        ok = False
    if not ok:
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
    email: str, action: str, ip_hash: str | None = None, *, days: int | None = None
) -> Account:
    """Grant or revoke, and record that it happened. Raises 404/400 on bad input.

    Idempotent by construction: granting what is already granted is a no-op that
    still returns the user, so re-running the curl after a flaky connection is
    safe rather than something the operator has to remember not to do.

    `days` overrides how long a `grant_pro` buys, and exists because the length
    of a period is a property of the PAYMENT, not of the tier: there is one Pro
    tier, bought monthly or yearly, and an annual purchase granted 31 days
    reads "Free (expired)" on day 32 having paid for a year. Ignored by every
    other action -- a download is not bought for a length of time. Left as
    None by the manual switch, which still means "one ordinary period".
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
            period = dt.timedelta(days=days or get_settings().pro_period_days)
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
    used = used_this_month(account)
    seen = last_used(account)
    return {
        "email": account.email,
        "tier": account.tier,
        # The key itself is never in here and cannot be: the database holds a
        # digest. These two are what the dashboard shows in its place -- which
        # key this is, and whether it is in use.
        "api_key_prefix": account.api_key_prefix,
        "api_key_last_used": _aware(seen).isoformat() if seen else None,
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
        # Drives the "Manage subscription" button. The dashboard hides it
        # unless this is true, because a button that answers 409 for everybody
        # who has never paid teaches people not to trust the page.
        "has_billing": account.has_billing,
        "api_access_paused": account.api_access_paused,
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


def customer_stats(recent: int = 10) -> dict[str, object]:
    """Who has signed up, who is paying, and roughly what that is worth.

    Counted off `api_users`, which is the ONLY customer table -- the Stripe
    webhook writes here, the manual grant switch writes here, and the API keys
    live here. A second `users` table would be a second answer to "is this
    person a customer", and the two would disagree the first time a webhook
    landed while somebody was reading the other one.

    `subscription_tier` is the column; `effective_tier` is the truth. An
    account whose Pro ran out yesterday still says "pro" in the column, and is
    counted as LAPSED here rather than as revenue -- counting it as revenue is
    how a dashboard tells you business is fine while it is not.

    Revenue is an ESTIMATE and says so everywhere it is shown. This service
    stores no amount, no currency and no charge id: Stripe is the system of
    record for money and there is no join between the two. What is computed
    here is live subscribers times list price, which is wrong for anybody on a
    comp, an old price, or an annual plan. It is a shape, not a figure to put
    in a return.
    """
    from sqlalchemy import desc, func, select

    from src import seedkeys
    from src.config.settings import get_settings
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    s = get_settings()
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)

    # Beside the customer counts and deliberately not inside them: a seeded
    # key is not a signup and must never be added to `total_users` or to the
    # revenue estimate. It is here because this is the payload an operator
    # already reads, and a credential that bypasses payment should be in front
    # of them without their having to go looking for it.
    seeded = seedkeys.listing()

    with session_scope() as session:
        total = int(
            session.execute(select(func.count()).select_from(ApiUser)).scalar_one()
        )
        download = int(
            session.execute(
                select(func.count())
                .select_from(ApiUser)
                .where(ApiUser.has_paid_download.is_(True))
            ).scalar_one()
        )
        # Live Pro: the column says pro AND the date has not passed. NULL means
        # comped and never expires, which is live.
        live_pro = int(
            session.execute(
                select(func.count())
                .select_from(ApiUser)
                .where(ApiUser.subscription_tier == "pro")
                .where(
                    (ApiUser.pro_expires_at.is_(None))
                    | (ApiUser.pro_expires_at > now)
                )
            ).scalar_one()
        )
        lapsed = int(
            session.execute(
                select(func.count())
                .select_from(ApiUser)
                .where(ApiUser.subscription_tier == "pro")
                .where(ApiUser.pro_expires_at.is_not(None))
                .where(ApiUser.pro_expires_at <= now)
            ).scalar_one()
        )
        # Monthly vs annual is not a column -- one Pro tier, and how long it
        # was paid for is Stripe's business. Split on how far the expiry runs:
        # past ~6 months can only have come from an annual purchase.
        annual = int(
            session.execute(
                select(func.count())
                .select_from(ApiUser)
                .where(ApiUser.subscription_tier == "pro")
                .where(ApiUser.pro_expires_at > now + dt.timedelta(days=180))
            ).scalar_one()
        )
        comped = int(
            session.execute(
                select(func.count())
                .select_from(ApiUser)
                .where(ApiUser.subscription_tier == "pro")
                .where(ApiUser.pro_expires_at.is_(None))
            ).scalar_one()
        )
        rows = session.execute(
            select(
                ApiUser.email, ApiUser.subscription_tier,
                ApiUser.has_paid_download, ApiUser.created_at,
            )
            .order_by(desc(ApiUser.created_at))
            .limit(max(1, min(recent, 50)))
        ).all()
        signups = [
            {
                "email": e,
                "tier": tier,
                "has_paid_download": bool(dl),
                "created_at": created.isoformat() if created else None,
            }
            for e, tier, dl, created in rows
        ]

    monthly = max(0, live_pro - annual - comped)
    return {
        "total_users": total,
        "free": max(0, total - live_pro),
        "pro_live": live_pro,
        "pro_monthly": monthly,
        "pro_annual": annual,
        "pro_comped": comped,
        "pro_lapsed": lapsed,
        "dataset_buyers": download,
        "seeded_keys": seeded,
        "recent_signups": signups,
        "revenue_estimate": {
            "mrr_usd": round(
                monthly * s.pro_price_usd + annual * s.pro_annual_price_usd / 12, 2
            ),
            "one_time_usd": round(download * s.dataset_price_usd, 2),
            "basis": "live subscribers x list price",
            "caveat": (
                "Estimate only. No amount, currency or charge id is stored by "
                "this service -- Stripe is the system of record for money and "
                "there is no join between the two. Comps, legacy prices and "
                "refunds are all invisible here."
            ),
        },
    }
