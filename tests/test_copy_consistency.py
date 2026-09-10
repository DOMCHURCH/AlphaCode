"""The site must not state two different totals for one product.

`/dataset` computed its row count live and said "1.8M rows". The home page,
`/api`, the blog post and `llms.txt` each carried a hand-typed "1.7M data
points". Same table, two answers, on a site whose entire pitch is that its
figures agree with each other and with the filings underneath them.

Four literals typed at four different times is a drift machine, and replacing
them with a fifth literal only resets the clock -- so they read
`dataset.facts_label()` now. What this file guards is the class of mistake
rather than the one instance: a stale figure written down anywhere fails here,
and the rendered pages are checked against each other rather than against a
number this test also has to know.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
Q = dt.date(2025, 12, 31)

# Figures that were true once and are now something else. A literal is not
# banned for being a number -- it is banned for being THIS number, written into
# copy that no longer matches what the database holds.
STALE = ("1.7M", "1.7 million")

SEARCHED = (
    ROOT / "src" / "report",
    ROOT / "static_seo",
)


def _copy_files() -> list[Path]:
    out: list[Path] = []
    for base in SEARCHED:
        for suffix in ("*.py", "*.txt", "*.js", "*.html", "*.css"):
            out.extend(p for p in base.rglob(suffix) if "__pycache__" not in str(p))
    return out


@pytest.mark.parametrize("stale", STALE)
def test_no_stale_dataset_figure_survives_in_copy(stale):
    """A comment explaining the bug is fine. A claim to a reader is not."""
    offenders = []
    for path in _copy_files():
        for n, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if stale not in line:
                continue
            stripped = line.lstrip()
            # Prose ABOUT the drift is allowed; prose asserting it is not.
            if stripped.startswith(("#", "*", '"""', "//")) or '"1.7' in stripped:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{n}: {stripped[:90]}")
    assert not offenders, "stale dataset figure in copy:\n" + "\n".join(offenders)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'copy.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import app
    from src.dataset import reset_count_cache

    reset_count_cache()
    with TestClient(app) as c:
        yield c

    reset_count_cache()
    get_settings.cache_clear()
    reset_engine_cache()


def seed(n_facts: int, *, start: int = 0) -> None:
    """`n_facts` rows across a few tickers, so the counts are non-zero.

    `start` offsets the metric names: the table has a UNIQUE constraint on
    (ticker, metric, period_end, source, filing_date), so seeding twice in one
    test has to add NEW rows rather than re-insert the same ones.
    """
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        for i in range(start, start + n_facts):
            ticker = f"T{i % 7:02d}"
            if start == 0 and i < 7:
                # Only on the first seed: `universe` is unique on
                # (as_of_date, ticker) and a second call would collide.
                s.add(UniverseSnapshot(
                    as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Inc"
                ))
            s.add(Fundamental(
                ticker=ticker, metric=f"m{i}", value=float(i + 1),
                period_end=Q, fiscal_period="FY",
                filing_date=dt.date(2026, 2, 1), source="sec", restated=False,
            ))


def _figures(html: str) -> set[str]:
    """Every "<n>M data points" / "<n>M rows" style claim on a page."""
    return set(re.findall(r"\b(\d+(?:\.\d+)?[Mk]|\d{1,3}(?:,\d{3})+)\b", html))


def test_every_page_states_the_same_dataset_size(client):
    seed(40)
    from src.dataset import facts_label, reset_count_cache

    reset_count_cache()
    label = facts_label()
    assert label, "the fixture must produce a countable table"

    for path in ("/", "/api", "/dataset", "/pricing", "/llms.txt"):
        html = client.get(path).text
        assert label in html, (
            f"{path} does not state the live figure {label!r}; "
            "it is probably carrying a literal again"
        )


def test_the_figure_moves_when_the_table_does(client):
    """The property that makes the literal impossible to reintroduce."""
    from src.dataset import facts_label, reset_count_cache

    seed(10)
    reset_count_cache()
    before = facts_label()

    seed(5_000, start=10)
    reset_count_cache()
    after = facts_label()

    assert before != after, (
        f"the label did not move when the table grew ({before!r} -> {after!r})"
    )


