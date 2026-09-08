"""The sitemap: every page worth indexing, and a real date on each one.

Two rules decide what goes in here, and both of them are the same rules the
rest of the site already follows.

**Only pages that have something on them.** The company list is the DRAWABLE
tickers -- the ones with a positive filed total for assets -- not every ticker
the fundamentals table has ever seen. A company with a handful of facts and
nothing to scale a drawing to renders a page that says so, and asking Google to
index six thousand of those would be asking it to index the empty state.

**Only dates that are true.** `lastmod` is a claim, and a crawler that catches
you making a false one learns to ignore the field. So a company page's date is
the filing date of the newest fact behind it -- which is genuinely the last time
that page's content changed -- and the legal pages carry the date written at the
top of the document. Nothing here is stamped "today" to look fresh.

Regenerating means a grouped scan over `fundamentals`, so the result is
memoised. The set changes when a quarter loads, four times a year; an hour of
staleness is invisible and a sequential scan on every crawler hit is not.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from xml.sax.saxutils import escape

import structlog
from sqlalchemy import func, select

from src.storage.db import session_scope
from src.storage.models import Fundamental

log = structlog.get_logger(__name__)

# An hour. The company set moves four times a year.
_TTL_S = 3600.0
# The protocol's own ceiling is 50,000 URLs (and 50 MB uncompressed) per file.
# At ~6,200 companies there is a lot of room, but a site that grows into an
# index file should find out by reading this note rather than by having Google
# reject the file.
MAX_URLS = 50_000

_lock = threading.Lock()
_cache: tuple[float, str, str] | None = None   # (built_at, base_url, xml)


def _company_rows() -> list[tuple[str, dt.date | None]]:
    """(ticker, newest filing date) for every company with a drawing.

    One grouped query rather than one per ticker: six thousand round trips to
    build a file a crawler reads once an hour would be a self-inflicted outage.
    """
    with session_scope() as session:
        rows = session.execute(
            select(Fundamental.ticker, func.max(Fundamental.filing_date))
            .where(Fundamental.metric == "total_assets", Fundamental.value > 0)
            .group_by(Fundamental.ticker)
            .order_by(Fundamental.ticker)
        ).all()
    return [(t, d) for t, d in rows if t]


def _newest_filing() -> dt.date | None:
    with session_scope() as session:
        return session.execute(select(func.max(Fundamental.filing_date))).scalar_one()


def _url(loc: str, lastmod: dt.date | str | None,
         changefreq: str, priority: str) -> str:
    when = ""
    if lastmod is not None:
        stamp = lastmod.isoformat() if isinstance(lastmod, dt.date) else str(lastmod)
        when = f"\n    <lastmod>{escape(stamp)}</lastmod>"
    return (
        f"  <url>\n"
        f"    <loc>{escape(loc)}</loc>{when}\n"
        f"    <changefreq>{changefreq}</changefreq>\n"
        f"    <priority>{priority}</priority>\n"
        f"  </url>"
    )


def build(base_url: str) -> str:
    """The XML, for a site served at `base_url` (no trailing slash).

    The host comes from the request rather than from a constant, so the file
    lists the domain the crawler actually asked on -- which is what Search
    Console requires, and what stops a sitemap fetched at one hostname from
    advertising another.
    """
    base = base_url.rstrip("/")
    parts: list[str] = []

    newest = _newest_filing()
    try:
        from src.report.legal import LAST_UPDATED_ISO
        legal_date: str | None = LAST_UPDATED_ISO
    except Exception:  # noqa: BLE001 - a sitemap must not depend on prose
        legal_date = None

    # The front door changes whenever a filing lands, because the hero drawing
    # and the counts on it are built from the data.
    parts.append(_url(f"{base}/", newest, "daily", "1.0"))
    parts.append(_url(f"{base}/api", legal_date, "monthly", "0.8"))
    parts.append(_url(f"{base}/dashboard", legal_date, "monthly", "0.7"))
    parts.append(_url(f"{base}/login", legal_date, "yearly", "0.3"))
    parts.append(_url(f"{base}/terms", legal_date, "yearly", "0.3"))
    parts.append(_url(f"{base}/privacy", legal_date, "yearly", "0.3"))

    companies = _company_rows()
    room = MAX_URLS - len(parts)
    if len(companies) > room:
        log.warning(
            "sitemap_truncated",
            companies=len(companies), room=room,
            note="past 50,000 URLs this needs to become a sitemap index",
        )
        companies = companies[:room]

    for ticker, filed in companies:
        # Weekly, not daily: a balance sheet changes when the company files,
        # roughly four times a year. Claiming daily on six thousand pages that
        # do not change daily is how a sitemap stops being believed.
        parts.append(
            _url(f"{base}/company/{ticker}", filed, "weekly", "0.6")
        )

    body = "\n".join(parts)
    log.info("sitemap_built", urls=len(parts), companies=len(companies))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def xml(base_url: str) -> str:
    """`build`, memoised for an hour and per host.

    Keyed on the base URL as well as on time: the same process answers on
    toscale.pro and on the Railway hostname, and a cache that ignored which one
    was asked would serve one domain's URLs to the other.
    """
    global _cache

    base = base_url.rstrip("/")
    now = time.monotonic()
    hit = _cache
    if hit is not None and hit[1] == base and now - hit[0] < _TTL_S:
        return hit[2]

    with _lock:
        # Re-check under the lock: without this, the first crawler hit after a
        # deploy starts one grouped scan per concurrent request.
        hit = _cache
        if hit is not None and hit[1] == base and now - hit[0] < _TTL_S:
            return hit[2]
        out = build(base)
        _cache = (time.monotonic(), base, out)
        return out


def reset_cache() -> None:
    """Forget the built file. For tests, and for anything that reloads data."""
    global _cache
    _cache = None


def robots(base_url: str) -> str:
    """robots.txt, whose only real job here is to name the sitemap.

    A sitemap nobody is told about is found only if it is submitted by hand, and
    this line is how every crawler that is not Google finds it. /admin is
    disallowed because it is an operator console, not a page -- it is already
    behind a secret, and a crawler wasting requests on it helps nobody.

    The private surfaces are listed as well as carrying `noindex` in their own
    markup, and the two are doing different jobs: `noindex` keeps a page out of
    the index if it is fetched, and this stops the fetch. Crawling /search with
    every query string a link ever carried is an unbounded set of near-
    duplicates, and /dashboard renders the signed-out state to a crawler --
    neither is worth anybody's request budget.
    """
    base = base_url.rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard\n"
        "Disallow: /login\n"
        "Disallow: /auth/\n"
        "Disallow: /search\n"
        "Disallow: /checkout\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
