"""Blog, company and comparison pages used to be three silos.

Every company page had exactly one outbound link -- the nav. Six thousand pages
passing authority to nothing, and a reader who wanted to understand what they
were looking at with nowhere to go. The blog linked to none of the companies it
was written about. The comparison pages linked one note.

What is asserted here is that the links exist, that they RESOLVE, and that they
are not the same links on every page. The last one matters most: a "related"
strip showing the same tickers everywhere is link stuffing with extra steps,
and it is the failure mode this shape slides into by default.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from src.report.blog import POSTS
from src.report.compare import PAGES

Q = dt.date(2025, 12, 31)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'links.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import app

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def seed(ticker: str, *, sector: str | None = None) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Inc"
        ))
        if sector:
            s.add(SectorMap(ticker=ticker, sector=sector))
        for metric, value in (
            ("total_assets", 1000.0),
            ("total_liabilities", 600.0),
            ("total_equity", 400.0),
        ):
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=Q,
                fiscal_period="FY", filing_date=dt.date(2026, 2, 1),
                source="sec", restated=False,
            ))


def links_in(html: str) -> set[str]:
    return set(re.findall(r'href="(/[^"#]*)', html))


# ---------------------------------------------------------------------------
# Blog -> companies, and blog -> blog
# ---------------------------------------------------------------------------

def test_every_post_links_to_the_companies_it_is_about(client):
    for post in POSTS:
        html = client.get(f"/blog/{post.slug}").text
        company_links = {ln for ln in links_in(html) if ln.startswith("/company/")}
        assert len(company_links) >= 2, (
            f"{post.slug} links to {len(company_links)} company pages"
        )


def test_the_company_links_differ_between_posts(client):
    """The anti-stuffing check. The same three tickers under every note would
    be a related-companies strip that is related to nothing."""
    per_post = []
    for post in POSTS:
        html = client.get(f"/blog/{post.slug}").text
        per_post.append(
            frozenset(ln for ln in links_in(html) if ln.startswith("/company/"))
        )
    assert len(set(per_post)) == len(per_post), (
        "two posts recommend an identical set of companies"
    )


def test_every_post_has_related_reading_to_the_others(client):
    assert len(POSTS) >= 2, "related reading needs something to relate to"
    for post in POSTS:
        html = client.get(f"/blog/{post.slug}").text
        assert 'id="related-reading"' in html, f"{post.slug} has no related reading"
        others = {f"/blog/{p.slug}" for p in POSTS if p.slug != post.slug}
        assert others <= links_in(html), f"{post.slug} does not link the others"


def test_no_post_recommends_itself(client):
    for post in POSTS:
        html = client.get(f"/blog/{post.slug}").text
        section = html.split('id="related-reading"', 1)[1]
        assert f"/blog/{post.slug}" not in section, f"{post.slug} recommends itself"


# ---------------------------------------------------------------------------
# Company -> blog
# ---------------------------------------------------------------------------

def test_a_company_page_offers_two_notes_and_the_product_links(client):
    seed("ACME")
    html = client.get("/company/ACME").text

    assert 'id="learn-more"' in html
    blog_links = {ln for ln in links_in(html) if ln.startswith("/blog/")}
    assert len(blog_links) == 2, f"expected two notes, got {sorted(blog_links)}"
    assert "/pricing" in links_in(html)
    assert "/api" in links_in(html)


def test_a_bank_is_pointed_at_the_bank_note(client):
    """A bank's page and a software company's page provoke different questions,
    so sending both to the same explainer wastes the link."""
    seed("JPM", sector="Financial Services")
    seed("PLAIN")

    bank = links_in(client.get("/company/JPM").text)
    plain = links_in(client.get("/company/PLAIN").text)

    assert "/blog/why-bank-balance-sheets-are-different" in bank
    assert "/blog/why-bank-balance-sheets-are-different" not in plain
    assert "/blog/understanding-the-accounting-identity" in plain


# ---------------------------------------------------------------------------
# Comparison -> blog
# ---------------------------------------------------------------------------

def test_every_comparison_page_links_two_notes(client):
    for page in PAGES:
        html = client.get(f"/{page.slug}").text
        blog_links = {ln for ln in links_in(html) if ln.startswith("/blog/")}
        assert len(blog_links) >= 2, f"{page.slug} links {len(blog_links)} notes"


# ---------------------------------------------------------------------------
# The links have to actually resolve
# ---------------------------------------------------------------------------

def test_every_internal_link_added_here_resolves(client):
    """A link that 404s is worse than no link: it spends crawl budget and tells
    a reader the site is broken."""
    # Every ticker any post links to, so this checks LINK INTEGRITY rather
    # than fixture coverage. Each was confirmed to resolve on production
    # before being written into `_POST_COMPANIES` -- a hand-picked link is only
    # as good as the page behind it, and nothing else in the suite would notice
    # a post pointing at a ticker the universe does not carry. A post naming a
    # ticker missing from this list fails here as a broken link, which is the
    # test working: add the ticker, do not drop the link.
    for ticker in ("JPM", "BAC", "GS", "WFC", "MSFT", "AAL", "WMT", "FCX",
                   "AAPL"):
        seed(ticker, sector="Financial Services" if ticker == "JPM" else None)

    pages = ["/company/JPM", "/blog"]
    pages += [f"/blog/{p.slug}" for p in POSTS]
    pages += [f"/{p.slug}" for p in PAGES]

    checked: set[str] = set()
    wanted = (
        "/company/", "/blog/", "/compare/", "/best/", "/alternatives/",
        "/pricing", "/api", "/dataset",
    )
    for page in pages:
        html = client.get(page).text
        checked.update(ln for ln in links_in(html) if ln.startswith(wanted))

    assert checked, "no internal links were found to check"
    for link in sorted(checked):
        code = client.get(link).status_code
        assert code == 200, f"{link} answered {code}"


def test_the_new_posts_are_in_the_sitemap(client):
    """The sitemap enumerates POSTS, so this guards the wiring rather than a
    hand-kept list -- but a post nobody can find is a post nobody reads."""
    sitemap = client.get("/sitemap.xml").text
    for slug in (
        "why-bank-balance-sheets-are-different",
        "understanding-the-accounting-identity",
    ):
        assert f"/blog/{slug}" in sitemap, f"{slug} is not in the sitemap"


# ---------------------------------------------------------------------------
# The comparison pages have to be reachable
# ---------------------------------------------------------------------------
# These are the pages with commercial intent on them, and every one of them
# had exactly one inlink: the sitemap. A page reachable only from the sitemap
# is a page a crawler discovers and a reader does not, and the reader case is
# the one that matters -- somebody landing on the Intrinio comparison from
# search had no route to the roundup that would actually have answered them.

MIN_INLINKS = 4


def _pages_that_could_link() -> list[str]:
    """Every page that is allowed to count as an inlink.

    Deliberately NOT the sitemap. Counting it would let all five pages pass
    on the single link they already had, which is the state this test exists
    to stop.
    """
    pages = ["/", "/pricing", "/api", "/blog", "/methodology", "/dataset"]
    pages += [f"/blog/{p.slug}" for p in POSTS]
    pages += [f"/{p.slug}" for p in PAGES]
    return pages


def test_every_comparison_page_has_at_least_four_inlinks(client):
    seed("JPM", sector="Financial Services")

    targets = [f"/{p.slug}" for p in PAGES]
    found: dict[str, set[str]] = {t: set() for t in targets}

    for source in _pages_that_could_link():
        r = client.get(source)
        assert r.status_code == 200, f"{source} answered {r.status_code}"
        hrefs = set(links_in(r.text))
        for target in targets:
            if target in hrefs and target != source:
                found[target].add(source)

    thin = {
        t: sorted(v) for t, v in found.items() if len(v) < MIN_INLINKS
    }
    assert not thin, (
        f"under {MIN_INLINKS} inlinks: "
        + "; ".join(f"{t} has {len(v)} ({v})" for t, v in thin.items())
    )


def test_a_comparison_page_does_not_link_to_itself(client):
    """The cross-link block is rendered from the same list the page is in, so
    the self-exclusion is a real thing to get wrong, and a self-link in a
    "read these instead" list reads as a bug to anyone who clicks it."""
    seed("JPM", sector="Financial Services")

    for page in PAGES:
        html = client.get(f"/{page.slug}").text
        block = html.split('id="compare-others"', 1)
        assert len(block) == 2, f"/{page.slug} has no Compare section"
        assert f'href="/{page.slug}"' not in block[1].split("</section>", 1)[0]
