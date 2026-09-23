"""What the 2026-09-23 SEO audit changed, pinned.

Each of these was a finding on the live site: a trailing slash answered 307 to
http://, five render-blocking stylesheets, shells like BRLL ($135K) inviting
indexing, company pages with no ask on them, and a post searched for as
"accounting identity" whose title tag never said so.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from html import escape

import pytest
from fastapi.testclient import TestClient

Q = dt.date(2026, 6, 30)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'seo.db'}")
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


def seed(ticker: str, assets: float, filed: dt.date = dt.date(2026, 8, 1)) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker,
                               name=f"{ticker} Inc"))
        for metric, value in (("total_assets", assets),
                              ("total_liabilities", assets * .6),
                              ("total_equity", assets * .4)):
            s.add(Fundamental(ticker=ticker, metric=metric, value=value,
                              period_end=Q, fiscal_period="Q2", filing_date=filed,
                              source="sec", restated=False))


# --- trailing slash ---------------------------------------------------------

def test_a_trailing_slash_is_a_permanent_relative_redirect(client):
    r = client.get("/company/AAPL/?x=1", follow_redirects=False)
    assert r.status_code == 301
    # Relative, so the reader's https survives behind the TLS proxy.
    assert r.headers["location"] == "/company/AAPL?x=1"


def test_api_paths_are_not_redirected(client):
    r = client.get("/api/company/AAPL/", follow_redirects=False)
    assert r.status_code != 301


# --- thin pages -------------------------------------------------------------

def test_the_thin_rule():
    from src.sitemap import is_thin

    today = dt.date(2026, 9, 23)
    assert is_thin(135_000, dt.date(2026, 8, 1), today)          # BRLL-sized
    assert not is_thin(5e9, dt.date(2026, 8, 1), today)
    assert is_thin(5e9, dt.date(2024, 12, 1), today)              # stopped filing
    assert is_thin(None, None, today)


def test_a_shell_is_noindexed_and_a_real_company_is_not(client):
    seed("TINY", 135_000.0)
    seed("BIGCO", 5e9)
    tiny = client.get("/company/TINY").text
    big = client.get("/company/BIGCO").text
    assert '<meta name="robots" content="noindex,follow">' in tiny
    assert 'name="robots"' not in big


def test_a_shell_is_left_out_of_the_sitemap(client):
    seed("TINY", 135_000.0)
    seed("BIGCO", 5e9)
    from src import sitemap

    sitemap.refresh_drawable()
    xml = client.get("/sitemap.xml").text
    assert "/company/BIGCO<" in xml
    assert "/company/TINY<" not in xml


# --- one stylesheet ---------------------------------------------------------

def test_pages_link_one_bundled_stylesheet(client):
    seed("BIGCO", 5e9)
    for path in ("/", "/company/BIGCO", "/blog"):
        html = client.get(path).text
        assert "/static/site.css?v=" in html, path
        assert "/static/glass.css" not in html, path


def test_the_bundle_keeps_every_layer_in_order_and_caches(client):
    from src.report.cssbundle import LAYERS

    r = client.get("/static/site.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "immutable" in r.headers["cache-control"]
    marks = [r.text.index(f"/* {name} */") for name in LAYERS]
    assert marks == sorted(marks)
    # The rules added in this pass made it through minification.
    assert ".peer-list a{" in r.text
    assert '.exband[style*="--l1"]' in r.text


# --- the ask on a company page ---------------------------------------------

def test_a_company_page_asks_for_the_key_in_its_header(client):
    seed("BIGCO", 5e9)
    html = client.get("/company/BIGCO").text
    header = html.split('<header class="chead">', 1)[1].split("</header>", 1)[0]
    assert 'href="/dashboard">Get BIGCO as JSON, free</a>' in header


# --- the accounting identity post ------------------------------------------

def test_the_identity_post_says_what_is_searched_for(client):
    html = client.get("/blog/understanding-the-accounting-identity").text
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "Accounting Identity" in title and len(title) <= 60
    assert "balance sheet identity" in html


def test_the_faq_markup_matches_the_visible_questions(client):
    from src.report.blog import BY_SLUG

    post = BY_SLUG["understanding-the-accounting-identity"]
    html = client.get(f"/blog/{post.slug}").text
    blocks = [json.loads(b) for b in re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]
    faq = [b for b in blocks if b.get("@type") == "FAQPage"]
    assert len(faq) == 1
    marked = [q["name"] for q in faq[0]["mainEntity"]]
    assert marked == [q for q, _ in post.faq]
    for q in marked:
        assert f"<summary>{escape(q)}</summary>" in html


def test_a_post_without_questions_carries_no_faq(client):
    html = client.get("/blog/sec-submissions-api-reconciliation").text
    assert "FAQPage" not in html
    assert 'id="faq"' not in html


# --- refunds ----------------------------------------------------------------

def test_the_refund_window_is_stated_where_a_buyer_reads(client):
    terms = re.sub(r"\s+", " ", client.get("/terms").text)
    assert "14-day refund window" in terms
    assert "non-refundable" in terms  # the dataset file, still
    pricing = re.sub(r"\s+", " ", client.get("/pricing").text)
    assert "14-day refund window" in pricing
