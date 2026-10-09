"""/companies and /compare: the directory pages that give the deep pages a parent."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

Q = dt.date(2026, 6, 30)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'hubs.db'}")
    monkeypatch.setenv("ADMIN_SECRET", "test-admin-secret-do-not-use")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src import sitemap
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    sitemap.reset_cache()
    sitemap.reset_drawable()

    import src.report.hubs as hubs

    hubs._cache = None

    from src.api import app

    with TestClient(app) as c:
        yield c


def _seed(ticker: str, name: str, assets: float = 5e9) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name=name))
        s.add(Fundamental(
            ticker=ticker, metric="total_assets", value=assets, period_end=Q,
            fiscal_period="Q2", filing_date=dt.date(2026, 8, 1), source="sec",
        ))


def test_companies_hub_lists_a_company_under_its_letter(client):
    _seed("ACME", "Acme Holdings Inc")
    _seed("ZETA", "Zeta Corp")

    r = client.get("/companies")

    assert r.status_code == 200
    assert 'href="/company/ACME"' in r.text and 'href="/company/ZETA"' in r.text
    assert 'id="letter-A"' in r.text and 'id="letter-Z"' in r.text
    assert 'href="#letter-A"' in r.text, "a way to jump to a letter"
    assert 'rel="canonical" href="https://' in r.text or 'rel="canonical"' in r.text
    assert "<title>Companies A to Z" in r.text


def test_companies_hub_leaves_out_what_the_sitemap_leaves_out(client):
    """The directory and the sitemap are one opinion about what is worth indexing."""
    from src import sitemap

    _seed("BIGCO", "Bigco Ltd", 5e9)
    _seed("TINY", "Tiny Shell Corp", 135_000.0)
    sitemap.refresh_drawable()

    html = client.get("/companies").text

    assert 'href="/company/BIGCO"' in html
    assert 'href="/company/TINY"' not in html


def test_compare_hub_lists_every_comparison_page(client):
    from src.report.compare import PAGES

    r = client.get("/compare")

    assert r.status_code == 200
    for page in PAGES:
        assert f'href="/{page.slug}"' in r.text, page.slug


def test_the_footer_links_the_hubs_and_the_data_study_from_every_page(client):
    _seed("ACME", "Acme Holdings Inc")

    for path in ("/", "/company/ACME", "/blog", "/pricing"):
        html = client.get(path).text
        foot = html.split("<footer>", 1)[1]
        for href in ("/companies", "/compare", "/blog/sec-xbrl-data-wrong-one-in-five"):
            assert f'href="{href}"' in foot, f"{path} footer is missing {href}"


def test_both_hubs_are_in_the_sitemap(client):
    from _sitemap_util import sitemap_flat

    flat = sitemap_flat(client)

    assert "/companies</loc>" in flat
    assert "/compare</loc>" in flat


def test_comparison_pages_carry_a_visible_faq_and_matching_faqpage_markup(client):
    """FAQPage markup must mirror text that is on the page. Both come from the
    same pairs, so every marked-up question is also visible, and the breadcrumb
    parent is a page that exists."""
    import json
    import re

    from src.report.compare import PAGES

    checked = 0
    for page in PAGES:
        r = client.get(f"/{page.slug}")
        assert r.status_code == 200, page.slug
        blocks = [
            json.loads(b) for b in re.findall(
                r'<script type="application/ld\+json">([\s\S]*?)</script>', r.text
            )
        ]
        flat = [x for b in blocks for x in (b if isinstance(b, list) else [b])]
        faq = [x for x in flat if x.get("@type") == "FAQPage"]
        if page.kind == "best":
            assert not faq, f"{page.slug}: a guide has no head-to-head FAQ"
            continue
        assert len(faq) == 1, page.slug
        for q in faq[0]["mainEntity"]:
            name = q["name"]
            assert name.replace("&", "&amp;") in r.text or name in r.text, (
                f"{page.slug}: marked-up question is not on the page: {name}"
            )
        crumbs = [x for x in flat if x.get("@type") == "BreadcrumbList"][0]
        assert any(i["item"].endswith("/compare") for i in crumbs["itemListElement"])
        checked += 1
    assert checked >= 10


# ---------------------------------------------------------------------------
# What the sitemap lists is what Google is allowed to index
# ---------------------------------------------------------------------------

_HDR = {"host": "balanceproof.dev", "x-forwarded-proto": "https"}


def _meta(html: str, name: str) -> str:
    import re
    from html import unescape

    m = re.search(rf'<meta name="{name}" content="([^"]*)"', html)
    return unescape(m.group(1)) if m else ""


def _title(html: str) -> str:
    import re
    from html import unescape

    m = re.search(r"<title>([^<]*)</title>", html)
    return unescape(m.group(1)) if m else ""


def test_every_url_in_the_sitemap_is_200_indexable_and_its_own_canonical(client):
    """No noindex page listed, no redirect, no canonical pointing elsewhere."""
    import re

    from _sitemap_util import sitemap_flat

    from src import sitemap

    _seed("BIGA", "Biga Corp", 5e9)
    _seed("BIGB", "Bigb Corp", 2e9)
    _seed("TINY", "Tiny Shell Corp", 135_000.0)
    sitemap.reset_cache()
    sitemap.refresh_drawable()

    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap_flat(client, _HDR))
    assert len(locs) > 30
    assert "https://balanceproof.dev/company/TINY" not in locs, "a thin page is listed"
    for loc in locs:
        path = loc.replace("https://balanceproof.dev", "") or "/"
        r = client.get(path, headers=_HDR)
        assert r.status_code == 200, f"{loc} answers {r.status_code}"
        assert "noindex" not in _meta(r.text, "robots"), f"{loc} is noindex and listed"
        canon = re.search(r'<link rel="canonical" href="([^"]+)"', r.text)
        assert canon, f"{loc} has no canonical"
        assert canon.group(1).rstrip("/") == loc.rstrip("/"), (
            f"{loc} canonicalises to {canon.group(1)}"
        )


def test_titles_and_descriptions_are_serp_sized_on_the_pages_this_work_touches(client):
    from src.report.compare import PAGES

    _seed("BIGA", "Biga Corp", 5e9)
    paths = ["/", "/pricing", "/dataset", "/companies", "/compare"] + [
        f"/{p.slug}" for p in PAGES
    ]
    over = []
    for path in paths:
        html = client.get(path, headers=_HDR).text
        t, d = _title(html), _meta(html, "description")
        if len(t) > 60:
            over.append(f"{path} title {len(t)}: {t}")
        if len(d) > 155:
            over.append(f"{path} description {len(d)}: {d[:70]}...")
    assert not over, "\n".join(over)


def test_no_page_says_balanceproof_covers_8_k(client):
    """The comparison tables may name 8-K as the OTHER product's coverage. What
    must never appear is a BalanceProof claim to cover it."""
    from src.report.compare import PAGES

    for page in PAGES:
        for row in page.rows:
            assert "8-K" not in row.ours, f"{page.slug}: '{row.label}' claims 8-K for us"
    for path in ("/", "/pricing", "/dataset", "/api", "/about", "/methodology"):
        html = client.get(path, headers=_HDR).text
        assert "8-K" not in html, f"{path} mentions 8-K"
