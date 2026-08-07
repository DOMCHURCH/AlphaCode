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
    from src.company.lookup import reset_cache
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'home.db'}")
    monkeypatch.setenv("API_KEY", "")
    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
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
    # 25% of the 88px thumbnail column, drawn to the same proportion. Which
    # block the card LABELS is a separate decision (see the card-label tests);
    # what is asserted here is that the picture is the same picture.
    assert "height:22.0px" in body


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


# ------------------------------------------------------------- card labels
def _blk(key, label, pct, kind="asset", value=None):
    return {"key": key, "label": label, "pct": pct, "kind": kind,
            "value": pct if value is None else value, "is_remainder": False}


def _view(assets, claims):
    import types

    v = types.SimpleNamespace()
    v.as_dict = lambda: {"assets": assets, "claims": claims}
    return v


# Roughly the real universe: banks are ~3% of filers, most companies report
# property and cash, about half report goodwill.
_COV = {"cash": 0.88, "inventory": 0.34, "property_plant_equipment": 0.66,
        "goodwill": 0.49, "loans": 0.031, "deposits": 0.029,
        "investment_securities": 0.05, "accounts_payable": 0.71,
        "long_term_debt": 0.63, "equity": 1.0}


def _five_production_shaped():
    """The exact case that produced four cards reading "Property & equipment":
    MSFT 39%, WMT 48%, FCX 70%, AAL 49%, all off the live site."""
    return [
        _view([_blk("cash", "Cash", 11),
               _blk("investment_securities", "Investment securities", 11),
               _blk("loans", "Loans", 33)],
              [_blk("deposits", "Customer deposits", 58, "liability"),
               _blk("equity", "Shareholders' equity", 8, "equity")]),
        _view([_blk("goodwill", "Goodwill", 18),
               _blk("property_plant_equipment", "Property & equipment", 39)],
              [_blk("equity", "Shareholders' equity", 59, "equity")]),
        _view([_blk("inventory", "Inventory", 21),
               _blk("property_plant_equipment", "Property & equipment", 48)],
              [_blk("accounts_payable", "Accounts payable", 22, "liability"),
               _blk("equity", "Shareholders' equity", 33, "equity")]),
        _view([_blk("property_plant_equipment", "Property & equipment", 70),
               _blk("inventory", "Inventory", 12)],
              [_blk("long_term_debt", "Long-term debt", 17, "liability"),
               _blk("equity", "Shareholders' equity", 44, "equity")]),
        _view([_blk("property_plant_equipment", "Property & equipment", 49)],
              [_blk("long_term_debt", "Long-term debt", 52, "liability"),
               _blk("equity", "Shareholders' equity", 7, "equity",
                    value=-4_500_000_000)]),
    ]


def test_no_two_cards_carry_the_same_label():
    """Four cards saying "Property & equipment" contradicted the sentence
    directly above them promising the five look nothing alike."""
    from src.report.home_page import headline_facts

    facts = headline_facts(_five_production_shaped(), _COV)

    assert len(set(facts)) == len(facts), f"repeated label in {facts}"


def test_each_card_gets_its_own_defining_line():
    from src.report.home_page import headline_facts

    jpm, msft, wmt, fcx, aal = headline_facts(_five_production_shaped(), _COV)

    assert jpm == "Customer deposits 58%", "a bank is deposits, not cash"
    assert msft == "Goodwill 18%"
    assert wmt == "Inventory 21%"
    assert fcx == "Property &amp; equipment 70%", "the most property wins it"
    assert aal == "Owes $4.5B more than it owns"


def test_a_shared_line_goes_to_the_company_that_most_exemplifies_it():
    """FCX is 70% property and MSFT 39%, so property is FCX's line. MSFT moves
    to its own next-most-distinctive one rather than repeating it."""
    from src.report.home_page import headline_facts

    facts = headline_facts(_five_production_shaped(), _COV)

    assert "Property &amp; equipment" in facts[3]
    assert "Property" not in facts[1]


