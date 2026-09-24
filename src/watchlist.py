"""Watchlists and new-filing alerts -- the Pro feature (Starter gets three).

A watch is (account, ticker, last_seen_filing). The scheduler's `watch_alerts`
job compares each watch's `last_seen_filing` with the newest `filing_date`
`fundamentals` holds for that ticker; anything newer is a filing the owner has
not been told about. One email per account per run, listing every watched
company that filed, with:

- whether the new balance sheet reconciles (the site's own identity check),
- what moved since the previous period,
- anything that filing restated,
- links to the company page and the SEC filing.

`last_seen_filing` is set to the CURRENT newest filing when a watch is created.
Starting it empty would make the first run mail every new watcher about
filings that happened before they asked.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import func, select

from src.storage.db import session_scope

log = structlog.get_logger(__name__)

SITE = "https://balanceproof.dev"


def newest_filing(ticker: str) -> dt.date | None:
    from src.storage.models import Fundamental

    with session_scope() as session:
        return session.execute(
            select(func.max(Fundamental.filing_date)).where(Fundamental.ticker == ticker)
        ).scalar_one_or_none()


def _canonical(ticker: str) -> str:
    from src.company.lookup import canonical_ticker

    return canonical_ticker((ticker or "").strip().upper()).upper()


def items(account: Any) -> list[dict[str, Any]]:
    from src.storage.models import WatchItem

    with session_scope() as session:
        rows = session.execute(
            select(WatchItem).where(WatchItem.user_id == account.id)
            .order_by(WatchItem.ticker)
        ).scalars().all()
        return [{
            "ticker": r.ticker,
            "watching_since": r.created_at.isoformat() if r.created_at else None,
            "last_filing_seen": r.last_seen_filing.isoformat() if r.last_seen_filing else None,
            "last_alert_at": r.last_alert_at.isoformat() if r.last_alert_at else None,
        } for r in rows]


def add(account: Any, ticker: str) -> dict[str, Any]:
    """Watch a company. Idempotent; 403 past the plan's limit; 404 if unknown."""
    from src import plans
    from src.storage.models import WatchItem

    limit = plans.require(account.tier, "watchlist")
    symbol = _canonical(ticker)
    newest = newest_filing(symbol)
    if newest is None:
        raise HTTPException(404, f"No filed balance sheets for {symbol}.")
    with session_scope() as session:
        held = session.execute(
            select(WatchItem).where(WatchItem.user_id == account.id)
        ).scalars().all()
        if any(w.ticker == symbol for w in held):
            return {"ticker": symbol, "added": False, "count": len(held), "limit": limit}
        if limit is not None and len(held) >= limit:
            raise HTTPException(403, detail={
                "error": "watchlist_full",
                "limit": limit,
                "your_plan": plans.TIER_NAMES.get(account.tier, "Free"),
                "upgrade": f"{SITE}/pricing",
            })
        session.add(WatchItem(user_id=account.id, ticker=symbol,
                              last_seen_filing=newest))
    return {"ticker": symbol, "added": True, "count": len(held) + 1, "limit": limit}


def remove(account: Any, ticker: str) -> bool:
    from src.storage.models import WatchItem

    symbol = _canonical(ticker)
    with session_scope() as session:
        row = session.execute(
            select(WatchItem).where(WatchItem.user_id == account.id,
                                    WatchItem.ticker == symbol)
        ).scalar_one_or_none()
        if row is None:
            return False
        session.delete(row)
        return True


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def pending() -> list[dict[str, Any]]:
    """Every watch whose company has filed since it was last reported."""
    from src.storage.models import ApiUser, Fundamental, WatchItem

    with session_scope() as session:
        newest = (
            select(Fundamental.ticker, func.max(Fundamental.filing_date).label("newest"))
            .group_by(Fundamental.ticker).subquery()
        )
        rows = session.execute(
            select(WatchItem, ApiUser, newest.c.newest)
            .join(ApiUser, ApiUser.id == WatchItem.user_id)
            .join(newest, newest.c.ticker == WatchItem.ticker)
            .where((WatchItem.last_seen_filing.is_(None))
                   | (newest.c.newest > WatchItem.last_seen_filing))
        ).all()
        from src.accounts import effective_tier

        return [{
            "watch_id": w.id, "user_id": u.id, "email": u.email,
            "tier": effective_tier(u), "ticker": w.ticker,
            "last_seen": w.last_seen_filing, "newest": n,
        } for w, u, n in rows]


