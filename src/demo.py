"""The home page's live demo: one real API call, no key in the browser.

The demo runs on a genuine account (`demo@to-scale.internal`) holding a genuine
key, and that key never leaves the process. The obvious build -- put the demo
key in the page and let the browser call `/api/company/{ticker}` -- publishes a
working credential to everyone who opens the site. The per-address limit would
then be trivially bypassed by lifting the key out of the HTML and calling the
real endpoint directly, which has no per-address limit at all.

So the browser calls `/api/demo/{ticker}`, the server attaches the key, and the
gate that matters sits on this side of it. The demo account is additionally
barred from the ordinary keyed API (see `accounts.get_current_user`), so even a
leaked demo key buys nothing the demo route does not already give away.

Anonymous callers are counted per salted address digest per UTC day, in a table
rather than in memory, because this platform restarts containers freely and an
in-process counter would make "five a day" mean "five per deploy".
"""

from __future__ import annotations

import structlog
from sqlalchemy import func, select

from src.config.settings import get_settings
from src.storage.db import session_scope
from src.storage.models import ApiUser, DemoUsage, current_day

log = structlog.get_logger(__name__)

# Fixed, and deliberately not a deliverable address: nobody should be able to
# register it by hand and inherit the demo account.
DEMO_EMAIL = "demo@to-scale.internal"


# Why the demo last failed to provision. Kept in memory rather than only in the
# log because the log is on the platform and the person debugging this is
# looking at /status: a swallowed exception that only a log sees is a fault
# nobody can act on. Never holds a key -- only the database's complaint.
_last_error: str = ""


def last_error() -> str:
    return _last_error


def is_enabled() -> bool:
    return bool(get_settings().demo_api_key)


def ensure_demo_user() -> bool:
    """Create or refresh the demo account. Idempotent; safe on every boot.

    Runs at startup rather than on first use so a cold container cannot serve
    the demo before the account exists, and so a changed DEMO_API_KEY takes
    effect on deploy instead of whenever the next visitor happens to try it.

    The account is Pro so that the visible limit is the per-address one. A demo
    sitting on the free tier would go dark for everybody on the tenth call of
    the month, which reads as a broken site rather than as a spent allowance.
    """
    from src import accounts

    global _last_error

    s = get_settings()
    if not s.demo_api_key:
        _last_error = "DEMO_API_KEY is not set"
        log.info("demo_disabled", reason=_last_error)
        return False
    try:
        with session_scope() as session:
            user = session.execute(
                select(ApiUser).where(ApiUser.email == DEMO_EMAIL)
            ).scalar_one_or_none()
            # The digest, like every other key. DEMO_API_KEY stays plaintext
            # in the environment -- it has to, the operator publishes it -- but
            # the database side of it is stored the same way as a customer's.
            hashed = accounts.hash_api_key(s.demo_api_key)
            prefix = accounts.key_prefix(s.demo_api_key)
            if user is None:
                session.add(
                    ApiUser(
                        email=DEMO_EMAIL,
                        api_key=hashed,
                        api_key_prefix=prefix,
                        subscription_tier="pro",
                        has_paid_download=False,
                    )
                )
                log.info("demo_user_created")
            else:
                user.api_key = hashed
                user.api_key_prefix = prefix
                user.subscription_tier = "pro"
                # Never the dataset. The demo shows one company, not the product.
                user.has_paid_download = False
    except Exception as exc:  # noqa: BLE001 - a missing demo must not stop boot
        _last_error = f"{type(exc).__name__}: {str(exc)[:220]}"
        log.warning("demo_user_setup_failed", error=_last_error)
        return False
    _last_error = ""
    return True


def account():
    """The demo account, provisioning it first if it is somehow not there.

    `_boot` creates it, but boot is a BACKGROUND task -- the server binds and
    answers before it finishes, deliberately, so a slow Postgres cannot fail the
    healthcheck. That leaves a window on a cold container where a visitor can
    reach the demo before the account exists. Rather than serve a confusing 503
    for the first second of every deploy, the lookup heals itself: one extra
    query on the unhappy path, and boot provisioning becomes an optimisation
    instead of a precondition.

    Returns None only when the demo is genuinely off (no DEMO_API_KEY) or the
    database is unreachable, both of which the caller reports as "not
    configured".
    """
    from src import accounts

    global _last_error

    key = get_settings().demo_api_key
    if not key:
        _last_error = "DEMO_API_KEY is not set"
        return None
    found = accounts.lookup(key)
    if found is None:
        ensure_demo_user()
        found = accounts.lookup(key)
        if found is None and not _last_error:
            # Provisioning reported success and the row still is not findable,
            # which means the key was written under a value that does not
            # compare equal to the one being looked up. Say that, rather than
            # leaving "not configured" to stand for it.
            _last_error = (
                "provisioned without error, but no account matches the key "
                f"({len(key)} characters) -- the stored value and the "
                "configured value differ"
            )
    return found


def calls_today(ip_hash: str, day: str | None = None) -> int:
    with session_scope() as session:
        return int(
            session.execute(
                select(func.count())
                .select_from(DemoUsage)
                .where(DemoUsage.ip_hash == ip_hash)
                .where(DemoUsage.day == (day or current_day()))
            ).scalar_one()
        )


UNLIMITED = -1


def remaining(ip_hash: str) -> int:
    """Calls left for this address today, or `UNLIMITED`.

    A zero limit means no cap, so it must not report zero REMAINING -- those
    are opposite states and returning the same number for both is how an
    uncapped demo would render as an exhausted one.
    """
    limit = get_settings().demo_calls_per_ip_per_day
    if not limit:
        return UNLIMITED
    return max(0, limit - calls_today(ip_hash))


def record(ip_hash: str, ticker: str) -> None:
    """Count one demo call. Never raises -- a lost row beats a failed demo."""
    try:
        with session_scope() as session:
            session.add(
                DemoUsage(ip_hash=ip_hash, day=current_day(), ticker=ticker[:16])
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("demo_record_failed", error=str(exc)[:200])
