"""The front door: /, /search, and the suggestions that back both.

The rules being defended here are the product's, not the framework's:

  * a suggestion is never offered unless it can actually be drawn -- sending a
    reader from one empty page to another is the one thing an empty state
    must not do
  * search takes a ticker and lands on that company, with no results page in
    between whose only job is to be clicked through
  * a thumbnail is built by the same code as the full drawing, so the home page
    cannot advertise a shape the page then contradicts
  * nothing on the page ranks, scores, or predicts
"""

from __future__ import annotations

import datetime as dt

import pytest

PE = dt.date(2025, 12, 31)
FD = dt.date(2026, 2, 13)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'home.db'}")
    monkeypatch.setenv("API_KEY", "")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from src import api

    with TestClient(api.app) as c:
        yield c


def _seed(ticker: str, metrics: dict[str, float], sector: str | None = None) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap

    with session_scope() as s:
        if sector:
            s.add(SectorMap(ticker=ticker, cik="0", sector=sector,
                            sector_source="sic"))
        for metric, value in metrics.items():
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=float(value), period_end=PE,
                fiscal_period="FY", filing_date=FD, source="sec", restated=False,
            ))


def _drawable(**over: float) -> dict[str, float]:
    base = {
        "total_assets": 1_000e6,
        "total_liabilities": 600e6,
        "total_equity": 400e6,
        "cash": 100e6,
        "property_plant_equipment": 500e6,
        "long_term_debt": 300e6,
    }
    base.update(over)
    return base


# ------------------------------------------------------------------ suggestions
def test_a_ticker_with_no_data_is_never_suggested(db):
    """Only JPM is loaded, so only JPM is offered. Offering the other four
    would send a reader from one empty page to another."""
    from src.company.suggest import suggestions

    _seed("JPM", _drawable())

    assert [s.ticker for s in suggestions()] == ["JPM"]


def test_a_zero_total_is_not_drawable_and_not_suggested(db):
    """There is no scale drawing without a positive total to scale to."""
    from src.company.suggest import suggestions

    _seed("JPM", {"total_assets": 0.0})

    assert suggestions() == []


def test_suggestions_fall_back_to_whatever_is_actually_loaded(db):
    """None of the five are present, so offer tickers that are -- rather than
    five dead links or an empty list."""
    from src.company.suggest import suggestions

    _seed("ZZZ", _drawable())
    _seed("AAA", _drawable())

    picks = suggestions()

    assert [s.ticker for s in picks] == ["AAA", "ZZZ"], "alphabetical, not ranked"
    assert all(s.kind == "" for s in picks), "no invented descriptions"


def test_suggestions_survive_an_unreachable_database(db, monkeypatch):
    """An empty list beats a stack trace on the front page."""
    import src.company.suggest as suggest

    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(suggest, "tickers_with_data", boom)
    with pytest.raises(RuntimeError):
        suggest.tickers_with_data(("JPM",))
    # The real path catches it internally:
    monkeypatch.undo()
    monkeypatch.setattr("src.storage.db.session_scope", boom)
    assert suggest.suggestions() == []


# ------------------------------------------------------------------------- home
def test_home_is_the_product_not_a_redirect(client):
    """/ used to bounce to /admin. It is the front door now."""
    _seed("JPM", _drawable(), sector="Financials")

    r = client.get("/")

    assert r.status_code == 200
    body = r.text
    assert "Filed financial statements, drawn at true proportion" in body
    assert 'action="/search"' in body
    assert 'href="/company/JPM"' in body


def test_home_draws_a_real_thumbnail_from_the_same_view_as_the_page(client):
    """The thumbnail's proportions come from build_view1, so a card can never
    show a shape the company page then contradicts."""
    from src.company.view1 import build_view1

    _seed("JPM", _drawable(cash=250e6), sector="Financials")

    body = client.get("/").text
    d = build_view1("JPM").as_dict()
    cash = next(b for b in d["assets"] if b["label"] == "Cash")

    assert cash["pct"] == pytest.approx(25.0)
    # 25% of the 88px thumbnail column, drawn to the same proportion.
    assert "height:22.0px" in body
    assert "Cash 25%" in body