def _section(p: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The email paragraph and the structured payload for one watched filing."""
    from src.company.history import changes
    from src.company.view1 import build_view1
    from src.report.page_extras import filing_url
    from src.storage.models import FilingEvent

    t = p["ticker"]
    view = build_view1(t)
    ch = changes(t) or {}
    name = (view.company_name if view else None) or t
    new_restated = [
        r for r in ch.get("restatements", [])
        if p["last_seen"] is None or r["revised_in_filing"] > p["last_seen"].isoformat()
    ]
    with session_scope() as session:
        ev = session.execute(
            select(FilingEvent).where(FilingEvent.ticker == t,
                                      FilingEvent.filing_date == p["newest"])
            .order_by(FilingEvent.id.desc()).limit(1)
        ).scalar_one_or_none()
        sec = filing_url(ev.cik, ev.accession, ev.primary_doc) if ev else None
        form = ev.form if ev else None

    lines = [f"{name} ({t}) filed{' a ' + form if form else ''} on {p['newest'].isoformat()}"
             + (f", balance sheet as of {ch['period_end']}" if ch.get("period_end") else "") + "."]
    if view is not None:
        if view.balances:
            lines.append("  Reconciles: Assets = Liabilities + Equity holds"
                         + (f" ({view.identity_basis})" if view.identity_basis else "") + ".")
        else:
            lines.append(f"  Does NOT reconcile: off by {view.imbalance_pct:.2f}% of assets.")
    for key, label in (("total_assets", "Total assets"), ("total_liabilities", "Total liabilities"),
                       ("shareholders_equity", "Equity"), ("cash", "Cash")):
        d = (ch.get("since_previous_period") or {}).get(key)
        if d and d.get("change_pct") is not None:
            lines.append(f"  {label}: {d['change_pct']:+.1f}% vs previous period.")
    for r in new_restated[:5]:
        lines.append(f"  Restated: {r['metric']} for {r['period_end']} "
                     f"{r['previous']:,.0f} -> {r['current']:,.0f}.")
    lines.append(f"  {SITE}/company/{t}" + (f"  |  SEC: {sec}" if sec else ""))
    payload = {
        "ticker": t, "company": name, "filing_date": p["newest"].isoformat(),
        "form": form, "sec_url": sec, "reconciles": bool(view.balances) if view else None,
        "imbalance_pct": view.imbalance_pct if view else None,
        "changes": ch.get("since_previous_period"), "restatements": new_restated,
    }
    return "\n".join(lines), payload


def _mark(watch_ids: list[int], newest_by_id: dict[int, dt.date], alerted: bool) -> None:
    from src.storage.models import WatchItem

    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with session_scope() as session:
        for wid in watch_ids:
            row = session.get(WatchItem, wid)
            if row is not None:
                row.last_seen_filing = newest_by_id[wid]
                if alerted:
                    row.last_alert_at = now


def run_alerts() -> dict[str, int]:
    """Send every pending alert. One email per account. Never raises per user."""
    from src import mailer, plans

    by_user: dict[str, list[dict[str, Any]]] = {}
    for p in pending():
        by_user.setdefault(p["user_id"], []).append(p)
    sent = skipped = failed = 0
    for _uid, group in by_user.items():
        ids = [g["watch_id"] for g in group]
        newest = {g["watch_id"]: g["newest"] for g in group}
        tier = group[0]["tier"]
        if not plans.allowance(tier, "watchlist"):
            # Lapsed to Free: advance silently so a renewal does not unleash
            # a backlog of stale alerts.
            _mark(ids, newest, alerted=False)
            skipped += 1
            continue
        try:
            sections = [_section(g) for g in group]
        except Exception as exc:  # noqa: BLE001 - one bad company must not stop the run
            log.warning("watch_alert_build_failed", error=str(exc)[:200])
            failed += 1
            continue
        n = len(sections)
        subject = (f"BalanceProof: {group[0]['ticker']} filed" if n == 1
                   else f"BalanceProof: {n} companies you watch filed")
        text = ("New filings from companies on your BalanceProof watchlist.\n\n"
                + "\n\n".join(s for s, _ in sections)
                + f"\n\nManage your watchlist: {SITE}/dashboard\n")
        ok = mailer._send(group[0]["email"], subject=subject, text=text,
                          event="watch_alert_sent")
        if ok:
            _mark(ids, newest, alerted=True)
            sent += 1
            try:
                from src import webhooks

                webhooks.deliver_alert(group[0]["user_id"], [p for _, p in sections])
            except ImportError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("watch_webhook_failed", error=str(exc)[:200])
        else:
            failed += 1
    return {"sent": sent, "skipped": skipped, "failed": failed}
