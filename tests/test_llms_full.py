"""`/llms-full.txt`: the site's prose, as text, in one response.

WHAT IS BEING DEFENDED. `llms.txt` is a map of URLs; this is what those URLs
say. A model that reads it and then answers a question about this product is
quoting the pages, so two things have to hold and neither is obvious from
looking at the output once.

First, that it is still the PAGES. It is assembled from what the routes
return, so a page rewritten tomorrow appears here rewritten, and a page added
to `blog.POSTS` or `compare.PAGES` appears here at all. The test that matters
is the one below asserting a sentence from each page, because that is what
fails when a section quietly renders empty.

Second, that it is TEXT. Not markup with the angle brackets removed -- a
paragraph wrapped across six source lines has to come back as one sentence, a
code sample has to keep its line breaks, and a comparison table has to stay a
table rather than becoming a wall of adjacent words. Each of those is one
assertion here, and each of them was a bug first.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from src.report.llms_full import page_text, to_text

# ---------------------------------------------------------------------------
# HTML in, readable text out
# ---------------------------------------------------------------------------


def test_the_chrome_is_dropped_and_the_prose_is_kept():
    """Thirteen copies of the same navigation is most of the file and none of
    the content."""
    text = to_text(
        "<nav><a href='/'>Home</a></nav>"
        "<p>The figure is checked.</p>"
        "<script>var x = 1;</script><style>p{color:red}</style>"
        "<footer><a href='/terms'>Terms</a></footer>"
    )
    assert text == "The figure is checked."


def test_a_paragraph_wrapped_in_the_source_comes_back_as_one_sentence():
    """The markup is indented, and a newline inside a paragraph is whitespace
    like any other. Collapsing only spaces left every continuation line
    broken and indented."""
    text = to_text("<p>We pull balance sheets\n      straight from EDGAR,\n"
                   "      verify each one.</p>")
    assert text == "We pull balance sheets straight from EDGAR, verify each one."


def test_a_non_breaking_space_is_a_space():
    assert to_text("<p>A&nbsp;=&nbsp;L&nbsp;+&nbsp;E</p>") == "A = L + E"


def test_two_elements_written_with_nothing_between_them_are_two_things():
    """The status strip is `<span>Source</span><span>SEC EDGAR</span>`, which
    ran together as "SourceSEC EDGAR"."""
    text = to_text('<div><span class="sv">SEC EDGAR</span>'
                   '<span class="sv">1.8M</span></div>')
    assert text == "SEC EDGAR 1.8M"


def test_a_label_takes_a_colon_rather_than_a_space():
    text = to_text('<div><span class="sk">Companies</span>'
                   '<span class="sv">6,184</span></div>')
    assert text == "Companies: 6,184"


def test_a_badge_is_beside_the_heading_and_not_the_next_word_of_it():
    text = to_text('<span class="plan-name">Pro annual'
                   '<span class="badge">Save $98</span></span>')
    assert text == "Pro annual Save $98"


def test_a_price_and_its_unit_are_one_word():
    """The rule above must not become "a space before every element": four
    lines from that badge sits `$490<small>/year</small>`."""
    assert to_text('<p class="plan-price">$490<small>/year</small></p>') == "$490/year"


def test_a_code_sample_keeps_its_line_breaks():
    """A shell command whose continuation was folded into the line above it is
    a command that no longer runs."""
    text = to_text('<pre class="code"><code>curl -H "X-API-Key: KEY" \\\n'
                   '  https://toscale.pro/api/company/JPM</code></pre>')
    assert text == ('curl -H "X-API-Key: KEY" \\\n'
                    '  https://toscale.pro/api/company/JPM')


def test_a_comparison_table_stays_a_table():
    """These pages are mostly table. Flattened into prose, the two columns
    become one sentence saying the opposite of what the row says."""
    text = to_text(
        "<table><thead><tr><th></th><th>To Scale</th><th>Intrinio</th></tr>"
        "</thead><tbody><tr><td>Scope</td><td>SEC balance sheets</td>"
        "<td>Broad</td></tr></tbody></table>"
    )
    assert text.splitlines() == [
        "| | To Scale | Intrinio |",
        "| Scope | SEC balance sheets | Broad |",
    ]


def test_a_list_reads_as_a_list():
    text = to_text("<ul><li>Every company</li><li>No key</li></ul>")
    assert text.splitlines() == ["- Every company", "- No key"]


def test_headings_keep_their_depth():
    text = to_text("<h2>Endpoints</h2><p>One.</p><h3>Authentication</h3>")
    assert "### Endpoints" in text
    assert "#### Authentication" in text


def test_a_caption_written_for_a_screen_reader_is_not_said_twice():
    text = to_text('<table><caption class="vh">Assets, line by line</caption>'
                   "<tbody><tr><td>Cash</td></tr></tbody></table>")
    assert "line by line" not in text
    assert "Cash" in text


def test_an_escaped_angle_bracket_is_content_and_comes_back_as_one():
    """`&lt;` is a character the page is showing, not a tag. Which is also why
    the assertions below look for the tags this converter strips rather than
    for anything shaped like a tag -- a post about XBRL is allowed to print
    `<xbrli:context>`, and a test that fires on that gets deleted."""
    text = to_text("<main><p>A &lt; B and C &gt; D</p>"
                   "<p>An element is written &lt;main&gt;.</p></main>")
    assert "A < B and C > D" in text
    assert "An element is written <main>." in text


# ---------------------------------------------------------------------------
# One page
# ---------------------------------------------------------------------------


def test_only_the_main_element_is_read_and_the_h1_becomes_the_heading():
    heading, body = page_text(
        "<html><head><title>x</title></head><body>"
        "<nav>Home Pricing</nav>"
        "<main><h1>SEC Filings API Pricing</h1><p>The drawings are free.</p>"
        "</main><footer>Terms</footer></body></html>"
    )
    assert heading == "SEC Filings API Pricing"
    assert body == "The drawings are free."
    # The heading becomes the section header, so it is not said twice.
    assert "Pricing" not in body


def test_a_page_with_no_main_falls_back_to_the_whole_document():
    """An empty section is worse than a noisy one: a model reading it cannot
    tell "this page rendered wrong" from "this product has nothing to say"."""
    heading, body = page_text("<div><h1>Notes</h1><p>Something.</p></div>")
    assert heading == "Notes"
    assert body == "Something."


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'llms.db'}")
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


@pytest.fixture()
def full(client) -> str:
    r = client.get("/llms-full.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    return r.text


MARKUP = re.compile(
    r"</?(?:html|head|body|main|nav|footer|section|div|span|p|a|ul|li|table|"
    r"tr|td|th|h[1-6]|script|style|pre|code|form|button|img)\b[^>]*>", re.I)


def test_it_is_served_as_plain_text_and_carries_no_markup(full):
    found = MARKUP.search(full)
    assert not found, f"markup survived: {found.group(0)!r}"
    assert "&nbsp;" not in full and "&amp;" not in full and "&#" not in full


def test_every_page_that_belongs_in_it_is_in_it(full):
    from src.report.blog import POSTS
    from src.report.compare import PAGES as COMPARISONS

    for path in ("/", "/methodology", "/pricing", "/api"):
        assert f"Source: http://testserver{path}\n" in full, f"{path} missing"
    for post in POSTS:
        assert f"/blog/{post.slug}\n" in full, f"{post.slug} missing"
    for page in COMPARISONS:
        assert f"/{page.slug}\n" in full, f"{page.slug} missing"


def test_each_section_carries_the_words_that_page_actually_says(full):
    """The assertion that fails when a section renders empty, which is the
    failure this file exists for: an empty section still looks like a file."""
    for sentence in (
        # /
        "traced to its source",
        # /methodology
        "Assets = Liabilities + Equity",
        # /pricing
        "$79.99",
        # /api
        "X-API-Key",
        # a comparison page, from its table rather than its prose
        "| Scope |",
    ):
        assert sentence in full, f"nothing in the file says {sentence!r}"


def test_a_page_added_to_the_collections_appears_without_being_listed_twice(full):
    """Blog posts and comparison pages are read from the same tuples the
    routes read, so the only way to add one is to add it once."""
    from src.report.compare import PAGES as COMPARISONS

    for page in COMPARISONS:
        assert page.h1 in full, f"{page.slug} rendered without its heading"


def test_it_points_back_at_the_map_it_is_the_other_half_of(full):
    assert "/llms.txt" in full
    assert "/sitemap.xml" in full


def test_no_company_gets_a_section_of_its_own(client):
    """Thousands of pages of figures would bury the prose, and the way to read
    those is the API. Seeded BEFORE the file is fetched -- the first version of
    this test took `full` as a fixture, so the company was created after the
    document had already been built and there was never anything to leak.

    A company NAME in the home page's prose is fine. A company SECTION is not.
    """
    _seed_balanced()
    text = client.get("/llms-full.txt").text
    assert "Source: http://testserver/company/" not in text


def test_a_drawing_stays_on_the_page_and_the_prose_around_it_comes_here():
    """The home page leads with one real balance sheet and five thumbnails.
    Drawn, those ARE the argument. As text they are one company's line items
    and five more companies' totals, in a document about what the product is
    -- and the way to read a company's figures is the API.

    Asserted against the markup rather than through the route, because what
    the home page chooses to draw depends on what is in the database, and a
    test that passes because there was nothing to draw is not a test.
    """
    text = to_text(
        '<section class="sec hero-bs">'
        '<div class="sec-head"><h2>Live from the filings</h2></div>'
        '<div class="bs">'
        '<div class="bs-cap"><span>Alpha owns</span><b>$1.00T</b></div>'
        '<table class="legend"><tbody><tr><td>Cash</td><td>$20B</td></tr>'
        "</tbody></table></div>"
        '<p class="plan-note">Both columns are the same money, counted twice.'
        "</p></section>"
    )
    assert "### Live from the filings" in text
    assert "Both columns are the same money, counted twice." in text
    assert "Cash" not in text and "$1.00T" not in text


def test_the_gallery_of_thumbnails_does_not_come_here_either():
    text = to_text(
        '<section class="sec">'
        '<div class="sec-head"><h2>Same scale rules, other companies</h2></div>'
        '<div class="cards"><a class="card" href="/company/JPM">'
        '<span class="ctick">JPM</span>'
        '<span class="csize">$4.36T of assets</span></a></div></section>'
    )
    assert "### Same scale rules, other companies" in text
    assert "JPM" not in text and "of assets" not in text


def _seed_balanced() -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    q = dt.date(2025, 12, 31)
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker="AAA", name="Alpha Inc",
            sector="Financials",
        ))
        s.add(SectorMap(ticker="AAA", sector="Financials", sector_source="sic"))
        for metric, value in (
            ("total_assets", 1_000e9),
            ("total_liabilities", 600e9),
            ("total_equity", 400e9),
        ):
            s.add(Fundamental(
                ticker="AAA", metric=metric, value=value, period_end=q,
                fiscal_period="FY", filing_date=dt.date(2026, 2, 13), source="sec",
            ))