def test_home_says_so_when_nothing_is_loaded(client):
    """An empty database gets an explanation and a way forward, not a blank
    page or five links to nowhere."""
    body = client.get("/").text

    assert "No filed statements loaded yet" in body
    assert 'href="/admin"' in body
    assert 'href="/company/' not in body


def test_home_offers_a_ticker_it_cannot_draw_without_a_fake_thumbnail(
    client, monkeypatch
):
    """A drawing that will not build is absent, never a placeholder -- a
    placeholder is a picture of nothing presented as a company."""
    import src.company.view1 as v1

    _seed("JPM", _drawable(), sector="Financials")

    def no_view(ticker, *a, **kw):
        raise RuntimeError("view exploded")

    monkeypatch.setattr(v1, "build_view1", no_view)
    r = client.get("/")

    assert r.status_code == 200, "one bad ticker must not take the page down"
    assert 'href="/company/JPM"' in r.text
    assert "card bare" in r.text
    assert "thumb" not in r.text


# Saying "no scores" is the opposite of scoring, so a blanket ban on the word
# would forbid the page from stating the very thing that must be stated. Each
# of these is stripped before the ban is applied, which means the ban still
# catches any OTHER use.
_DISCLAIMERS = (
    "no predictions", "no scores", "no recommendations", "no ratings",
    "nothing predicted", "makes no prediction", "produces no scores",
    "it is not advice",
)


def _body_without_disclaimers(client) -> str:
    body = client.get("/").text.lower()
    for phrase in _DISCLAIMERS:
        body = body.replace(phrase, "")
    return body


def test_home_ranks_nothing(client):
    """No scores, no ratings, no advice, no ordering language -- except where
    the page is saying it does none of those things."""
    _seed("JPM", _drawable(), sector="Financials")

    body = _body_without_disclaimers(client)

    for word in ("rank", "score", "rating", "best", "top pick", " buy ",
                 " sell ", "undervalued", "recommend", "outperform",
                 "predict", "forecast"):
        assert word not in body, f"the front page must not say {word!r}"


def test_home_still_says_what_it_does_not_do(client):
    """The stripping above must not let the section itself go missing."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text.lower()

    for phrase in ("no predictions", "no scores", "no recommendations",
                   "nothing estimated"):
        assert phrase in body, f"the page must still say {phrase!r}"


# ------------------------------------------------------------ the numbers
def test_the_scale_is_counted_live_not_written_down(client):
    """A portfolio page with a hardcoded row count is wrong the first time the
    data reloads, and overstating its own scale is the one failure this project
    cannot afford."""
    from src.company.stats import counts

    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")

    c = counts()
    body = client.get("/").text

    assert c["companies"] == 2
    assert c["facts"] == 12, "6 metrics x 2 companies"
    assert c["quarters"] == 1
    assert f"{c['facts']:,}" in body
    assert "2</b><span class=\"statk\">companies" in body.replace("\n", "")


def test_drawable_is_narrower_than_present(client):
    """A company with facts but no positive total for assets has nothing to
    scale a drawing to. Counting it would overstate what the site can show."""
    from src.company.stats import counts

    _seed("JPM", _drawable())
    _seed("GHOST", {"cash": 5.0, "total_assets": 0.0})

    c = counts()

    assert c["companies"] == 2
    assert c["drawable"] == 1


def test_the_numbers_section_is_absent_on_an_empty_database(client):
    """Better to say nothing than to print zeroes as if they were a scale."""
    body = client.get("/").text

    assert "The numbers behind it" not in body
    assert "as-reported fact" not in body


def test_the_identity_line_is_omitted_until_it_is_known(client, monkeypatch):
    """It is a claim about every company, so it is stated only once actually
    computed -- never guessed at, never defaulted to 100%."""
    import src.company.stats as stats

    _seed("JPM", _drawable(), sector="Financials")
    monkeypatch.setattr(stats, "identity", lambda *a, **k: None)

    body = client.get("/").text

    assert "The numbers behind it" in body, "the counts still show"
    assert "satisfy assets = liabilities + equity" not in body


def test_the_identity_line_reports_the_real_pass_rate(client):
    """JPM balances exactly; the ghost has no complete sheet so is not counted."""
    from src.company.stats import identity

    _seed("JPM", _drawable(), sector="Financials")
    _seed("GHOST", {"total_assets": 100.0})

    ident = identity(max_age_s=0.0)
    body = client.get("/").text

    assert ident["checkable"] == 1, "only companies with a full sheet count"
    assert ident["pass_rate_pct"] == 100.0
    assert "100.0%" in body
    assert "satisfy assets = liabilities + equity" in body


def test_counts_survive_an_unreachable_database(monkeypatch):
    """The home page renders without its own statistics rather than 500ing."""
    import src.company.stats as stats

    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr("src.storage.db.session_scope", boom)
    monkeypatch.setattr(stats, "identity", lambda *a, **k: None)

    out = stats.site_stats()

    assert out["facts"] == 0 and out["identity"] is None


# ------------------------------------------------------- explaining the thing
def test_the_page_explains_the_drawing_before_it_shows_numbers(client):
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text

    assert "What you're looking at" in body
    assert "same money, counted twice" in body
    # The schematic is drawn, not described.
    assert 'class="example"' in body and "exband" in body


def test_the_example_figure_is_labelled_as_an_example(client):
    """An unlabelled illustration sitting among real figures is exactly the
    kind of thing this project exists to avoid."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text

    assert "not a real company" in body


