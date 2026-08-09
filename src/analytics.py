"""Counting who looked at the site, and being exact about what that means.

"Accurate" here is two separate promises:

  1. EVERY qualifying request is recorded, once, and survives a restart. No
     sampling, no in-memory counters that reset when the container recycles,
     no rollups that throw away the detail.
  2. Nothing is called something it is not. A count of distinct IP hashes is
     reported as a count of addresses, not of people -- an office shares one
     and a phone moving from wifi to cellular produces two. Crawler traffic is
     kept and shown separately rather than quietly dropped, so the numbers
     reconcile against the server log instead of being a smaller mystery.

What is NOT counted, and why: /admin and /admin.json, because that is the
operator and the page polls itself every ten seconds; /static, /health,
/favicon.ico and /api, because they are assets and probes rather than
readers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Prefixes that are never a visit. /admin polls /admin.json every 10s while
# open, which would otherwise be most of the "traffic" on the site.
IGNORED_PREFIXES = (
    "/admin", "/static", "/health", "/favicon", "/api", "/status", "/reconcile",
)

# Substrings that identify automated clients. Matched case-insensitively
# against User-Agent. Not exhaustive -- no such list is -- so this splits
# traffic rather than claiming to have found every crawler.
_BOT_MARKERS = (
    "bot", "crawler", "spider", "slurp", "curl", "wget", "python-requests",
    "httpx", "headlesschrome", "phantomjs", "scrapy", "facebookexternalhit",
    "embedly", "quora link preview", "bitlybot", "skypeuripreview",
    "whatsapp", "telegrambot", "discordbot", "slackbot", "preview",
    "monitor", "uptime", "pingdom", "lighthouse", "gtmetrix",
)


def is_bot(user_agent: str | None) -> bool:
    if not user_agent:
        # No User-Agent at all is not a browser. Real ones always send it.
        return True
    ua = user_agent.lower()
    return any(m in ua for m in _BOT_MARKERS)


def hash_ip(ip: str) -> str:
    """A salted digest of the caller's address.

    Salted with the app secret so the digests are not reversible from a
    precomputed table of the IPv4 space, which a bare hash of an address is.
    """
    from src.config.settings import get_settings

    s = get_settings()
    salt = s.openrouter_api_key or s.api_key or "to-scale"
    return hashlib.sha256(f"visits:{salt}:{ip}".encode()).hexdigest()[:32]


def client_ip(headers: Any, fallback: str) -> str:
    """The visitor's address, not the proxy's.

    On Railway (and any platform that terminates TLS in front of the app)
    `request.client.host` is the load balancer, identical for everybody. Using
    it would collapse every visitor into one address and report the unique
    count as 1 forever -- a number that is not merely imprecise but wrong.

    The real address is the leftmost entry of X-Forwarded-For. That header is
    client-settable, so a determined visitor can inflate the unique count;
    that is a nuisance, and the alternative is a figure that is guaranteed
    wrong rather than occasionally gamed.
    """
    forwarded = headers.get("x-forwarded-for") or ""
    first = forwarded.split(",")[0].strip()
    if first:
        return first[:64]
    real = (headers.get("x-real-ip") or "").strip()
    return (real or fallback or "unknown")[:64]


def should_count(path: str) -> bool:
    return not any(path.startswith(p) for p in IGNORED_PREFIXES)


def record(
    path: str,
    ip: str,
    user_agent: str | None,
    *,
    status: int = 200,
    referrer: str | None = None,
) -> None:
    """Persist one view. Never raises -- counting must not break a page."""
    from src.storage.db import session_scope
    from src.storage.models import PageView

    ticker = None
    if path.startswith("/company/"):
        ticker = path.split("/company/", 1)[1].split("/", 1)[0][:16].upper() or None

    try:
        with session_scope() as s:
            s.add(PageView(
                path=path[:128],
                ticker=ticker,
                ip_hash=hash_ip(ip),
                is_bot=is_bot(user_agent),
                referrer=(referrer or None) and referrer[:128],
                status=int(status),
            ))
    except Exception as exc:  # noqa: BLE001 - a missed count beats a 500
        log.warning("pageview_record_failed", error=str(exc)[:200])


def _since(now: dt.datetime, days: int) -> dt.datetime:
    return now - dt.timedelta(days=days)


def summary(now: dt.datetime | None = None) -> dict[str, Any]:
    """Everything the /admin visitors panel shows.

    Windows are rolling, not calendar: "last 24 hours", not "since midnight",
    because a number that resets at midnight UTC looks like traffic collapsing
    every morning to anyone reading it from another timezone.
    """
    from sqlalchemy import distinct, func, select

    from src.storage.db import session_scope
    from src.storage.models import PageView

    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    out: dict[str, Any] = {"windows": {}, "top_pages": [], "top_tickers": []}

    try:
        with session_scope() as s:
            human = PageView.is_bot.is_(False)

            for label, days in (("24h", 1), ("7d", 7), ("30d", 30)):
                since = _since(now, days)
                views, uniques = s.execute(
                    select(
                        func.count(PageView.id),
                        func.count(distinct(PageView.ip_hash)),
                    ).where(human, PageView.created_at >= since)
                ).one()
                bots = s.execute(
                    select(func.count(PageView.id)).where(
                        PageView.is_bot.is_(True), PageView.created_at >= since
                    )
                ).scalar_one()
                out["windows"][label] = {
                    "views": int(views or 0),
                    "addresses": int(uniques or 0),
                    "bot_views": int(bots or 0),
                }

            total_views, total_uniques, first_seen, last_seen = s.execute(
                select(
                    func.count(PageView.id),
                    func.count(distinct(PageView.ip_hash)),
                    func.min(PageView.created_at),
                    func.max(PageView.created_at),
                ).where(human)
            ).one()
            out["all_time"] = {
                "views": int(total_views or 0),
                "addresses": int(total_uniques or 0),
                "first_seen": first_seen.isoformat() if first_seen else None,
                "last_seen": last_seen.isoformat() if last_seen else None,
            }
            out["bot_views_all_time"] = int(s.execute(
                select(func.count(PageView.id)).where(PageView.is_bot.is_(True))
            ).scalar_one() or 0)

            since30 = _since(now, 30)
            out["top_pages"] = [
                {"path": p, "views": int(n)}
                for p, n in s.execute(
                    select(PageView.path, func.count(PageView.id))
                    .where(human, PageView.created_at >= since30)
                    .group_by(PageView.path)
                    .order_by(func.count(PageView.id).desc())
                    .limit(8)
                ).all()
            ]
            out["top_tickers"] = [
                {"ticker": t, "views": int(n)}
                for t, n in s.execute(
                    select(PageView.ticker, func.count(PageView.id))
                    .where(human, PageView.ticker.isnot(None),
                           PageView.created_at >= since30)
                    .group_by(PageView.ticker)
                    .order_by(func.count(PageView.id).desc())
                    .limit(8)
                ).all()
            ]
    except Exception as exc:  # noqa: BLE001 - /admin must still render
        out["error"] = str(exc)[:200]
    return out