def test_a_rare_line_beats_a_bigger_common_one():
    """Almost nobody files loans; almost everybody files property. Filing
    loans at all says more than being averagely property-heavy."""
    from src.report.home_page import _distinctiveness

    loans = _blk("loans", "Loans", 33)
    ppe = _blk("property_plant_equipment", "Property & equipment", 45)

    assert _distinctiveness(loans, _COV) > _distinctiveness(ppe, _COV)


def test_a_very_large_common_line_still_wins_its_own_card():
    """Being 70% property is itself the distinguishing fact, even though
    property is a line almost everyone files."""
    from src.report.home_page import _distinctiveness

    ppe = _blk("property_plant_equipment", "Property & equipment", 70)
    inventory = _blk("inventory", "Inventory", 12)

    assert _distinctiveness(ppe, _COV) > _distinctiveness(inventory, _COV)


def test_negative_equity_wins_outright():
    """Owing more than you own is categorical, not a magnitude -- the single
    most unusual thing a balance sheet can say."""
    from src.report.home_page import headline_facts

    v = _view([_blk("property_plant_equipment", "Property & equipment", 95)],
              [_blk("equity", "Equity", 7, "equity", value=-4_500_000_000)])

    assert headline_facts([v], _COV) == ["Owes $4.5B more than it owns"]


def test_coverage_is_unknown_rather_than_noisy_on_a_small_universe(client):
    """In a five-company database one filer with receivables makes receivables
    look as unusual as bank deposits. That is a fact about the sample, not
    about how companies file, so it is reported as unknown."""
    from src.company.stats import component_coverage

    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(receivables=80e6))

    assert component_coverage(max_age_s=0.0) == {}


def test_labels_still_work_with_no_coverage_data(client):
    """A cold or unreachable coverage cache degrades to picking the largest
    block -- a worse label, never a wrong one."""
    from src.report.home_page import headline_facts

    facts = headline_facts(_five_production_shaped(), {})

    assert all(facts), "every card still gets a line"
    assert len(set(facts)) == len(facts)


def test_the_card_label_is_a_real_block_off_the_drawing(client):
    """Whatever is chosen, the label and share come off the company's own
    drawing -- nothing composed, nothing rounded into being."""
    from src.company.view1 import build_view1

    _seed("WMT", _drawable(inventory=210e6), sector="Consumer Staples")

    body = client.get("/").text
    d = build_view1("WMT").as_dict()
    shown = [b for b in d["assets"] + d["claims"]
             if not b["is_remainder"] and f"{b['pct']:.0f}%" in body]

    assert shown, "the card's figure must be one the drawing actually has"


# Saying "no scores" is the opposite of scoring, so a blanket ban on the word
# would forbid the page from stating the very thing that must be stated. Each
# of these is stripped before the ban is applied, which means the ban still
# catches any OTHER use.
_DISCLAIMERS = (
    "no predictions", "no scores", "no recommendations", "no ratings",
    "nothing predicted", "makes no prediction", "produces no scores",
    "it is not advice",
)


def _body_without_disclaimers(client, path: str = "/") -> str:
    body = client.get(path).text.lower()
    for phrase in _DISCLAIMERS:
        body = body.replace(phrase, "")
    return body


@pytest.mark.parametrize("path", ["/", "/company/JPM"])
def test_no_page_ranks_anything(client, path):
    """No scores, no ratings, no advice, no ordering language -- except where
    a page is saying it does none of those things."""
    _seed("JPM", _drawable(), sector="Financials")

    body = _body_without_disclaimers(client, path)

    for word in ("rank", "score", "rating", "best", "top pick", " buy ",
                 " sell ", "undervalued", "recommend", "outperform",
                 "predict", "forecast"):
        assert word not in body, f"{path} must not say {word!r}"


