"""The blog index is sectioned, and every post is on it without JavaScript.

What is pinned: each post carries exactly one label from SECTIONS, the server
renders every post under its section whether or not the script runs, the filter
and sort buttons ship `hidden` for the script to reveal, and none of them is a
`<nav>` (the site-wide `nav` CSS would style it as the header bar). The browser
half, filtering and sorting and the phone width, is e2e/tests/blog-index.spec.ts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.report.blog import POSTS, SECTIONS

SECTION_NAMES = [name for name, _blurb in SECTIONS]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'blog.db'}")
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


def test_every_post_has_exactly_one_known_section():
    bad = [(p.slug, p.section) for p in POSTS if p.section not in SECTION_NAMES]
    assert not bad, f"posts with a section that is not in SECTIONS: {bad}"


def test_every_section_has_a_post():
    used = {p.section for p in POSTS}
    assert used == set(SECTION_NAMES), f"unused sections: {set(SECTION_NAMES) - used}"


def test_the_index_lists_every_post_once_without_javascript(client):
    page = client.get("/blog").text
    # The footer also links the report, so count inside the section blocks.
    html = "".join(re.findall(r'<section class="blog-sec".*?</section>', page, re.S))
    for p in POSTS:
        assert html.count(f'href="/blog/{p.slug}"') == 1, p.slug
    # Each post sits inside its own section's block.
    for name, _blurb in SECTIONS:
        sec_id = name.lower().replace(" ", "-")
        m = re.search(
            rf'<section class="blog-sec" id="{sec_id}".*?</section>', html, re.S
        )
        assert m, f"no block for the {name} section"
        block = m.group(0)
        for p in POSTS:
            assert (f'href="/blog/{p.slug}"' in block) == (p.section == name), p.slug


def test_the_tools_are_hidden_until_the_script_runs_and_are_not_a_nav(client):
    html = client.get("/blog").text
    tools = re.search(r'<div class="blog-tools"[^>]*>', html)
    assert tools and " hidden" in tools.group(0)
    # the jump links are what a reader without the script gets instead
    jump = re.search(r'<p class="blog-jump" id="blog-jump">(.*?)</p>', html, re.S)
    assert jump
    for name, _blurb in SECTIONS:
        assert f'href="#{name.lower().replace(" ", "-")}"' in jump.group(1)
    blob = html.split('id="blog-tools"', 1)[1].split("</section>", 1)[0]
    assert "<nav" not in blob
    assert "/static/blog.js" in html
    assert Path("src/report/static/blog.js").exists()


def test_each_item_carries_what_the_sort_reads(client):
    html = client.get("/blog").text
    items = re.findall(r'<li class="post-item"([^>]*)>', html)
    assert len(items) == len(POSTS)
    for attrs in items:
        assert re.search(r'data-date="\d{4}-\d{2}-\d{2}"', attrs)
        assert re.search(r'data-min="\d+"', attrs)
        assert re.search(r'data-i="\d+"', attrs)


def test_the_index_makes_no_freshness_claim(client):
    html = client.get("/blog").text
    text = re.sub(r"<[^>]+>", " ", html)
    for word in ("counted live", "real-time", "daily"):
        assert word not in text, word


def test_the_blog_pages_carry_no_personal_byline(client):
    for path in ("/blog", f"/blog/{POSTS[0].slug}"):
        html = client.get(path).text
        assert "Dominique" not in html and "Written by" not in html, path


# ---------------------------------------------------------------------------
# The companion post
# ---------------------------------------------------------------------------

SLUG = "what-a-failed-identity-check-means"


def test_the_companion_post_renders_and_links_the_report_and_the_demo(client):
    r = client.get(f"/blog/{SLUG}")
    assert r.status_code == 200
    html = r.text
    assert "995 of 38,568" in html
    assert 'href="/blog/which-sec-balance-sheets-fail-the-identity-check"' in html
    assert 'href="/blog"' in html
    assert "/api/demo/" in html and "/#demo" in html
    # official documentation for each source it describes
    for url in (
        "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets",
        "https://massive.com/docs/rest/stocks/fundamentals/balance-sheets",
    ):
        assert url in html, url
    # the post names the date and the copy its counts come from
    assert "9 October 2026" in html and "17:12 UTC" in html


def test_the_companion_post_does_not_say_companies_have_wrong_accounts():
    body = next(p for p in POSTS if p.slug == SLUG).body.lower()
    for phrase in ("wrong accounts", "incorrect accounts", "misstated", "error by the company"):
        assert phrase not in body, phrase
    for word in ("live", "daily", "real-time", "realtime"):
        assert not re.search(rf"\b{word}\b", body), word
