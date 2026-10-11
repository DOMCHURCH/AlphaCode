"""Product landing pages (`report.landing`): one search, one page, real samples.

Parametrised over `landing.PAGES`, so a page added to that tuple is held to every
rule here without a new test.
"""

from __future__ import annotations

import datetime as dt
import html as _html
import json
import re

import pytest
from fastapi.testclient import TestClient

from src.report import landing

Q = dt.date(2026, 6, 30)
FILED = dt.date(2026, 7, 31)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'landing.db'}")
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

    from src.api import app

    with TestClient(app) as c:
        yield c


def _seed(ticker=landing.SAMPLE_TICKER):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    figures = {
        "total_assets": 1_000_000_000.0, "total_liabilities": 600_000_000.0,
        "total_equity": 400_000_000.0, "cash": 123_456_000.0,
    }
    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=Q, ticker=ticker, name="Sample Corp"))
        for metric, value in figures.items():
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=Q,
                fiscal_period="Q2", filing_date=FILED, source="sec",
                source_tag=f"us-gaap:{metric}",
            ))


def _text(h: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", " ", h))


SLUGS = [p.slug for p in landing.PAGES]


@pytest.mark.parametrize("slug", SLUGS)
def test_page_is_served_with_a_self_canonical_and_serp_sized_metadata(client, slug):
    _seed()
    r = client.get(f"/{slug}")
    assert r.status_code == 200
    h = r.text
    assert f'<link rel="canonical" href="https://balanceproof.dev/{slug}">' in h
    assert "noindex" not in h
    title = re.search(r"<title>(.*?)</title>", h, re.S).group(1)
    desc = re.search(r'<meta name="description" content="(.*?)">', h, re.S).group(1)
    assert len(_html.unescape(title)) <= 60, title
    assert len(_html.unescape(desc)) <= 155, desc


@pytest.mark.parametrize("slug", SLUGS)
def test_page_links_the_api_and_every_sibling_resolves(client, slug):
    _seed()
    page = landing.BY_SLUG[slug]
    h = client.get(f"/{slug}").text
    assert 'href="/api"' in h
    for href, _label, _why in page.related:
        assert f'href="{href}"' in h
        assert client.get(href).status_code == 200, href


@pytest.mark.parametrize("slug", SLUGS)
def test_page_makes_no_8k_claim(client, slug):
    _seed()
    assert "8-K" not in client.get(f"/{slug}").text


@pytest.mark.parametrize("slug", SLUGS)
def test_visible_faq_and_faqpage_markup_are_the_same_questions(client, slug):
    _seed()
    page = landing.BY_SLUG[slug]
    h = client.get(f"/{slug}").text
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', h, re.S)
    faq = [json.loads(b) for b in blocks if '"FAQPage"' in b]
    assert len(faq) == 1
    in_markup = [q["name"] for q in faq[0]["mainEntity"]]
    assert in_markup == [q for q, _a in page.faq()]
    visible = _html.unescape(h)
    for q, a in page.faq():
        assert f"<h3>{q}</h3>" in visible and a in visible


@pytest.mark.parametrize("slug", SLUGS)
def test_page_is_in_the_sitemap_and_the_compare_hub(client, slug):
    _seed()
    from _sitemap_util import sitemap_flat

    assert f"/{slug}</loc>" in sitemap_flat(client)
    assert f'href="/{slug}"' in client.get("/compare").text


def test_xbrl_page_shows_the_real_figures_from_the_database(client):
    _seed()
    h = client.get("/xbrl-balance-sheet-api").text
    text = _text(h)
    assert "1000000000.0" in h and "123456000.0" in h
    assert "2026-06-30" in h and "2026-07-31" in h
    # The arithmetic line comes from the same view the company page draws.
    assert "Assets $1,000,000,000" in text
    assert "= $0" in text
    assert "balanced" in text.lower()


def test_xbrl_page_leaves_the_sample_out_when_the_database_has_nothing(client):
    r = client.get("/xbrl-balance-sheet-api")
    assert r.status_code == 200
    assert "A real response" not in r.text
    assert "None" not in _text(r.text)


def test_sample_is_a_subset_of_the_real_response_not_an_edit_of_it(client):
    """Printed values must equal what the API serves for the same company."""
    from dataclasses import asdict

    from src.company.balancesheet import get_balance_sheet

    _seed()
    sample = landing.sample_response()
    real = asdict(get_balance_sheet(landing.SAMPLE_TICKER))
    for group in ("assets", "liabilities", "equity"):
        for key, cell in sample.get(group, {}).items():
            assert cell["value"] == real[group][key]["value"]


def test_coverage_count_is_counted_from_the_database_not_typed(client):
    for t in ("AAPL", "MSFT", "JPM"):
        _seed(t)
    text = _text(client.get("/xbrl-balance-sheet-api").text)
    assert "3 SEC filers are covered" in text


def test_sample_values_follow_the_database_between_requests(client):
    """Nothing on the page is a stored copy: change the row, change the page."""
    from sqlalchemy import update

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    _seed()
    first = client.get("/xbrl-balance-sheet-api").text
    assert "123456000.0" in first
    with session_scope() as s:
        s.execute(
            update(Fundamental)
            .where(Fundamental.ticker == landing.SAMPLE_TICKER)
            .where(Fundamental.metric == "cash")
            .values(value=777_000_000.0)
        )
    second = client.get("/xbrl-balance-sheet-api").text
    assert "777000000.0" in second and "123456000.0" not in second


def test_free_page_table_is_read_from_the_plan_matrix(client):
    """Change a cell in plans.MATRIX and the page moves with it."""
    from src import accounts, plans

    _seed()
    text = _text(client.get("/free-sec-fundamentals-api").text)
    for key in plans.MATRIX:
        assert plans.FEATURE_LABELS[key].capitalize() in text
    # The tier that unlocks a gated feature is named from the matrix too.
    need = plans.TIER_NAMES[plans.lowest_tier_with("changes")]
    assert f"{need} and above" in text
    assert f"{accounts.tier_limit('free'):,}" in text
    assert f"{accounts.tier_limit('pro'):,}" in text
    assert "1 year back" in text


def test_free_page_sample_is_the_real_history_the_free_plan_gets(client):
    from src.company.history import history

    _seed()
    # A second, older period inside the free year.
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    older, filed = dt.date(2026, 3, 31), dt.date(2026, 5, 2)
    with session_scope() as s:
        for metric, value in (("total_assets", 900e6), ("total_liabilities", 500e6),
                              ("total_equity", 400e6)):
            s.add(Fundamental(
                ticker=landing.SAMPLE_TICKER, metric=metric, value=value,
                period_end=older, fiscal_period="Q1", filing_date=filed,
                source="sec", source_tag=f"us-gaap:{metric}"))
    h = _html.unescape(client.get("/free-sec-fundamentals-api").text)
    real = history(landing.SAMPLE_TICKER, 1)
    assert real["periods"] == 2
    assert '"periods": 2' in h
    assert "900000000.0" in h and "1000000000.0" in h
    assert '"plan": "free"' in h and '"years_allowed": 1' in h


def test_free_page_leaves_the_sample_out_when_the_database_is_empty(client):
    r = client.get("/free-sec-fundamentals-api")
    assert r.status_code == 200
    assert "A real response from the free plan" not in r.text
    assert "What Free includes" in r.text


def test_mcp_page_lists_every_tool_the_server_serves(client):
    from src.mcp_server import TOOLS

    _seed()
    h = client.get("/mcp").text
    for tool in TOOLS:
        assert f"<code>{tool['name']}</code>" in h
        assert tool["name"] in landing._TOOL_BLURB, f"no human blurb for {tool['name']}"


def test_mcp_page_sample_is_the_tool_result_not_a_copy_of_its_wording(client):
    from src.mcp_server import _tool_check_balance_sheet

    _seed()

    class _M:
        def spend(self, _t):
            return None

    text, _payload = _tool_check_balance_sheet({"ticker": landing.SAMPLE_TICKER}, _M())
    assert _html.escape(text) in client.get("/mcp").text


def test_mcp_page_makes_no_directory_or_registry_claim(client):
    _seed()
    low = _text(client.get("/mcp").text).lower()
    for word in ("smithery", "glama", "mcp.so", "registry", "directory", "listed in"):
        assert word not in low, word


def test_mcp_page_url_is_the_real_endpoint_and_headers_match_the_code(client):
    _seed()
    h = client.get("/mcp").text
    assert "https://balanceproof.dev/mcp" in h
    assert "Authorization: Bearer" in h
    # The code reads a Bearer token on /mcp and X-API-Key on REST; the page says so.
    assert "X-API-Key" in h