def test_the_page_still_says_what_it_does_not_do(client):
    """The stripping above must not let the section itself go missing."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text.lower()

    for phrase in ("no predictions", "no scores", "no recommendations",
                   "nothing estimated"):
        assert phrase in body, f"/about must still say {phrase!r}"


# ------------------------------------------------------- search-first landing
def test_the_landing_page_puts_search_before_showing_off(client):
    """One page, in the order someone actually uses it. The explanation stays
    on it, but every part of it comes after the search and the five drawings.
    """
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text
    fold = body.index('id="how"')

    assert body.index('action="/search"') < fold
    assert body.index('href="/company/JPM"') < fold
    assert body.index('class="summary"') < fold
    for later in ("Why it's harder than it looks", "What you're looking at",
                  "What this doesn't do", "The numbers behind it",
                  'class="example"'):
        assert body.index(later) > fold, f"{later!r} must come after the fold"


def test_the_nav_does_not_advertise_the_same_page_as_a_destination(client):
    """A nav item pointing at an anchor on the page you are already on reads
    as somewhere else to go. The jump under the stat line reads as what it is
    -- a jump to a section -- and that one stays."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text
    nav = body.split("<nav>", 1)[1].split("</nav>", 1)[0]

    assert "#how" not in nav
    assert 'href="#how"' in body.split("</nav>", 1)[1], "the stat-line jump stays"


def test_about_redirects_onto_the_home_page(client):
    """It was a real URL for a while; a link that used to work keeps working."""
    r = client.get("/about", follow_redirects=False)

    assert r.status_code == 307
    assert r.headers["location"] == "/#how"


def test_admin_is_reachable_without_typing_a_url(client):
    """It is not part of the product, so it is a footnote rather than a nav
    item -- but it has to be reachable from the UI."""
    _seed("JPM", _drawable(), sector="Financials")

    home = client.get("/").text
    company = client.get("/company/JPM").text

    for page, body in (("/", home), ("/company/JPM", company)):
        foot = body.split("<footer>", 1)[1]
        assert 'href="/admin"' in foot, f"no way to admin from {page}"
    # And not in the nav, where it would compete with the product.
    assert 'href="/admin"' not in home.split("<nav>", 1)[1].split("</nav>", 1)[0]


def test_the_landing_page_states_its_scale_in_one_line(client):
    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")

    body = client.get("/").text

    assert 'class="summary"' in body
    assert "12 facts from SEC filings" in body
    assert "2 companies" in body
    assert "100.0% reconcile" in body
    assert 'href="#how"' in body


def test_the_summary_line_drops_clauses_it_cannot_fill(client):
    """An empty database gets a short line, not a boastful one about nothing."""
    body = client.get("/").text

    assert "facts from SEC filings" not in body
    assert 'href="#how"' in body, "the way to the explanation always shows"


def test_a_million_facts_reads_as_1_2m(client):
    """The summary is read at a glance; the exact digits are on /about."""
    from src.report.home_page import _compact

    assert _compact(1_237_331) == "1.2M"
    assert _compact(2_000_000) == "2M"
    assert _compact(5_944) == "5,944"
    assert _compact(41_233) == "41k"


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


def test_period_ends_are_never_reported_as_quarters(client):
    """Seven quarterly downloads produce ninety-odd distinct period ends,
    because filers close their books on different days. Counting those and
    calling them quarters overstated the load by an order of magnitude.

    Two companies with different fiscal year-ends are enough to reproduce it.
    """
    from src.company.stats import counts
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    _seed("JPM", _drawable())
    with session_scope() as s:
        for m, v in (("total_assets", 500e6), ("total_liabilities", 300e6),
                     ("total_equity", 200e6)):
            s.add(Fundamental(
                ticker="ODD", metric=m, value=v,
                period_end=dt.date(2025, 6, 30),   # a June fiscal year-end
                fiscal_period="FY", filing_date=dt.date(2025, 8, 1),
                source="sec", restated=False,
            ))

    c = counts()

    assert "quarters" not in c, "a period-end count is not a dataset count"
    # What IS reported is measured: the span of filing dates.
    assert c["earliest_filing"] == "2025-08-01"
    assert c["latest_filing"] == "2026-02-13"

    body = client.get("/").text
    assert "quarters" not in body.lower().replace(
        "sec quarterly financial statement data sets", "")
    assert "most recent filing" in body