def test_the_hard_part_is_stated_with_the_real_numbers(client):
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text

    assert "Why it's harder than it looks" in body
    assert "twenty-three separate times" in body
    assert "$641 billion instead of $4.4 trillion" in body
    assert "ExcludingAccruedInterest" in body
    assert "confirmed against the raw filing data" in body


def test_the_suggestions_say_why_each_one_is_there(client):
    """One word each. "negative equity" is the reason AAL is on the list;
    "an airline" would only describe it."""
    _seed("JPM", _drawable(), sector="Financials")
    _seed("AAL", _drawable(total_equity=-50e6, total_liabilities=1_050e6))

    body = client.get("/").text

    assert ">bank<" in body
    assert ">negative equity<" in body


# ----------------------------------------------------------------------- search
@pytest.mark.parametrize(
    "query,expected",
    [
        ("JPM", "/company/JPM"),
        ("jpm", "/company/JPM"),
        ("  msft  ", "/company/MSFT"),
        ("$jpm", "/company/JPM"),
        ("brk.b", "/company/BRK.B"),
    ],
)
def test_search_goes_straight_to_the_company(client, query, expected):
    """No results page in between. The answer to "JPM" is JPM's page."""
    r = client.get("/search", params={"q": query}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == expected


def test_search_for_an_unknown_ticker_still_reaches_the_page_that_can_say_so(client):
    """A symbol we hold nothing for is not a search error -- /company is the one
    place that can explain and offer alternatives."""
    _seed("JPM", _drawable(), sector="Financials")

    r = client.get("/search", params={"q": "NOSUCH"}, follow_redirects=True)

    assert r.status_code == 404
    assert "Nothing to draw for NOSUCH" in r.text
    assert 'href="/company/JPM"' in r.text, "offer one that exists"


def test_search_with_nothing_typed_asks_for_a_ticker(client):
    _seed("JPM", _drawable(), sector="Financials")

    r = client.get("/search", params={"q": "   "})

    assert r.status_code == 400
    assert "Type a ticker" in r.text
    assert 'href="/company/JPM"' in r.text


def test_search_rejects_something_that_is_not_a_ticker(client):
    r = client.get("/search", params={"q": "<script>x</script>"})

    assert r.status_code == 404
    assert "does not look like a ticker" in r.text
    assert "<script>x</script>" not in r.text, "the echo must be escaped"


def test_the_company_page_and_search_clean_a_ticker_the_same_way(client):
    """One helper, so /search?q=$jpm and /company/$jpm cannot disagree."""
    from src.api import _clean_ticker

    assert _clean_ticker(" $jpm ") == "JPM"
    assert _clean_ticker("brk.b") == "BRK.B"
    assert _clean_ticker("") == ""


def test_the_service_is_branded_to_scale(client):
    _seed("JPM", _drawable(), sector="Financials")

    assert "To&nbsp;Scale" in client.get("/").text
    assert "To&nbsp;Scale" in client.get("/company/JPM").text
    assert client.get("/api").json()["service"] == "To Scale"
