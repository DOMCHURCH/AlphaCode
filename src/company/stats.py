"""The numbers the home page states about itself.

Counted live, never written down. A portfolio page with a hardcoded row count
is wrong the first time the data reloads, and a page that overstates its own
scale is a worse failure than one that says nothing -- the whole claim of this
project is that every figure traces to something real.

The counts are cheap SQL aggregates and run on every request. The accounting
identity is not: it re-derives the balance sheet for every company, so it is
computed once and cached, warmed in the background at startup. Until it is
ready `identity` is None and the page simply omits that line rather than
guessing at it.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# The identity check walks the whole universe, so it is recomputed at most this
# often. A reload is a manual, minutes-long operation -- half an hour of
# staleness on a headline statistic costs nothing.
IDENTITY_TTL_S = 1800.0

_identity_lock = threading.Lock()
_identity: dict[str, Any] | None = None
_identity_at: float = 0.0


def counts() -> dict[str, Any]:
    """Scale, straight out of the fundamentals table."""
    from sqlalchemy import distinct, func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    # NOT a count of distinct period_end. That counts fiscal year-ends across
    # companies, not datasets: seven quarterly downloads produce ninety-odd
    # distinct period ends because filers close their books on different days,
    # so reporting it as "92 quarters" overstates the load by an order of
    # magnitude. The measurable, honest span is the filing dates themselves.
    with session_scope() as s:
        facts, companies, earliest_filing, latest_filing = s.execute(
            select(
                func.count(Fundamental.id),
                func.count(distinct(Fundamental.ticker)),
                func.min(Fundamental.filing_date),
                func.max(Fundamental.filing_date),
            )
        ).one()
        # "Drawable" is not "present": a company with a handful of facts but no
        # positive total for assets has nothing to scale a drawing to, and
        # counting it would overstate what the site can actually show.
        drawable = s.execute(
            select(func.count(distinct(Fundamental.ticker))).where(
                Fundamental.metric == "total_assets", Fundamental.value > 0
            )
        ).scalar_one()

    return {
        "facts": int(facts or 0),
        "companies": int(companies or 0),
        "drawable": int(drawable or 0),
        "earliest_filing": earliest_filing.isoformat()
        if isinstance(earliest_filing, dt.date)
        else None,
        "latest_filing": latest_filing.isoformat()
        if isinstance(latest_filing, dt.date)
        else None,
    }


def _compute_identity() -> dict[str, Any] | None:
    """Pass rate for assets = liabilities + equity, across every company.

    Delegates to the same `run_universe_check` /admin uses. Re-deriving it here
    in cheaper SQL would mean two implementations of the identity that could
    disagree, and the one on the marketing page would be the one nobody checks.
    """
    from src.company.universe_check import run_universe_check

    try:
        result = run_universe_check()
    except Exception as exc:  # noqa: BLE001 - the page renders without it
        log.warning("site_identity_failed", error=str(exc)[:200])
        return None
    ident = result.get("identity") or {}
    if not ident.get("checkable"):
        return None
    return {
        "pass_rate_pct": ident.get("pass_rate_pct"),
        "checkable": ident.get("checkable"),
    }


def identity(max_age_s: float = IDENTITY_TTL_S) -> dict[str, Any] | None:
    """The cached identity result, or None if it has never been computed."""
    global _identity, _identity_at
    if _identity is not None and (time.monotonic() - _identity_at) < max_age_s:
        return _identity
    if not _identity_lock.acquire(blocking=False):
        # Another thread is already on it. Serve what we have, even if stale --
        # queueing a page render behind a whole-universe walk is not a trade
        # worth making.
        return _identity
    try:
        computed = _compute_identity()
        if computed is not None:
            _identity, _identity_at = computed, time.monotonic()
        return _identity
    finally:
        _identity_lock.release()


def warm_identity() -> None:
    """Compute the identity once at startup, off the critical path."""
    try:
        identity(max_age_s=0.0)
        log.info("site_identity_warm", result=_identity)
    except Exception as exc:  # noqa: BLE001
        log.warning("site_identity_warm_failed", error=str(exc)[:200])


def site_stats() -> dict[str, Any]:
    """Everything the home page states about the data behind it."""
    try:
        out = counts()
    except Exception as exc:  # noqa: BLE001 - the page must still render
        log.warning("site_counts_failed", error=str(exc)[:200])
        return {"facts": 0, "companies": 0, "drawable": 0,
                "earliest_filing": None, "latest_filing": None, "identity": None}
    out["identity"] = identity()
    return out