def test_counts_survive_an_unreachable_database(monkeypatch):
    """The home page renders without its own statistics rather than 500ing."""
    import src.company.stats as stats

    def boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr("src.storage.db.session_scope", boom)
    monkeypatch.setattr(stats, "identity", lambda *a, **k: None)

    out = stats.site_stats()

    assert out["facts"] == 0 and out["identity"] is None


# ------------------------------------------------- the page explains itself
def test_the_page_explains_the_drawing(client):
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


def test_search_rejects_something_that_is_neither_ticker_nor_name(client):
    """It cannot be a symbol and matches no company, so it is answered here
    rather than redirected -- and whatever was typed is echoed escaped."""
    r = client.get("/search", params={"q": "<script>x</script>"})

    assert r.status_code == 404
    assert "<script>x</script>" not in r.text, "the echo must be escaped"
    assert "&lt;script&gt;" in r.text


# ------------------------------------------------------------ search by name
def _named(ticker: str, name: str, sector: str | None = None, **over) -> None:
    """A company with fundamentals AND a name in the universe."""
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed(ticker, _drawable(**over), sector=sector)
    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=PE, ticker=ticker, name=name,
                               sector=sector))


def test_a_company_name_lands_on_its_ticker(client):
    _named("WMT", "Walmart Inc.", "Consumer Staples")
    _named("JPM", "JPMorgan Chase & Co", "Financials")

    for query, expected in (("Walmart", "/company/WMT"),
                            ("walmart", "/company/WMT"),
                            ("JPMorgan", "/company/JPM"),
                            ("  jpmorgan chase ", "/company/JPM")):
        r = client.get("/search", params={"q": query}, follow_redirects=False)
        assert r.status_code == 303, query
        assert r.headers["location"] == expected, query


def test_an_exact_ticker_beats_any_name_match(client):
    """AAL is American Airlines. There is a company called Aalberts and it
    does not get a vote."""
    _named("AAL", "American Airlines Group Inc.", "Industrials")
    _named("ABC", "Aalberts Industries", "Industrials")

    r = client.get("/search", params={"q": "AAL"}, follow_redirects=False)

    assert r.headers["location"] == "/company/AAL"


def test_several_matches_offer_a_choice(client):
    """Nothing here is simply called "Walmart", so there is a real choice and
    the page has to let the reader make it."""
    _named("WMTX", "Walmart de Mexico SAB de CV", "Consumer Staples")
    _named("WMMY", "Walmart Chile SA", "Consumer Staples")
    _named("WMCA", "Walmart Canada Bank", "Financials")

    r = client.get("/search", params={"q": "walmart"})

    assert r.status_code == 200
    assert "companies match" in r.text
    for ticker in ("WMTX", "WMMY", "WMCA"):
        assert f'href="/company/{ticker}"' in r.text
    # Ticker, name and sector, so the choice can actually be made.
    assert "Walmart de Mexico SAB de CV" in r.text
    assert "Consumer Staples" in r.text