def test_an_unreadable_count_renders_as_nothing_not_as_zero(client, monkeypatch):
    """"0 facts" on the front page of a data product is worse than silence."""
    from src import dataset

    monkeypatch.setattr(dataset, "row_count", lambda: 0)
    assert dataset.facts_label() == ""

    def boom():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(dataset, "row_count", boom)
    assert dataset.facts_label() == ""

    html = client.get("/").text
    assert html.count("<h1") == 1, "the page must still render without a count"
    assert "0 data points" not in html


def test_no_company_count_literal_survives_in_rendered_copy(client):
    """The other half of the same drift, and the half that was missed.

    Last session single-sourced the FACT count and left "6,201 companies"
    written by hand in four more places: the /dataset description, the /pricing
    description, the `featureList` of the SoftwareApplication JSON-LD, and
    llms.txt. The fact count moving on its own while the company count stayed
    frozen is the same bug with a different number.
    """
    seed(40)
    from src.dataset import reset_count_cache
    from src.report.home_page import companies_label

    reset_count_cache()
    label = companies_label()
    assert label and label != "6,201", (
        "the fixture must produce a company count unlike the old literal"
    )

    for path in ("/dataset", "/pricing", "/llms.txt"):
        body = client.get(path).text
        assert "6,201" not in body, f"{path} still carries the literal"
        assert label in body, f"{path} does not state the live count {label!r}"


def test_the_structured_data_states_the_live_company_count(client):
    """Structured data is the worst place for a stale figure: it is machine-read
    and quoted back by engines without a human glancing at it first."""
    seed(40)
    from src.dataset import reset_count_cache
    from src.report.home_page import companies_label

    reset_count_cache()
    html = client.get("/").text

    assert "application/ld+json" in html
    assert f"Reconciled balance sheets for {companies_label()} US-listed" in html
    assert "6,201 US-listed" not in html


def test_llms_txt_leaves_no_unfilled_placeholder(client):
    """A crawler reading `{COMPANIES}` verbatim is worse than a stale number."""
    seed(40)
    from src.dataset import reset_count_cache

    reset_count_cache()
    body = client.get("/llms.txt").text

    assert body, "llms.txt must still be served"
    assert "{COMPANIES}" not in body
    assert "{FACTS}" not in body
    # `{TICKER}` is a URL PATTERN and must survive -- it is telling a crawler
    # the shape of the company URLs, not asking to be filled in.
    assert "/company/{TICKER}" in body


# ---------------------------------------------------------------------------
# The four pages the metadata pass skipped
# ---------------------------------------------------------------------------

def test_the_four_skipped_pages_have_canonical_and_meta(client):
    """Six page types got description + canonical + og:url + JSON-LD. Four did
    not: /login, /terms, /privacy, /dashboard.

    Two of them are pages a person genuinely searches for, and two are
    utilities that were being offered to Google as though they were content.
    Both halves of that were costing something.
    """
    for path in ("/terms", "/privacy", "/login", "/dashboard"):
        html = client.get(path).text
        assert 'rel="canonical"' in html, f"{path} has no canonical"
        assert 'property="og:url"' in html, f"{path} has no og:url"
        assert 'name="description"' in html, f"{path} has no description"
        assert html.count("<h1") == 1, f"{path} should have exactly one h1"


def test_the_legal_pages_are_indexable_and_described(client):
    """/terms and /privacy answer real searches ("is my data stored"), so they
    keep their place in the index and get a description that says which of the
    two a searcher wants."""
    for path, wanted in (("/terms", "refunds"), ("/privacy", "bcrypt")):
        html = client.get(path).text
        assert "noindex" not in html, f"{path} must stay indexable"
        assert "application/ld+json" in html, f"{path} has no structured data"
        desc = html.split('name="description" content="', 1)[1].split('"', 1)[0]
        assert wanted in desc, f"{path} description is boilerplate: {desc[:80]}"


