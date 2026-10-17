"""Two directory pages that give the deep pages a parent: /companies and /compare.

/companies lists every indexable company A-Z and every sector hub; /compare lists
every comparison, alternatives and "best" page. Before these, the comparison pages
had no link from a company page, a sector page or the footer, and the only route
from the home page to the other 5,000 companies was through one of five.

Both are linked from the footer, which is on every page, so each is one click from
anywhere and the pages under them are two.
"""

from __future__ import annotations

import threading
import time
from html import escape
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_TTL_S = 3600.0
_lock = threading.Lock()
_cache: tuple[float, list[tuple[str, list[tuple[str, str]]]]] | None = None


def _directory() -> list[tuple[str, list[tuple[str, str]]]]:
    """[(letter, [(ticker, name), ...]), ...] for every company the sitemap lists.

    The same set as the sitemap on purpose: a directory that links a page the
    sitemap leaves out (a `noindex` shell) would be a second opinion about what
    is worth indexing. Memoised for an hour, like the sitemap.
    """
    global _cache
    now = time.monotonic()
    hit = _cache
    if hit is not None and now - hit[0] < _TTL_S:
        return hit[1]
    with _lock:
        hit = _cache
        if hit is not None and time.monotonic() - hit[0] < _TTL_S:
            return hit[1]
        out = _build_directory()
        if out:
            _cache = (time.monotonic(), out)
        return out


def _build_directory() -> list[tuple[str, list[tuple[str, str]]]]:
    from sqlalchemy import select

    from src import sitemap
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    try:
        tickers = [t for t, _filed in sitemap._company_rows()]
        with session_scope() as session:
            names = dict(session.execute(
                select(UniverseSnapshot.ticker, UniverseSnapshot.name)
                .order_by(UniverseSnapshot.as_of_date)
            ).all())
    except Exception as exc:  # noqa: BLE001 - a directory must not take a page down
        log.warning("companies_directory_failed", error=str(exc)[:200])
        return []

    grouped: dict[str, list[tuple[str, str]]] = {}
    for ticker in tickers:
        name = (names.get(ticker) or ticker).strip()
        first = name[:1].upper()
        letter = first if first.isalpha() else "#"
        grouped.setdefault(letter, []).append((ticker, name))
    for items in grouped.values():
        items.sort(key=lambda it: (it[1].lower(), it[0]))
    return sorted(grouped.items(), key=lambda kv: ("~" if kv[0] == "#" else kv[0]))


def render_companies(*, nav: str = "", sectors: list[dict[str, Any]] | None = None) -> str:
    from src.report.home_page import shell
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld

    directory = _directory()
    total = sum(len(items) for _letter, items in directory)

    letters = " ".join(
        f'<a href="#letter-{"other" if letter == "#" else letter}">{escape(letter)}</a>'
        for letter, _items in directory
    )
    sector_items = "".join(
        f'<li><a href="/sector/{escape(s["slug"])}">{escape(s["sector"])}</a>'
        f' <span class="muted">{s["companies"]:,}</span></li>'
        for s in (sectors or [])
    )
    sector_block = f"""
    <section class="sec" id="by-sector">
      <div class="sec-head"><h2>By sector</h2></div>
      <p class="sec-sub">Each sector page ranks its companies by total assets and
        shows how many of them balance.</p>
      <ul class="notelist sectorlist">{sector_items}</ul>
    </section>""" if sector_items else ""

    sections = "".join(
        f"""
    <section class="sec" id="letter-{"other" if letter == "#" else letter}">
      <div class="sec-head"><h2>{escape(letter)}</h2></div>
      <ul class="notelist companylist">{"".join(
            f'<li><a href="/company/{escape(t)}">{escape(n)}</a> <span class="muted">{escape(t)}</span></li>'
            for t, n in items
        )}</ul>
    </section>"""
        for letter, items in directory
    )

    count = f"{total:,}" if total else "Every"
    body = f"""{nav}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <h1 class="htitle">SEC-reporting companies, A to Z</h1>
      <p class="hlede">{count} companies, each with its latest balance sheet from
        SEC EDGAR drawn to scale and checked against Assets = Liabilities +
        Equity where the filing allows. Pick a letter or a sector.</p>
      <p class="sec-sub letterbar">{letters}</p>
    </header>
    {sector_block}
    {sections}
  </article>
{render_footer()}
</main>"""
    return shell(
        "Companies A to Z, balance sheets checked — BalanceProof",
        body,
        description=(
            f"{count} companies A to Z, each with its latest SEC balance sheet "
            "drawn to scale and checked against A = L + E where testable. Browse by letter or sector."
        ),
        canonical="/companies",
        ld=breadcrumb_ld([("Home", "/"), ("Companies", "/companies")]),
    )


_KINDS = (
    ("compare", "Head to head", "BalanceProof against one other data source."),
    ("alternatives", "Alternatives", "What to look for in a replacement for a named product."),
    ("best", "How to choose", "What to test before you pick a filings or fundamentals API."),
)


def render_compare_index(*, nav: str = "") -> str:
    from src.report.compare import PAGES
    from src.report.home_page import shell
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld

    blocks = []
    for kind, heading, blurb in _KINDS:
        pages = [p for p in PAGES if p.slug.split("/", 1)[0] == kind]
        if not pages:
            continue
        items = "".join(
            f'<li><a href="/{escape(p.slug)}">{escape(p.h1)}</a></li>' for p in pages
        )
        blocks.append(f"""
    <section class="sec" id="{kind}">
      <div class="sec-head"><h2>{escape(heading)}</h2></div>
      <p class="sec-sub">{escape(blurb)}</p>
      <ul class="notelist">{items}</ul>
    </section>""")

    from src.report.landing import PAGES as LANDING_PAGES

    if LANDING_PAGES:
        items = "".join(
            f'<li><a href="/{escape(p.slug)}">{escape(p.h1)}</a></li>'
            for p in LANDING_PAGES
        )
        blocks.insert(0, f"""
    <section class="sec" id="product">
      <div class="sec-head"><h2>The product</h2></div>
      <p class="sec-sub">What the API does, with a real response from it.</p>
      <ul class="notelist">{items}</ul>
    </section>""")

    body = f"""{nav}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <h1 class="htitle">BalanceProof compared</h1>
      <p class="hlede">Every comparison, alternatives and buyer's-guide page in one
        place. They say where another product is the better fit as well as where
        this one is.</p>
    </header>
    {"".join(blocks)}
    <section class="sec">
      <p class="sec-sub">The method behind every figure is on
        <a href="/methodology">how we verify</a>; the study that motivates it is
        <a href="/blog/sec-xbrl-data-wrong-one-in-five">why naive XBRL extraction is
        wrong 1 in 5 times</a>; how many filings fail and why is in
        <a href="/blog/which-sec-balance-sheets-fail-the-identity-check">the report on 38,568 balance sheets</a>.</p>
    </section>
  </article>
{render_footer()}
</main>"""
    return shell(
        "SEC data API comparisons and alternatives — BalanceProof",
        body,
        description=(
            "BalanceProof against sec-api.io, Financial Modeling Prep and other "
            "SEC data APIs: head-to-head pages, alternatives and how to choose."
        ),
        canonical="/compare",
        ld=breadcrumb_ld([("Home", "/"), ("Compare", "/compare")]),
    )
