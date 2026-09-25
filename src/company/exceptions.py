"""The exceptions feed: every filing that failed its check, and every restatement.

The thing no other fundamentals vendor sells, because it is the by-product of
the check they do not run. Two kinds of event, across every company and every
loaded period, dated by the filing that produced them:

* `failed_check` -- a balance sheet whose A = L + E does not hold, with the
  category `stats.classify_identity` gives it (the same function the home page
  counts with, so the feed and the headline number cannot disagree).
* `restatement` -- a later filing that changed a figure an earlier one reported
  for the same period. Equal re-reports (every 10-K repeats last year as a
  comparative) are not restatements.

Built from the whole table, so it is computed once and cached in process
rather than per request; FEED_TTL_S bounds how stale it gets. A new filing
reaches the feed on the next rebuild after it is loaded.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import structlog

log = structlog.get_logger(__name__)

FEED_TTL_S = 6 * 3600.0
# Restatement tracking covers the figures the balance sheet and statements show.
TRACKED = (
    "total_assets", "total_liabilities", "total_equity", "total_equity_incl_nci",
    "cash", "current_assets", "current_liabilities", "long_term_debt",
    "revenue", "net_income", "operating_income", "operating_cash_flow",
    "eps_diluted",
)

# Whose problem a failed check is. Only "filer" is an irregularity in the
# filing itself; "extraction" is a line that did not reach this dataset, and
# the feed says so rather than passing our gap off as the company's.
CATEGORY_MEANING: dict[str, tuple[str, str]] = {
    "broken": ("filer", "The filing's own totals disagree: total assets is not "
               "its stated total liabilities and equity."),
    "rounding": ("filer", "Off by under 1%, within the filer's presentation rounding."),
    "missing_tag": ("extraction", "The filer's own totals agree; a liability or "
                    "equity line did not reach this dataset."),
    "unexplained": ("unknown", "Does not balance, and the filing states no total "
                    "to check against."),
}

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "events": None}


def _iso(v: Any) -> str:
    """A date from a window function: a date on Postgres, a string on SQLite."""
    return v.isoformat() if hasattr(v, "isoformat") else str(v)[:10]


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, abs(a) * 1e-9)


def _compute() -> list[dict[str, Any]]:
    """Both event kinds, with the heavy lifting in SQL.

    The tracked metrics are ~1M rows; pulling them all into Python took over
    ten minutes over the wire. Window functions return only what matters: the
    latest value per (ticker, period, metric) for the identity, and only the
    rows whose value differs from the filing before them for restatements.
    """
    from sqlalchemy import and_, func, select

    from src.company.stats import FLAG_CATEGORIES, IDENTITY_METRICS, classify_identity
    from src.storage.db import session_scope
    from src.storage.models import Fundamental as F

    newest = func.row_number().over(
        partition_by=(F.ticker, F.period_end, F.metric),
        order_by=F.filing_date.desc(),
    ).label("rn")
    ident = (
        select(F.ticker, F.period_end, F.metric, F.filing_date, F.value, newest)
        .where(F.metric.in_(IDENTITY_METRICS))
        .subquery()
    )
    window = dict(partition_by=(F.ticker, F.metric, F.period_end), order_by=F.filing_date)
    moves = (
        select(
            F.ticker, F.metric, F.period_end, F.filing_date, F.value,
            func.lag(F.value).over(**window).label("prev"),
            func.lag(F.filing_date).over(**window).label("prev_filed"),
            func.first_value(F.value).over(**window).label("first"),
            func.first_value(F.filing_date).over(**window).label("first_filed"),
        )
        .where(F.metric.in_(TRACKED))
        .subquery()
    )
    with session_scope() as session:
        latest_rows = session.execute(
            select(ident.c.ticker, ident.c.period_end, ident.c.metric,
                   ident.c.filing_date, ident.c.value)
            .where(and_(ident.c.rn == 1, ident.c.value.is_not(None)))
        ).all()
        changed = session.execute(
            select(moves).where(and_(moves.c.prev.is_not(None),
                                     moves.c.value.is_not(None),
                                     moves.c.value != moves.c.prev))
        ).all()

    latest: dict[tuple[str, Any], dict[str, tuple[Any, float]]] = {}
    for ticker, period_end, metric, filed, value in latest_rows:
        latest.setdefault((ticker, period_end), {})[metric] = (filed, float(value))

    events: list[dict[str, Any]] = []
    for (ticker, period_end), got in latest.items():
        if "total_assets" not in got:
            continue
        category, drift = classify_identity({k: v for k, (_, v) in got.items()})
        if category not in FLAG_CATEGORIES:
            continue
        filed = max(_iso(f) for f, _ in got.values())
        equity = got.get("total_equity_incl_nci") or got.get("total_equity")
        events.append({
            "type": "failed_check",
            "date": filed,
            "ticker": ticker,
            "period_end": _iso(period_end),
            "filing_date": filed,
            "category": category,
            "attribution": CATEGORY_MEANING[category][0],
            "meaning": CATEGORY_MEANING[category][1],
            "drift_pct": round(drift, 4) if drift is not None else None,
            "total_assets": got["total_assets"][1],
            "total_liabilities": got["total_liabilities"][1] if "total_liabilities" in got else None,
            "total_equity": equity[1] if equity else None,
        })
    for r in changed:
        value, prev = float(r.value), float(r.prev)
        if _same(value, prev):
            continue
        change = value - prev
        events.append({
            "type": "restatement",
            "date": _iso(r.filing_date),
            "ticker": r.ticker,
            "period_end": _iso(r.period_end),
            "metric": r.metric,
            "previous": prev,
            "previously_filed": _iso(r.prev_filed),
            "revised": value,
            "filing_date": _iso(r.filing_date),
            "change": change,
            "change_pct": round(change / abs(prev) * 100, 4) if prev else None,
            "original": float(r.first) if r.first is not None else None,
            "originally_filed": _iso(r.first_filed),
        })
    events.sort(key=lambda e: (e["date"], e["ticker"]), reverse=True)
    return events


def _rebuild(fresh_since: float | None = None) -> list[dict[str, Any]]:
    with _lock:
        # Someone else built it while this caller waited for the lock.
        if fresh_since is not None and _cache["events"] is not None and _cache["at"] > fresh_since:
            return _cache["events"]
        started = time.monotonic()
        events = _compute()
        _cache.update(at=time.monotonic(), events=events)
        log.info("exceptions_feed_built", events=len(events),
                 seconds=round(time.monotonic() - started, 1))
        return events


def all_events(max_age_s: float = FEED_TTL_S) -> list[dict[str, Any]]:
    """Every event, newest first.

    Only the very first call (normally the boot warm-up) waits for the build.
    After that a stale copy is served while a fresh one builds in a thread:
    the build takes tens of seconds, and no request should wait on it.
    """
    events = _cache["events"]
    if events is None:
        return _rebuild(fresh_since=_cache["at"])
    if time.monotonic() - _cache["at"] >= max_age_s and not _lock.locked():
        threading.Thread(target=_rebuild, name="exceptions-feed", daemon=True).start()
    return events


def reset_cache() -> None:
    _cache.update(at=0.0, events=None)


def feed(*, since: dt.date | None = None, until: dt.date | None = None,
         kind: str | None = None, ticker: str | None = None,
         attribution: str | None = None,
         limit: int = 500, offset: int = 0) -> dict[str, Any]:
    """Filtered slice of the feed. `since`/`until` bound the filing date;
    `attribution=filer` keeps only failed checks in the filing itself."""
    events = all_events()
    lo = since.isoformat() if since else None
    hi = until.isoformat() if until else None
    picked = [
        e for e in events
        if (lo is None or e["date"] >= lo) and (hi is None or e["date"] <= hi)
        and (kind is None or e["type"] == kind)
        and (ticker is None or e["ticker"] == ticker)
        and (attribution is None or e.get("attribution") == attribution)
    ]
    return {
        "since": lo, "until": hi, "type": kind, "ticker": ticker,
        "attribution": attribution,
        "total": len(picked), "offset": offset, "limit": limit,
        "events": picked[offset: offset + limit],
    }


CSV_COLUMNS = (
    "type", "date", "ticker", "period_end", "filing_date", "category", "attribution",
    "drift_pct",
    "metric", "previous", "previously_filed", "revised", "change", "change_pct",
    "original", "originally_filed", "total_assets", "total_liabilities", "total_equity",
)


def to_csv(events: list[dict[str, Any]]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for e in events:
        w.writerow(e)
    return buf.getvalue()
