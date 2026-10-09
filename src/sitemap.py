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
# URLs per file. The protocol's own ceiling is 50,000 (and 50 MB uncompressed);
# 10,000 keeps every file small enough to fetch quickly and to read in Search
# Console. Company pages are split into `company-1`, `company-2`, ... past it.
MAX_URLS = 10_000

_lock = threading.Lock()
# (built_at, base_url, {file name: entries})
_cache: tuple[float, str, dict] | None = None


# The tickers whose page actually renders, or None before it has been computed.
#
# `build_view1` is the SAME call `/company/{ticker}` makes to decide between a
# page and a 404, so this set cannot drift from the route the way a second SQL
# condition would. It is held here rather than recomputed per build because
# `get_balance_sheet` reads every fact a ticker has -- roughly 290 rows each,
# 1.8 million across the universe -- and doing that inside the memoised build
# would put a minute of work on whichever crawler request found the cache cold,
# with every other request queued behind the lock. That is the self-inflicted
# outage this module's docstring warns about, so the walk happens once at boot
# instead.
_drawable: frozenset[str] | None = None
_thin: frozenset[str] = frozenset()

# A page Google should not be asked to index. Two tests, either sufficient:
#
# * Under $10M in total assets. Below that the universe is shells, blank-check
#   vehicles and dormant registrants -- /company/BRLL is $135K -- and on a new
#   domain Search Console reported 5,516 of 6,251 URLs "discovered, currently
#   not indexed" (2026-09-23): Google is rationing crawl, and every shell in the
#   sitemap is a crawl not spent on a page someone searches for. BRLL's one
#   contribution was 71 impressions from a Bank of Baroda rate query it could
#   never satisfy.
# * No filing in 18 months. The page is a picture of a company that has stopped
#   reporting, and its "most recent balance sheet" is not recent.
#
# The pages still render and still link out -- `noindex,follow` -- so a reader
# who arrives by search or by the peer list loses nothing. Only the invitation
# to index is withdrawn.
THIN_ASSETS_USD = 10_000_000
THIN_STALE_DAYS = 548


def is_thin(total_assets: float | None, filing_date: dt.date | None,
            today: dt.date | None = None) -> bool:
    """Whether a company page is kept out of the sitemap and marked noindex."""
    if not total_assets or total_assets < THIN_ASSETS_USD:
        return True
    if filing_date is None:
        return False
    today = today or dt.date.today()
    return (today - filing_date).days > THIN_STALE_DAYS


def refresh_drawable() -> int:
    """Walk the candidates through `build_view1` and remember which render.

    Returns the number of drawable tickers. Called off the request path (boot,
    and after a fundamentals reload). Leaves the previous answer in place if it
    fails: a stale filter lists a handful of 404s, an empty one would drop six
    thousand real pages out of the sitemap.
    """
    global _drawable, _thin

    from src.company.view1 import build_view1

    try:
        candidates = [t for t, _ in _candidate_rows()]
        drawable = set()
        thin = set()
        for ticker in candidates:
            try:
                view = build_view1(ticker)
            except Exception:  # noqa: BLE001 - one ticker is not the run
                continue
            if view is None:
                continue
            drawable.add(ticker)
            if is_thin(view.total_assets, view.filing_date):
                thin.add(ticker)
        _drawable = frozenset(drawable)
        _thin = frozenset(thin)
        log.info(
            "sitemap_drawable_refreshed",
            candidates=len(candidates),
            drawable=len(_drawable),
            excluded=len(candidates) - len(_drawable),
            thin=len(_thin),
        )
    except Exception as exc:  # noqa: BLE001 - the sitemap still builds without it
        log.warning("sitemap_drawable_refresh_failed", error=str(exc)[:200])
        return -1
    reset_cache()
    return len(_drawable)


