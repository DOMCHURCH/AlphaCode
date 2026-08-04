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

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# The metric a page cannot be drawn without: there is no scale drawing without
# a positive total to scale to.
_REQUIRED_METRIC = "total_assets"

CANDIDATES: tuple[tuple[str, str], ...] = (
    ("JPM", "a bank"),
    ("MSFT", "a software company"),
    ("WMT", "a retailer"),
    ("FCX", "a miner"),
    ("AAL", "an airline"),
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
