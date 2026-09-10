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

# Coverage is "what fraction of companies file this line", used to decide which
# line on a card is the distinctive one. Under this many companies the answer
# says more about the sample than about filers, so it is reported as unknown.
MIN_COMPANIES_FOR_COVERAGE = 100

_identity_lock = threading.Lock()
_identity: dict[str, Any] | None = None
_identity_at: float = 0.0

# How long the scale figures may be stale. Fifteen minutes against a table that
# moves four times a year is not a meaningful staleness -- and the alternative
# was measured: `counts()` was uncached, so every home page render paid a
# COUNT(*), a COUNT(DISTINCT ticker) and a MIN/MAX over 1.24M rows. That was
# most of a 1.35s TTFB on the one page every visitor sees first.
COUNTS_TTL_S = 900.0

_counts_lock = threading.Lock()
_counts: dict[str, Any] | None = None
_counts_at: float = 0.0


def counts(max_age_s: float = COUNTS_TTL_S) -> dict[str, Any]:
    """Scale, straight out of the fundamentals table. Memoised.

    Same shape of cache as `identity()` below, and for the same reason: a
    figure that changes when a quarter loads must not be recomputed for every
    reader. `max_age_s=0` forces a fresh count and WAITS for it, which is what
    a test asserting on freshly seeded rows needs.

    A caller that arrives while another thread is counting is handed the
    previous value rather than queued behind a full-table scan. On the first
    ever call there is no previous value, so that caller does the work.
    """
    global _counts, _counts_at

    fresh_enough = (
        _counts is not None and (time.monotonic() - _counts_at) < max_age_s
    )
    if fresh_enough:
        return dict(_counts)

    forced = max_age_s <= 0
    if not _counts_lock.acquire(blocking=forced or _counts is None):
        return dict(_counts) if _counts is not None else _compute_counts()
    try:
        computed = _compute_counts()
        _counts, _counts_at = computed, time.monotonic()
        return dict(computed)
    finally:
        _counts_lock.release()


def reset_counts_cache() -> None:
    """Forget the counted figures. Module state outlives any one database."""
    global _counts, _counts_at
    with _counts_lock:
        _counts, _counts_at = None, 0.0


def _compute_counts() -> dict[str, Any]:
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
        # `generated` is when the newest row was WRITTEN, which is not
        # `latest_filing` -- the newest thing filed. It is the snapshot date the
        # dataset is sold under ("as of ..."), and the two differ by however
        # long the load lagged the filing, which is the honest gap to show.
        facts, companies, earliest_filing, latest_filing, generated = s.execute(
            select(
                func.count(Fundamental.id),
                func.count(distinct(Fundamental.ticker)),
                func.min(Fundamental.filing_date),
                func.max(Fundamental.filing_date),
                func.max(Fundamental.ingested_at),
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
        "generated": generated if isinstance(generated, (dt.date, dt.datetime))
        else None,
    }


_coverage_lock = threading.Lock()
_coverage: dict[str, float] | None = None
_coverage_at: float = 0.0


def _compute_coverage() -> dict[str, float]:
    """{metric -> fraction of companies that report it at all}.

    One GROUP BY, no joins. Used to decide which line on a card is the
    distinctive one: a metric almost nobody files (loans, deposits) says more
    about a company than one almost everybody files (property & equipment).
    """
    from sqlalchemy import distinct, func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        rows = s.execute(
            select(Fundamental.metric, func.count(distinct(Fundamental.ticker)))
            .group_by(Fundamental.metric)
        ).all()
        total = s.execute(
            select(func.count(distinct(Fundamental.ticker)))
        ).scalar_one()
    if not total or total < MIN_COMPANIES_FOR_COVERAGE:
        # Below this, "rare" is an artifact of the sample rather than a fact
        # about how companies file: in a five-company database one filer with
        # receivables makes receivables look as unusual as bank deposits.
        # Unknown is reported as unknown, and the caller falls back to size.
        log.info("component_coverage_sample_too_small", companies=int(total or 0))
        return {}
    return {metric: n / total for metric, n in rows}


def component_coverage(max_age_s: float = IDENTITY_TTL_S) -> dict[str, float]:
    """Cached coverage. Returns {} rather than raising -- a card falls back to
    its largest block, which is only a worse label, not a wrong one."""
    global _coverage, _coverage_at
    if _coverage is not None and (time.monotonic() - _coverage_at) < max_age_s:
        return _coverage
    if not _coverage_lock.acquire(blocking=False):
        return _coverage or {}
    try:
        _coverage = _compute_coverage()
        _coverage_at = time.monotonic()
    except Exception as exc:  # noqa: BLE001
        log.warning("component_coverage_failed", error=str(exc)[:200])
        _coverage = _coverage or {}
    finally:
        _coverage_lock.release()
    return _coverage


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
    """The cached identity result, or None if it has never been computed.

    `max_age_s=0` means "computed from the data as it is NOW" and is honoured
    as such: such a caller WAITS for whoever holds the lock rather than being
    handed the previous value. It used to be handed the previous value with no
    way to tell, which made a startup warm capable of publishing a figure
    derived from data that is no longer there.

    Every other caller is a page render, and for those the original trade still
    holds: serve what we have, even if stale, rather than queue a render behind
    a whole-universe walk.
    """
    global _identity, _identity_at
    fresh_enough = (
        _identity is not None and (time.monotonic() - _identity_at) < max_age_s
    )
    if fresh_enough:
        return _identity

    forced = max_age_s <= 0
    if not _identity_lock.acquire(blocking=forced):
        return _identity
    try:
        computed = _compute_identity()
        if computed is not None:
            _identity, _identity_at = computed, time.monotonic()
        return _identity
    finally:
        _identity_lock.release()


def reset_identity_cache() -> None:
    """Forget the computed identity.

    `_identity` is module state, so it outlives any one database. Tests that
    swap the database underneath the process must clear it the same way they
    reset the engine and lookup caches -- otherwise a figure counted from one
    test's data is served to the next.
    """
    global _identity, _identity_at
    with _identity_lock:
        _identity, _identity_at = None, 0.0


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
                "earliest_filing": None, "latest_filing": None,
                "generated": None, "identity": None}
    out["identity"] = identity()
    return out