def test_a_legal_suffix_is_not_part_of_the_name(client):
    """"Walmart" and "Walmart Inc." are the same answer, so the plain company
    wins outright even with Walmart de Mexico in the same result set."""
    _named("WMT", "Walmart Inc.", "Consumer Staples")
    _named("WMTX", "Walmart de Mexico SAB de CV", "Consumer Staples")

    r = client.get("/search", params={"q": "walmart"}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/company/WMT"


def test_a_single_exact_prefix_does_not_stop_to_ask(client):
    """"walmart" matches three, but only one company is actually called that."""
    _named("WMT", "Walmart Inc.", "Consumer Staples")
    _named("WMTX", "Wal-Mart de Mexico", "Consumer Staples")
    _named("XYZ", "Acme Walmart Suppliers", "Industrials")

    r = client.get("/search", params={"q": "walmart"}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/company/WMT"


def test_a_name_with_nothing_to_draw_is_not_offered(client):
    """Matching a name and then landing on "nothing to draw" sends a reader
    from one empty page to another."""
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _named("WMT", "Walmart Inc.", "Consumer Staples")
    with session_scope() as s:  # a name, but no fundamentals behind it
        s.add(UniverseSnapshot(as_of_date=PE, ticker="GHOST",
                               name="Walmart Ghost Holdings"))

    r = client.get("/search", params={"q": "walmart"}, follow_redirects=False)

    assert r.headers["location"] == "/company/WMT", "the ghost is not a match"


def test_a_misspelling_is_offered_never_followed(client):
    """The rule that matters: a near-miss is a question, not an answer.

    Redirecting on a guess would put a company on screen nobody asked for,
    under a heading that reads as the site asserting it is the right one.
    """
    _named("WMT", "Walmart Inc.", "Consumer Staples")

    r = client.get("/search", params={"q": "walmrt"}, follow_redirects=False)

    assert r.status_code == 404, "a guess is never a redirect"
    assert "Nothing matches" in r.text
    assert "Did you mean" in r.text
    assert 'href="/company/WMT"' in r.text


def test_common_typos_reach_the_right_company(client):
    from src.company.lookup import resolve

    _named("WMT", "Walmart Inc.", "Consumer Staples")
    _named("MSFT", "Microsoft Corporation", "Information Technology")
    _named("AAL", "American Airlines Group Inc.", "Industrials")

    for typo, expected in (("walmrt", "WMT"), ("wallmart", "WMT"),
                           ("micrsoft", "MSFT"), ("microsofy", "MSFT"),
                           ("amercan airlines", "AAL")):
        found = resolve(typo)
        assert found.kind == "fuzzy", typo
        assert expected in [m.ticker for m in found.matches], typo


def test_two_different_companies_are_not_called_typos_of_each_other(client):
    """Walgreens is not a misspelling of Walmart. The threshold has to be high
    enough that a real company is never proposed as a correction to another."""
    from src.company.lookup import resolve

    _named("WMT", "Walmart Inc.", "Consumer Staples")
    _named("WBA", "Walgreens Boots Alliance", "Consumer Staples")

    found = resolve("walgreens")

    assert found.kind == "one"
    assert found.ticker == "WBA", "an exact match is never a fuzzy one"


def test_punctuation_is_not_a_misspelling(client):
    """"freeport mcmoran" matches FREEPORT-MCMORAN INC exactly as far as the
    reader is concerned. Answering it with "did you mean" would be wrong
    twice: it did match, and the site holds that name."""
    _named("FCX", "FREEPORT-MCMORAN INC", "Materials")

    r = client.get("/search", params={"q": "freeport mcmoran"},
                   follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/company/FCX"


def test_a_short_query_is_not_fuzzy_matched(client):
    """Under four characters everything is close to everything."""
    from src.company.lookup import close_matches

    _named("WMT", "Walmart Inc.", "Consumer Staples")

    assert close_matches("wal") == []


def test_a_ticker_typo_still_reaches_the_page_that_explains_it(client):
    """A short ticker-shaped miss goes to /company, which can say what is
    missing about that symbol, rather than guessing at a company name."""
    _named("JPM", "JPMorgan Chase & Co", "Financials")

    r = client.get("/search", params={"q": "JPMM"}, follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/company/JPMM"


def test_a_name_matching_nothing_says_so_and_offers_the_examples(client):
    _named("WMT", "Walmart Inc.", "Consumer Staples")

    r = client.get("/search", params={"q": "not a real company plc"})

    assert r.status_code == 404
    assert "No ticker or company name matches that" in r.text
    assert 'href="/company/WMT"' in r.text


def test_name_search_says_when_names_are_not_loaded(client):
    """Distinct from "no match": one is missing data, the other is the
    reader's query, and they have completely different fixes."""
    _seed("JPM", _drawable(), sector="Financials")  # fundamentals, no name

    r = client.get("/search", params={"q": "jpmorgan chase"})

    assert r.status_code == 404
    assert "Company names are not loaded" in r.text
    assert 'href="/company/JPM"' in r.text


def test_a_one_word_company_name_is_not_reported_as_a_dead_ticker(client):
    """THE reported bug. "walmart" is one word, so it is ticker-SHAPED, and the
    unloaded-names answer used to be skipped for exactly the queries that
    needed it: the reader was redirected to /company/WALMART and told there
    were no filed fundamentals for WALMART -- a symbol nobody typed, about a
    company the site had never looked for.
    """
    _seed("WMT", _drawable(), sector="Consumer Staples")  # fundamentals, no name

    for query in ("walmart", "WALMART", "Walmart", "microsoft"):
        r = client.get("/search", params={"q": query}, follow_redirects=False)

        assert r.status_code == 404, query
        assert "Company names are not loaded" in r.text, query
        assert "/company/WALMART" not in r.text, query


def test_a_query_that_could_be_a_symbol_is_still_answered_as_one(client):
    """The other half of the same rule: SPACE is a plausible ticker, so it goes
    to the page that can say what is missing about that symbol. Only queries
    too long to be a symbol are diverted."""
    _seed("JPM", _drawable(), sector="Financials")

    for query in ("SPACE", "space", "brk.b", "NOSUCH"):
        r = client.get("/search", params={"q": query}, follow_redirects=False)

        assert r.status_code == 303, query
        assert r.headers["location"].startswith("/company/"), query


def test_the_company_page_offers_the_company_whose_name_was_typed(client):
    """A miss that has an obvious right answer must show it.

    /company/WALMART is where a typed URL and a short name query both land.
    Listing WMT there is the difference between "we don't have Walmart" and
    "Walmart is one tap away".
    """
    _named("WMT", "Walmart Inc.", "Consumer Staples")

    r = client.get("/company/WALMART")

    assert r.status_code == 404, "nothing matched, and the page still says so"
    assert "Did you mean" in r.text
    assert 'href="/company/WMT"' in r.text


def test_the_company_page_offers_a_near_miss_too(client):
    _named("WMT", "Walmart Inc.", "Consumer Staples")

    r = client.get("/company/WALMRT")

    assert r.status_code == 404
    assert 'href="/company/WMT"' in r.text


def test_the_company_page_says_when_names_are_not_loaded(client):
    """The one page every unanswerable query reaches has to name the real
    cause, not describe the symptom as a missing ticker."""
    _seed("WMT", _drawable(), sector="Consumer Staples")  # fundamentals, no name

    r = client.get("/company/WALMART")

    assert r.status_code == 404
    assert "Company names are not loaded" in r.text


def test_a_drawable_company_page_is_untouched_by_any_of_that(client):
    """The suggestion machinery runs only on the empty state."""
    _named("WMT", "Walmart Inc.", "Consumer Staples")

    r = client.get("/company/WMT")

    assert r.status_code == 200
    assert "Did you mean" not in r.text


def test_the_universe_is_snapshotted_daily_so_matches_are_deduped(client):
    """One row per ticker in the results, not one per day the company existed."""
    import datetime as _dt

    from src.company.lookup import resolve
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _named("WMT", "Walmart Inc.", "Consumer Staples")
    with session_scope() as s:
        for day in range(1, 6):
            s.add(UniverseSnapshot(as_of_date=_dt.date(2026, 1, day),
                                   ticker="WMT", name="Walmart Inc."))

    found = resolve("walmart")

    assert found.kind == "one"
    assert found.ticker == "WMT"


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
