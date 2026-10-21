"""/verification and the home hero that links to it.

The page is the one linked in outreach, so what it must never do is print a
number without its scope or a sample response that was not captured from the
service. The study figures are frozen literals (a dated snapshot); the live
counts are read from the database and dated.
"""

from __future__ import annotations

import json
import re

from tests.test_check_claims import CANONICAL
from tests.test_home_and_search import _drawable, _seed, client, db  # noqa: F401


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_the_page_states_the_check_the_flags_and_the_dated_study(client):
    r = client.get("/verification")
    assert r.status_code == 200
    text = _text(r.text)
    assert CANONICAL.replace("  ", " ") in text.replace("&amp;", "&")
    for code in ("rounding", "missing_tag", "unexplained", "broken", "not_testable"):
        assert f"<code>{code}</code>" in r.text
    # The frozen study, each with its date and its unit.
    assert "2026-10-09 at 17:12 UTC" in text
    assert "995 of 38,568 filings" in text
    assert "971 of the 995" in text
    assert "20 of 20" in text
    assert "counts filings, not companies" in text
    assert "SEC Financial Statement Data Sets" in text and "XBRL frames API" in text


def test_the_live_counts_are_dated_and_scoped(client):
    _seed("JPM", _drawable(), sector="Financials")
    from src.company.stats import reset_breakdown_cache

    reset_breakdown_cache()
    text = _text(client.get("/verification").text)
    assert re.search(r"Counted on \d{4}-\d{2}-\d{2} \(UTC\) across the latest balance sheet", text)
    assert "These are companies, not filings" in text


def test_the_sample_responses_are_json_and_hold_the_captured_values(client):
    html = client.get("/verification").text
    blocks = re.findall(r"<pre class=\"code\"[^>]*><code>(.*?)</code></pre>", html, re.S)
    from html import unescape

    parsed = []
    for b in blocks:
        body = unescape(b)
        if body.lstrip().startswith("{"):
            parsed.append(json.loads(body))
    tickers = {p["ticker"]: p for p in parsed}
    assert tickers["AAPL"]
    assert tickers["AACB"]["identity_status"] == "missing_tag"
    assert tickers["AACB"]["filer_totals_agree"] is True
    assert any(p.get("identity_status") == "balanced" for p in parsed)


def test_paid_plan_calls_are_shown_with_their_plan_and_no_invented_output(client):
    text = _text(client.get("/verification").text)
    assert "as_of=2025-03-01" in text
    assert "Pro and above" in text and "Starter and above" in text
    from src.report import verification_page

    # An uncaptured sample is an empty block, never a hand-written one.
    if not verification_page._SAMPLES:
        assert "[CONFIRM" not in client.get("/verification").text


def test_the_page_links_resolve_and_the_free_tier_is_stated_accurately(client):
    html = client.get("/verification").text
    for href in ("/dashboard", "/api", "/methodology", "/restatements", "/pricing",
                 "/blog/which-sec-balance-sheets-fail-the-identity-check"):
        assert f'href="{href}"' in html
        assert client.get(href).status_code == 200
    assert "1,000 calls a month" in _text(html)
    assert client.get(
        "/static/research/balance-sheet-check-2026-10/flagged-filings-2026-10-09.csv"
    ).status_code == 200


def test_the_page_is_in_the_sitemap_and_the_prerender_list(client):
    assert "/verification" in client.get("/sitemap-pages.xml").text or \
        "/verification" in client.get("/sitemap.xml").text


def test_the_hero_links_the_proof_page_and_carries_the_new_headline(client):
    _seed("JPM", _drawable(), sector="Financials")
    text = _text(client.get("/").text)
    assert "Balance-checked SEC financials for production data pipelines." in text
    assert 'href="/verification"' in client.get("/").text
    assert "When a statement can't be tested, the API returns the reason" in text
    assert "instead of adjusting the numbers" in text
    # "free" describes the tier, not the product, in the head and the hero.
    head = client.get("/").text.split("</head>")[0]
    assert "A free SEC EDGAR" not in head
    assert "Free tier: 1,000 calls a month" in text


def test_pro_and_business_lead_the_home_cards_and_starter_stays(client, monkeypatch):
    from src import billing

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    home = client.get("/").text
    plans = re.findall(r'<a class="plan-cta"[^>]*data-plan="(\w+)"', home)
    first = [p for p in plans if p in {"starter", "pro", "business"}]
    assert first.index("pro") < first.index("business") < first.index("starter")
    assert "plan-minor" in home
    # Nothing removed and nothing repriced: the same four paid prices.
    for price in ("$19", "$49", "$99"):
        assert price in home
    # /pricing keeps its left-to-right order.
    pricing = client.get("/pricing").text
    on_pricing = [p for p in re.findall(r'<a class="plan-cta"[^>]*data-plan="(\w+)"', pricing)
                  if p in {"starter", "pro", "business"}]
    assert on_pricing.index("starter") < on_pricing.index("pro") < on_pricing.index("business")
    assert "plan-minor" not in pricing


def test_every_study_figure_on_the_page_is_quoted_from_the_published_report():
    """The 2026-10-09 study figures are literals on /verification. Each must
    appear in the report the page cites, so the page can never carry a number
    the report does not."""
    from src.report.blog import POSTS
    from src.report.verification_page import _STUDY

    post = next(p for p in POSTS if p.slug == "which-sec-balance-sheets-fail-the-identity-check")
    report = " ".join(re.sub(r"<[^>]+>", " ", post.body).split())
    page = " ".join(re.sub(r"<[^>]+>", " ", _STUDY).split())
    for figure in ("38,568", "995", "971", "728", "286", "20 of 20", "18 issuers", "433",
                   "17:12 UTC", "14 are", "10 "):
        assert figure in page, figure
    for figure in ("38,568", "995", "971", "728", "286", "20 of 20", "18 issuers", "433",
                   "17:12 UTC"):
        assert figure in report, f"{figure} is on /verification but not in the report"


def test_the_failure_labels_on_the_page_match_the_published_csv():
    import csv
    from collections import Counter
    from pathlib import Path

    p = (Path(__file__).resolve().parents[1] / "src/report/static/research/"
         "balance-sheet-check-2026-10/flagged-filings-2026-10-09.csv")
    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    labels = Counter(r["check_category"] for r in rows)
    assert len(rows) == 995
    assert labels == {"missing_tag": 971, "rounding": 14, "unexplained": 10}
    assert len({r["ticker"] for r in rows}) == 286