def _candidate_rows() -> list[tuple[str, dt.date | None]]:
    """(ticker, newest filing date) for every ticker with a positive assets row.

    A SUPERSET of the drawable set, and the source of `lastmod`. A ticker
    qualifies here on any period it has ever filed, while the page is drawn from
    the newest one -- so a filer whose latest period carries liabilities and
    equity but no total for assets passes this and still 404s. That gap is what
    `refresh_drawable` closes.

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


def _company_rows() -> list[tuple[str, dt.date | None]]:
    """The candidates, less the ones whose page would 404.

    Unfiltered until `refresh_drawable` has run. Listing a few URLs that answer
    404 is the behaviour this replaces; listing none would be worse.
    """
    rows = _candidate_rows()
    known = _drawable
    if known is None:
        log.info("sitemap_unfiltered", reason="drawable set not computed yet")
        return rows
    return [(t, d) for t, d in rows if t in known and t not in _thin]


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


# One entry per URL: (loc, lastmod as an ISO string or None, changefreq, priority).
Entry = tuple[str, str | None, str, str]


def _iso(d: dt.date | str | None) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, dt.date) else str(d)


def _newest(*dates: dt.date | str | None) -> str | None:
    """The latest of several dates, as an ISO string. ISO dates sort as text."""
    stamps = [x for x in (_iso(d) for d in dates) if x]
    return max(stamps) if stamps else None


# The order the index lists the files in. A page type that is not named here
# (a new URL prefix in `compare.PAGES`) still gets its own file, after these.
_GROUP_ORDER = ("pages", "blog", "compare", "alternatives", "best", "sector")


def build_groups(base_url: str) -> dict[str, list[Entry]]:
    """Every indexable URL, grouped by page type, one group per sitemap file.

    The host comes from the request rather than from a constant, so the files
    list the domain the crawler actually asked on -- which is what Search
    Console requires, and what stops a sitemap fetched at one hostname from
    advertising another.

    Only HTML pages that are indexable and canonical are listed. The two
    machine-readable disclosures (/llms.txt, /financial-data.txt) are text
    files for crawlers that read them from robots.txt, not pages to rank, so
    they are not here. Neither are /dashboard and /login (both `noindex`: a
    sitemap that lists a noindexed URL is two instructions that contradict
    each other), /search (400 without a query) or /docs (off in production).

    `lastmod` is a claim, so each date is one the page can defend: the filing
    date behind a company page, the date written on a post or a comparison page,
    the newest post for the blog index, and for the pages that print live counts
    the later of the newest filing and the legal-document date.
    """
    base = base_url.rstrip("/")

    newest = _newest_filing()
    try:
        from src.report.legal import LAST_UPDATED_ISO
        legal_date: str | None = LAST_UPDATED_ISO
    except Exception:  # noqa: BLE001 - a sitemap must not depend on prose
        legal_date = None

    # `newest` is None on a database with no loaded filings. Falling back to the
    # legal date keeps the guarantee unconditional: every URL carries a lastmod,
    # whatever the database holds.
    newest_iso = _iso(newest) or legal_date
    # Pages that print live counts (the strip, the coverage line) move when the
    # data does, or when somebody rewrites them: the later of the two.
    live = _newest(newest_iso, legal_date)

    posts = _blog_posts()
    blog_index = _newest(*[updated for _slug, updated in posts]) or legal_date

    compare_index = _newest(*[updated for _slug, updated in _compare_pages()]) or legal_date

    groups: dict[str, list[Entry]] = {}

    groups["pages"] = [
        (f"{base}/", newest_iso, "daily", "1.0"),
        (f"{base}/api", live, "monthly", "0.8"),
        (f"{base}/pricing", live, "monthly", "0.8"),
        (f"{base}/dataset", newest_iso, "monthly", "0.7"),
        (f"{base}/restatements", newest_iso, "daily", "0.7"),
        (f"{base}/about", legal_date, "monthly", "0.7"),
        (f"{base}/methodology", live, "monthly", "0.7"),
        (f"{base}/blog", blog_index, "weekly", "0.7"),
        (f"{base}/companies", newest_iso, "weekly", "0.7"),
        (f"{base}/compare", compare_index, "monthly", "0.7"),
        (f"{base}/terms", legal_date, "yearly", "0.3"),
        (f"{base}/privacy", legal_date, "yearly", "0.3"),
    ]
    # Product pages (`report.landing`), enumerated like the comparison pages so
    # the page, its route and its sitemap entry share one source.
    try:
        from src.report.landing import PAGES as LANDING_PAGES

        groups["pages"] += [
            (f"{base}/{p.slug}", p.updated, "monthly", "0.7")
            for p in LANDING_PAGES
        ]
    except Exception:  # noqa: BLE001 - a sitemap must not depend on a page module
        log.warning("sitemap_landing_pages_unavailable")
    groups["blog"] = [
        (f"{base}/blog/{slug}", _iso(updated), "monthly", "0.6")
        for slug, updated in posts
    ]
    # Enumerated from `compare.PAGES` rather than listed here, so adding a page
    # to that tuple is the only step. The slug's first segment names the file:
    # compare/, alternatives/ and best/ are three page types.
    for slug, updated in _compare_pages():
        kind = slug.split("/", 1)[0]
        groups.setdefault(kind, []).append(
            (f"{base}/{slug}", _iso(updated), "monthly", "0.6")
        )
    # `lastmod` is the newest filing date IN THAT SECTOR.
    groups["sector"] = [
        (f"{base}/sector/{slug}", _iso(updated), "weekly", "0.7")
        for slug, updated in _sector_pages()
    ]

    # Weekly, not daily: a balance sheet changes when the company files,
    # roughly four times a year. Claiming daily on six thousand pages that do
    # not change daily is how a sitemap stops being believed.
    companies = [
        (f"{base}/company/{ticker}", _iso(filed), "weekly", "0.6")
        for ticker, filed in _company_rows()
    ]
    for n, start in enumerate(range(0, len(companies), MAX_URLS), start=1):
        groups[f"company-{n}"] = companies[start:start + MAX_URLS]

    ordered: dict[str, list[Entry]] = {}
    for name in _GROUP_ORDER:
        if groups.get(name):
            ordered[name] = groups.pop(name)
    for name in sorted(groups):
        if groups[name]:
            ordered[name] = groups[name]
    log.info(
        "sitemap_built",
        files=len(ordered), urls=sum(len(v) for v in ordered.values()),
        companies=len(companies),
    )
    return ordered


def _urlset(entries: list[Entry]) -> str:
    body = "\n".join(_url(loc, last, freq, prio) for loc, last, freq, prio in entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>\n"
    )


def _index(base: str, groups: dict[str, list[Entry]]) -> str:
    items = []
    for name, entries in groups.items():
        last = _newest(*[e[1] for e in entries])
        when = f"\n    <lastmod>{escape(last)}</lastmod>" if last else ""
        items.append(
            f"  <sitemap>\n    <loc>{escape(base)}/sitemap-{name}.xml</loc>{when}\n  </sitemap>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(items)
        + "\n</sitemapindex>\n"
    )


def _groups_cached(base_url: str) -> dict[str, list[Entry]]:
    """`build_groups`, memoised for an hour and per host.

    Keyed on the base URL as well as on time: the same process answers on
    balanceproof.dev and on the Railway hostname, and a cache that ignored which
    one was asked would serve one domain's URLs to the other.
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
        out = build_groups(base)
        _cache = (time.monotonic(), base, out)
        return out


