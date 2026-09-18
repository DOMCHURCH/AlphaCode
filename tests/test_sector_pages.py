"""Sector hubs: the parent the 6,189 company pages did not have.

The site's own SEO problem, stated plainly: the sitemap lists 6,189 company
pages and almost nothing links to any of them. Five from the home page, none
from /methodology, four peers from each other. A page reachable only through a
sitemap is a page the crawler has been told not to care about.

What is pinned here is the link, the content that makes the parent worth
indexing, and the one mistake that would silently undo the whole thing -- a
chip rendered into stored `intro_html` rather than on the live path.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
PE = dt.date(2026, 6, 30)
FD = dt.date(2026, 8, 6)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sector.db'}")
    monkeypatch.setenv("API_KEY", "")
    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()
    _seed()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed() -> None:
    """Eight financials, six industrials, six with no sector at all."""
    from src.storage.db import session_scope
    from src.storage.models import (
        Fundamental, SectorMap, UniverseSnapshot,
    )

    plan = (
        [(f"FIN{i}", "Financials", 1_000_000_000.0 * (i + 1)) for i in range(8)]
        + [(f"IND{i}", "Industrials", 500_000_000.0 * (i + 1)) for i in range(6)]
        + [(f"ORP{i}", None, 100_000_000.0 * (i + 1)) for i in range(6)]
    )
    with session_scope() as s:
        for ticker, sector, assets in plan:
            if sector:
                s.add(SectorMap(ticker=ticker, cik="0", sector=sector,
                                sector_source="sic"))
            s.add(UniverseSnapshot(
                as_of_date=PE, ticker=ticker, name=f"{ticker} Holdings",
            ))
            liab, eq = assets * 0.6, assets * 0.4
            for metric, value in (
                ("total_assets", assets),
                ("total_liabilities", liab),
                ("total_equity", eq),
            ):
                s.add(Fundamental(
                    ticker=ticker, metric=metric, value=value, period_end=PE,
                    fiscal_period="FY", filing_date=FD, source="sec",
                    restated=False,
                ))


def test_a_hub_lists_its_sector_ranked_by_assets(db):
    from src.report.sector_page import build_sector

    data = build_sector("financials")
    assert data is not None
    assert data["sector"] == "Financials"
    assert len(data["companies"]) == 8
    assets = [c["assets"] for c in data["companies"]]
    assert assets == sorted(assets, reverse=True), "largest first"
    assert data["companies"][0]["name"] == "FIN7 Holdings"


def test_companies_with_no_sector_still_get_a_parent(db):
    """An unclassified company is orphaned twice, so it needs the link most."""
    from src.report.sector_page import build_sector

    data = build_sector("unclassified")
    assert data is not None
    assert data["unclassified"] is True
    assert {c["ticker"] for c in data["companies"]} == {
        f"ORP{i}" for i in range(6)
    }


def test_a_sector_too_small_to_be_worth_a_page_gets_none(db):
    """A hub of two companies is the thin page this is meant to prevent."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap
    from src.report.sector_page import load_sectors

    with session_scope() as s:
        s.add(SectorMap(ticker="TINY", cik="0", sector="Utilities",
                        sector_source="sic"))
        s.add(Fundamental(
            ticker="TINY", metric="total_assets", value=1.0, period_end=PE,
            fiscal_period="FY", filing_date=FD, source="sec", restated=False,
        ))

    assert "utilities" not in {s["slug"] for s in load_sectors()}


def test_the_route_renders_and_an_unknown_slug_is_a_404(db):
    from src.api import app

    client = TestClient(app)

    ok = client.get("/sector/financials")
    assert ok.status_code == 200
    body = ok.text
    assert "Financials balance sheets" in body
    # The links that are the entire point.
    assert '/company/FIN7' in body
    assert 'href="/sector/industrials"' in body, "hubs must link each other"

    assert client.get("/sector/nope").status_code == 404


def test_the_company_page_chip_links_to_the_hub_on_the_LIVE_path(db):
    """The one mistake that would silently undo all of this.

    `intro_html` and `peers_html` are pre-rendered into `company_page_extras`
    and only rebuilt when a filing moves. A link added there would not appear
    until somebody remembered to force a backfill -- which is exactly the trap
    that kept 6,184 pages naming a dead domain through a deploy that had
    already fixed the code.

    So the chip is asserted on a page rendered with NO stored extras at all.
    """
    from src.api import app

    client = TestClient(app)
    page = client.get("/company/FIN3")
    assert page.status_code == 200
    assert 'href="/sector/financials"' in page.text, (
        "the sector chip must link on the live render path, not via stored "
        "page-extras"
    )


def test_every_hub_is_in_the_sitemap_with_a_real_lastmod(db):
    from src.sitemap import _sector_pages

    pages = dict(_sector_pages())
    assert "financials" in pages
    assert "industrials" in pages
    assert "unclassified" in pages
    # A real filing date, not today -- the same rule the company pages follow.
    assert pages["financials"] == FD.isoformat()


def test_titles_stay_inside_the_result_list_budget():
    from src.report.sector_page import seo_title

    for sector in (
        "Information Technology", "Consumer Discretionary",
        "Communication Services", "Health Care", "Financials", "Materials",
        "Real Estate", "Consumer Staples", "Energy", "Utilities",
        "Industrials", "Unclassified",
    ):
        title = seo_title(sector)
        assert len(title) <= 60, f"{len(title)}: {title}"


def test_slugs_round_trip_and_never_collide():
    from src.report.sector_page import UNCLASSIFIED_SLUG, sector_slug

    seen = {}
    for sector in (
        "Information Technology", "Consumer Discretionary", "Health Care",
        "Financials", "Real Estate", "Communication Services",
    ):
        slug = sector_slug(sector)
        assert slug == slug.lower() and " " not in slug
        assert slug not in seen, f"collision: {sector} and {seen.get(slug)}"
        seen[slug] = sector

    assert sector_slug(None) == UNCLASSIFIED_SLUG
    assert sector_slug("   ") == UNCLASSIFIED_SLUG
