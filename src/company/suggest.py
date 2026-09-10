"""Which tickers to offer a reader who hasn't typed one yet.

The five are chosen for CONTRAST, not size: a bank, a software company, a
retailer, a miner and an airline put five genuinely different balance-sheet
shapes next to each other. A bank is mostly loans funded by deposits; a
retailer is mostly stores and stock; an airline is mostly aircraft and debt.
Seeing them side by side is the argument the whole product makes.

Every suggestion is checked against the database before it is offered. Sending
a reader to a ticker with nothing behind it is the one thing an empty state
must not do -- it turns "we don't have that" into "this doesn't work".
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# The metric a page cannot be drawn without: there is no scale drawing without
# a positive total to scale to.
_REQUIRED_METRIC = "total_assets"

# One word each, saying WHY it is on the list. "an airline" describes AAL;
# "negative equity" says what makes its drawing worth looking at, which is the
# only reason any of these five are here.
CANDIDATES: tuple[tuple[str, str], ...] = (
    ("JPM", "bank"),
    ("MSFT", "software"),
    ("WMT", "retail"),
    ("FCX", "mining"),
    ("AAL", "negative equity"),
)


@dataclass(frozen=True)
class Suggestion:
    ticker: str
    kind: str  # "a bank" -- lowercase, reads as a sentence fragment


def tickers_with_data(tickers: tuple[str, ...]) -> set[str]:
    """Which of `tickers` have a positive filed total for assets."""
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    if not tickers:
        return set()
    try:
        with session_scope() as session:
            rows = session.execute(
                select(Fundamental.ticker)
                .where(
                    Fundamental.ticker.in_(tickers),
                    Fundamental.metric == _REQUIRED_METRIC,
                    Fundamental.value > 0,
                )
                .distinct()
            ).all()
    except Exception as exc:  # noqa: BLE001 - an empty list beats a broken page
        log.warning("suggest_lookup_failed", error=str(exc)[:200])
        return set()
    return {t for (t,) in rows}


def _fallback(limit: int) -> list[Suggestion]:
    """Any tickers that are actually drawable, when none of the five are.

    Alphabetical rather than "biggest" or "most interesting" -- a ranking is
    exactly what this product does not do, and an arbitrary one dressed up as a
    recommendation would be worse than an honest list.
    """
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    try:
        with session_scope() as session:
            rows = session.execute(
                select(Fundamental.ticker)
                .where(
                    Fundamental.metric == _REQUIRED_METRIC,
                    Fundamental.value > 0,
                )
                .distinct()
                .order_by(Fundamental.ticker)
                .limit(limit)
            ).all()
    except Exception as exc:  # noqa: BLE001
        log.warning("suggest_fallback_failed", error=str(exc)[:200])
        return []
    return [Suggestion(t, "") for (t,) in rows]


def suggestions(limit: int = 5) -> list[Suggestion]:
    """The tickers to offer, every one of them confirmed drawable."""
    have = tickers_with_data(tuple(t for t, _ in CANDIDATES))
    out = [Suggestion(t, kind) for t, kind in CANDIDATES if t in have][:limit]
    if out:
        return out
    log.info("suggest_using_fallback", candidates=[t for t, _ in CANDIDATES])
    return _fallback(limit)


# ---------------------------------------------------------------------------
# The home page's five drawings
# ---------------------------------------------------------------------------

# Fifteen minutes, matching `stats.COUNTS_TTL_S`. The five tickers are a fixed
# list and their filings move four times a year, so this is not a meaningful
# staleness -- and the alternative was measured: the home page called
# `build_view1` FIVE TIMES on every request, which together with the uncached
# counts made it a 1.35s TTFB against 0.23s for /pricing.
PANELS_TTL_S = 900.0

_panels_lock = threading.Lock()
_panels: list[tuple[Suggestion, Any]] | None = None
_panels_at: float = 0.0


def panels(max_age_s: float = PANELS_TTL_S) -> list[tuple[Suggestion, Any]]:
    """The suggested tickers, each paired with its drawing (or None). Memoised.

    Same cache shape as `stats.counts`: a caller arriving while another thread
    is building gets the previous value rather than queueing behind five
    balance-sheet reads, and `max_age_s=0` forces a rebuild and waits for it.

    A ticker whose drawing will not build is paired with None rather than
    dropped, and that None is CACHED like any other answer. Retrying five
    failing reads on every request would be the exact cost this exists to
    avoid, and the page already renders that case correctly -- the company is
    offered without a drawing, never with a placeholder.
    """
    global _panels, _panels_at

    fresh_enough = (
        _panels is not None and (time.monotonic() - _panels_at) < max_age_s
    )
    if fresh_enough:
        return list(_panels)

    forced = max_age_s <= 0
    if not _panels_lock.acquire(blocking=forced or _panels is None):
        return list(_panels) if _panels is not None else _build_panels()
    try:
        built = _build_panels()
        _panels, _panels_at = built, time.monotonic()
        return list(built)
    finally:
        _panels_lock.release()


def reset_panels_cache() -> None:
    """Forget the built drawings. Module state outlives any one database."""
    global _panels, _panels_at
    with _panels_lock:
        _panels, _panels_at = None, 0.0


def _build_panels() -> list[tuple[Suggestion, Any]]:
    """Five suggestions, drawn. One bad ticker must not take the page down."""
    from src.company.view1 import build_view1

    out: list[tuple[Suggestion, Any]] = []
    for s in suggestions():
        try:
            out.append((s, build_view1(s.ticker)))
        except Exception as exc:  # noqa: BLE001 - one bad ticker, not the page
            log.warning("home_thumbnail_failed", ticker=s.ticker, error=str(exc)[:200])
            out.append((s, None))
    return out


def warm_panels() -> None:
    """Build the five drawings once at startup, off the critical path.

    Mirrors `stats.warm_identity`. Without it the FIRST reader after a deploy
    pays for all five reads, and the first reader after a deploy is
    disproportionately likely to be a crawler measuring the site.
    """
    try:
        panels(max_age_s=0.0)
        log.info("home_panels_warm", panels=len(_panels or []))
    except Exception as exc:  # noqa: BLE001
        log.warning("home_panels_warm_failed", error=str(exc)[:200])