def xml(base_url: str) -> str:
    """The sitemap INDEX: one `<sitemap>` per page type, each with the newest
    `lastmod` of the URLs in it. This is what /sitemap.xml serves and what
    robots.txt names."""
    base = base_url.rstrip("/")
    return _index(base, _groups_cached(base))


def file_xml(base_url: str, name: str) -> str | None:
    """One child file (`/sitemap-{name}.xml`), or None if there is no such file.

    Only names the index lists exist, so the index and its children cannot
    disagree: a child that is not listed is a 404, and a listed child is a 200."""
    groups = _groups_cached(base_url.rstrip("/"))
    entries = groups.get(name)
    return _urlset(entries) if entries else None


def all_urls(base_url: str) -> list[tuple[str, str | None]]:
    """Every (loc, lastmod) across every file. For diagnostics and tests."""
    return [(e[0], e[1]) for v in _groups_cached(base_url.rstrip("/")).values() for e in v]


def reset_cache() -> None:
    """Forget the built file. For tests, and for anything that reloads data."""
    global _cache
    _cache = None


def reset_drawable() -> None:
    """Forget which tickers render. For tests."""
    global _drawable
    _drawable = None


def _sector_pages() -> list[tuple[str, str]]:
    """(slug, lastmod) per sector hub. Empty on failure, never raises.

    A sitemap that cannot be built is worse than one missing a section, so a
    broken sector read costs the hubs their entries and nothing else.
    """
    try:
        from src.report.sector_page import load_sectors

        return [
            (s["slug"], s["newest"].isoformat())
            for s in load_sectors() if s.get("newest")
        ]
    except Exception:  # noqa: BLE001
        log.warning("sitemap_sector_pages_unavailable")
        return []