def test_the_utility_pages_are_noindexed(client):
    """Signed out, the dashboard is a paste-your-key box and /login is a single
    field. A crawler has no session, so that is all either one ever shows it."""
    for path in ("/login", "/dashboard"):
        assert "noindex" in client.get(path).text, f"{path} is still indexable"


def test_the_sitemap_lists_nothing_it_tells_google_to_ignore(client):
    """A sitemap entry and a noindex are two instructions that contradict each
    other. Search Console reports it as an error, and the crawl budget spent
    resolving it comes out of the six thousand pages that do want indexing."""
    sitemap = client.get("/sitemap.xml").text

    for path in ("/login", "/dashboard"):
        assert f"<loc>https://testserver{path}</loc>" not in sitemap
        assert path not in sitemap, f"{path} is noindexed and still in the sitemap"

    for path in ("/terms", "/privacy"):
        assert path in sitemap, f"{path} is indexable and should be listed"


def test_the_methodology_page_explains_all_four_exception_categories(client):
    """A = L + E is an identity, so 99.9% is a claim that needs a reason. The
    page is that reason, and a category quietly dropped from it turns the
    honest number back into a marketing one."""
    html = client.get("/methodology").text

    assert html.count("<h1") == 1
    for phrase in (
        "noncontrolling interest",
        "Mezzanine equity",
        "Rounding",
        "genuinely does not balance",
        "LiabilitiesAndEquity",
    ):
        assert phrase in html, f"/methodology does not explain {phrase!r}"
    assert 'rel="canonical"' in html


def test_the_homepage_says_what_the_missing_tenth_of_a_percent_is(client):
    html = client.get("/").text

    assert "99.9%" in html, "the claim itself must stay"
    assert "never silently fudged" in html
    assert "/methodology" in html, "the explanation must be reachable"


def test_the_site_does_not_claim_a_hundred_percent(client):
    """The 0.1% is real. Rounding it away would be the one dishonest fix."""
    for path in ("/", "/methodology", "/pricing"):
        html = client.get(path).text
        assert "100% accurate" not in html
        assert "100% accuracy" not in html


# ---------------------------------------------------------------------------
# Sitemap completeness
# ---------------------------------------------------------------------------

def test_the_sitemap_is_a_flat_urlset_not_an_index(client):
    """A sitemap index whose children 404 is the classic way a site ends up
    "unknown to Google". This one is flat, so there are no children to break."""
    xml = client.get("/sitemap.xml").text

    assert "<urlset" in xml
    assert "<sitemapindex" not in xml, (
        "if this ever becomes an index, every child needs its own 200 check"
    )


def test_every_page_type_is_in_the_sitemap(client):
    """One assertion per page TYPE, because a generator that quietly stops
    emitting a whole category is invisible in a URL count."""
    seed(40)
    from src.report.blog import POSTS
    from src.report.compare import PAGES

    xml = client.get("/sitemap.xml").text

    for path in (
        "/", "/api", "/pricing", "/dataset", "/blog", "/methodology",
        "/terms", "/privacy", "/llms.txt", "/financial-data.txt",
    ):
        assert f"{path}</loc>" in xml, f"{path} missing"

    for post in POSTS:
        assert f"/blog/{post.slug}</loc>" in xml, f"{post.slug} missing"
    for page in PAGES:
        assert f"/{page.slug}</loc>" in xml, f"{page.slug} missing"


def test_every_sitemap_url_carries_a_lastmod(client):
    """Google uses `lastmod` to decide what to re-crawl. A URL without one is a
    URL it has no reason to come back to."""
    seed(40)
    xml = client.get("/sitemap.xml").text

    assert xml.count("<loc>") > 0
    assert xml.count("<lastmod>") == xml.count("<loc>"), (
        f"{xml.count('<loc>')} URLs but {xml.count('<lastmod>')} lastmod tags"
    )


def test_the_sitemap_does_not_advertise_swagger(client):
    """/docs is FastAPI's generated UI and is off in production. Asking Google
    to index a 404 is worse than not asking."""
    xml = client.get("/sitemap.xml").text
    assert "/docs</loc>" not in xml
    assert "/openapi.json" not in xml
