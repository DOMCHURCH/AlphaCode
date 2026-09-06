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
    s = get_settings()
    if not s.demo_api_key:
        log.info("demo_disabled", reason="DEMO_API_KEY is not set")
        return False
    try:
        with session_scope() as session:
            user = session.execute(
                select(ApiUser).where(ApiUser.email == DEMO_EMAIL)
            ).scalar_one_or_none()
            if user is None:
                session.add(
                    ApiUser(
                        email=DEMO_EMAIL,
                        api_key=s.demo_api_key,
                        subscription_tier="pro",
                        has_paid_download=False,
                    )
                )
                log.info("demo_user_created")
            else:
                user.api_key = s.demo_api_key
                user.subscription_tier = "pro"
                # Never the dataset. The demo shows one company, not the product.
                user.has_paid_download = False
    except Exception as exc:  # noqa: BLE001 - a missing demo must not stop boot
        log.warning("demo_user_setup_failed", error=str(exc)[:200])
        return False
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

    key = get_settings().demo_api_key
    if not key:
        return None
    found = accounts.lookup(key)
    if found is None:
        ensure_demo_user()
        found = accounts.lookup(key)
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


def remaining(ip_hash: str) -> int:
    limit = get_settings().demo_calls_per_ip_per_day
    if not limit:
        return 0
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