def _compare_pages() -> list[tuple[str, str]]:
    """(slug, updated) for every comparison and decision page.

    Import is local and failure is swallowed for the same reason `_blog_posts`
    does it: a sitemap must not stop existing because a prose module raised.
    """
    try:
        from src.report.compare import PAGES

        return [(p.slug, p.updated) for p in PAGES]
    except Exception:  # noqa: BLE001 - a sitemap must not depend on prose
        log.warning("sitemap_compare_pages_unavailable")
        return []


def _blog_posts() -> list[tuple[str, str]]:
    """(slug, last-updated) for every published post.

    Read from the post registry rather than listed here, so publishing is one
    edit rather than two -- a sitemap that has to be remembered is a sitemap
    that goes stale.
    """
    try:
        from src.report.blog import POSTS

        return [(post.slug, post.updated) for post in POSTS]
    except Exception:  # noqa: BLE001 - a sitemap must not depend on prose
        return []


def robots(base_url: str) -> str:
    """robots.txt, whose only real job here is to name the sitemap.

    A sitemap nobody is told about is found only if it is submitted by hand, and
    this line is how every crawler that is not Google finds it. /admin is
    disallowed because it is an operator console, not a page -- it is already
    behind a secret, and a crawler wasting requests on it helps nobody.
    """
    base = base_url.rstrip("/")
    return "\n".join([
        "User-agent: *",
        "Allow: /",
        # An operator console, not a page. It IS behind a secret now -- the
        # note that used to claim so here was false for months -- and a
        # crawler spending requests on a login gate helps nobody.
        "Disallow: /admin",
        "Disallow: /api/",
        "",
        # AI crawlers, invited explicitly. Being quoted in a generated answer
        # is a distribution channel, and for a product whose pitch is a
        # verifiable accuracy claim it is a good one: the claim travels with
        # the citation. Named individually rather than left to the wildcard
        # because that is what these crawlers' operators ask for -- and it
        # documents which ones were considered rather than defaulted into.
        "User-agent: GPTBot",
        "User-agent: OAI-SearchBot",
        "User-agent: ChatGPT-User",
        "User-agent: PerplexityBot",
        "User-agent: Perplexity-User",
        "User-agent: ClaudeBot",
        "User-agent: Claude-User",
        "User-agent: Claude-SearchBot",
        "User-agent: Google-Extended",
        "User-agent: Applebot-Extended",
        "User-agent: CCBot",
        "User-agent: meta-externalagent",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /api/",
        "",
        # Not standard directives, and harmless to a crawler that ignores
        # them -- but none of these three has a discovery mechanism of its
        # own, and this is the first place an agent looks. Above the sitemap
        # line rather than below it, because an agent that came here for them
        # should not have to read past a declaration meant for a search
        # engine to find the ones meant for it.
        f"# llms.txt: {base}/llms.txt",
        f"# llms-full.txt: {base}/llms-full.txt",
        f"# financial data disclosure: {base}/financial-data.txt",
        "",
        f"Sitemap: {base}/sitemap.xml",
        "",
    ])
