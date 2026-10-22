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

import re

import datetime as dt

import pytest

PE = dt.date(2025, 12, 31)
FD = dt.date(2026, 2, 13)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
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
    assert "SEC EDGAR Balance Sheet API" in body
    assert "99.9%" not in body, "the retired accuracy claim must not return"
    # The old fallback description was the SAME sentence on all eight
    # shell pages, which is one page to a search engine and eight
    # near-duplicates to a crawler. Each page carries its own now.
    #
    # The Organization and WebSite nodes carry the defining sentence rather
    # than the data-formats blurb they used to share with the /pricing
    # Product block: "what is balanceproof.dev" is answered by naming the
    # category and the method, not by listing delivery formats.
    #
    # That category is the verification, not "a financial data API" -- the
    # shelf three larger vendors already occupy. This asserts SITE_DESCRIPTION
    # reaches the page, which is the string an engine quotes.
    from src.report.schema import SITE_DESCRIPTION

    assert SITE_DESCRIPTION in body
    # The hero's search box gave way to the free-key form. Bare /search
    # answers 400 by design, so the way to a company page that stays is the
    # A-to-Z directory, linked from the drawing's caption and the demo.
    assert 'href="/companies"' in body
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


# "Best for: one-time analysis, research, Excel work" -- on a PLAN card, about
# which of two products suits a reader's workflow. It is not a claim about a
# company, which is the only thing the ban below exists to stop: the failure
# mode being guarded against is this site telling somebody a stock is a good
# one. Stripped by the exact phrase and nothing wider, so "best" used as a
# judgement anywhere else on the page still fails.
# "Recommended for production" is the Pro plan's badge (owner's wording,
# 2026-10-19): a statement about which PLAN suits a production workload, the
# same kind of claim as "best for:". Exact phrase only; "recommend" anywhere
# else on these pages still fails.
_PRODUCT_FIT = ("best for:", "recommended for production")


# The href of the API roundup, which has "best" in its URL and is linked from
# the home page. Stripped as the exact PATH and nothing wider: the ban exists
# to stop this site telling somebody a stock is a good one, and a slug about
# which SEC filings API suits quant work is not that. The link's visible text
# carries no ranking word at all, so a reader never sees the judgement -- only
# a crawler reading the URL does.
#
# Deliberately not a relaxation of the word itself. "best" written as a
# judgement anywhere on these pages still fails, including in the anchor text
# of this very link.
_OWN_URLS = ("/best/sec-filings-api-for-quants",)


def _body_without_disclaimers(client, path: str = "/") -> str:
    """The page's PROSE, lower-cased, with the legitimate uses removed.

    JSON-LD is stripped first. It is markup, not writing -- and schema.org
    spells things like `operatingSystem`, which contains "rating" and trips a
    substring ban aimed at the sentence "we rate this stock". Leaving it in
    means the ban fails on a word no reader ever sees.
    """
    import re

    body = client.get(path).text.lower()
    body = re.sub(
        r'<script type="application/ld\+json">.*?</script>', "", body, flags=re.S
    )
    for phrase in _DISCLAIMERS + _PRODUCT_FIT + _OWN_URLS:
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
def test_the_landing_page_puts_the_key_and_a_real_call_before_showing_off(client):
    """One page, in the order a first-time reader needs it (light redesign,
    2026-10-19): the claim and the one ask, the proof strip, one real filing
    drawn, the buyer tabs (dark band), why pipelines break, how the check
    works, the live demo with a real request and response, pricing, trust, and
    the ask again in the closing dark band. `#demo < #how < #pricing` became
    `#how < #demo < #pricing`; tests/test_pricing_demo_recovery.py pins its own
    order."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text
    order = [
        'class="htitle"',
        "data-keyform",
        'class="proof"',
        "hero-bs",
        'id="use"',
        'id="pipelines"',
        'id="how"',
        'id="demo"',
        'class="codecard"',
        'id="pricing"',
        'id="trust"',
        'id="start"',
    ]
    at = [body.index(marker) for marker in order]
    assert at == sorted(at), dict(zip(order, at))
    # The drawing is the hero's own, from the same view the company page uses.
    assert body.index('href="/company/JPM"') > body.index('class="proof"')
    assert "Why SEC data breaks pipelines" in body
    assert 'class="example"' not in body


def test_the_nav_does_not_advertise_the_same_page_as_a_destination(client):
    """A nav item pointing at an anchor on the page you are already on reads
    as somewhere else to go. The explanation is a section further down this
    page, reached by scrolling, and the nav does not pretend otherwise."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text
    nav = body.split("<nav>", 1)[1].split("</nav>", 1)[0]

    assert "#how" not in nav
    assert 'id="how"' in body.split("</nav>", 1)[1], "the explanation is here"


def test_about_is_a_real_page_that_defines_the_product(client):
    """/about used to 307 onto /#how, because the explanation lived on the home
    page and a URL that once worked should keep working.

    It is a page again. Answer engines were describing this product out of
    three company pages -- the most numerous thing here, each mentioning the
    product only in passing -- because nothing on the site declared itself as
    the page about the product. A redirect cannot be cited; a page can.
    """
    r = client.get("/about", follow_redirects=False)

    assert r.status_code == 200
    body = r.text

    assert "<h1>About BalanceProof</h1>" in body
    assert "BalanceProof is the verification layer over SEC EDGAR" in body
    assert '"@type":"AboutPage"' in body, "the node an engine should prefer"
    assert "Dominique Church" in body, "who built it, on the page about it"
    for href in ('href="/methodology"', 'href="/pricing"'):
        assert href in body, f"/about must link {href}"


def test_the_home_page_carries_a_quotable_definition(client):
    """The sentence an assistant should lift, high under the H1.

    The lede now comes first and the definition follows it. Both used to open
    on the same clause, which on a phone rendered as one paragraph printed
    twice: the naming sentence belongs to the lede, and the definition below
    carries the method and the counts.
    """
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text
    hero = body.split('<header class="hero">', 1)[1].split("</header>", 1)[0]

    # The definition survives as the meta description -- which is what an
    # assistant actually lifts -- but it no longer OPENS the lede. It used to:
    # the most-read line on the page spent its first eight words naming a
    # category before saying what the reader gets, so the benefit now comes
    # first and the H1 and nav carry the name. See
    # docs/internal/homepage-checklist-2026-09-22.md, "Benefit before feature".
    assert "BalanceProof is the verification layer over SEC EDGAR" in body
    assert body.index('class="htitle"') < body.index('class="hlede"')
    lede = hero.split('class="hlede"', 1)[1].split("</p>", 1)[0]
    # The subline says how each statement is checked, what it is returned with,
    # and what a statement that cannot be verified gets: the reason, not an
    # adjusted number.
    lede = " ".join(lede.split())
    assert "checked against assets = liabilities + equity where the filing reports the lines needed" in lede
    assert "returned with the SEC filing it came from" in lede
    assert "returns the reason instead of adjusting the numbers" in lede
    # The naming clause must not come back into the hero, where it delayed the
    # benefit. Once, in the head, is where it belongs.
    assert "BalanceProof is the verification layer" not in hero

    # The method sentence MOVED OUT of the hero and into "How it works",
    # where it is that section's thesis rather than a second lede. What must
    # not change is that it is still on the page exactly once: it is the
    # sentence an assistant lifts when asked what this product does.
    assert "Each filing is checked against Assets = Liabilities + Equity" in body
    assert body.count(
        "Each filing is checked against Assets = Liabilities + Equity"
    ) == 1, "the method sentence must appear once, not once per section"
    assert body.index('class="hlede"') < body.index('id="how"'), (
        "the lede comes before the explanation"
    )
    # The counts belong to the strip and must not come back into the method
    # sentence, wherever that sentence lives. It now opens "How it works"
    # rather than the hero, so that is where this looks for it.
    method = body.split('id="how"', 1)[1].split("</p>", 1)[0]
    assert "Each filing is checked against Assets = Liabilities + Equity" in method
    assert "companies covered" not in method
    assert "reconcile directly" not in method

    # And it stays quotable where a crawler looks first.
    assert "accounting identity" in body or "A = L + E" in body, (
        "the method must stay quotable from the home page"
    )
    desc = re.search(r'<meta name="description" content="([^"]+)"', body)
    assert desc and ("A = L + E" in desc.group(1)
                     or "accounting identity" in desc.group(1)), (
        "the description a crawler reads must still carry the method"
    )
    assert 'href="/about"' in body, "one visible route to the About page"


def test_admin_is_not_linked_from_any_public_page(client):
    """/admin is the operator's page, opened by URL. It is not linked from the
    home page footer, the nav or any company page: it is disallowed in
    robots.txt, and a public link is only a hint to scanners. The routes are
    unchanged and the data and action routes still need the admin secret
    (see the require_admin tests)."""
    _seed("JPM", _drawable(), sector="Financials")

    home = client.get("/").text
    company = client.get("/company/JPM").text

    foot = home.split("<footer>", 1)[1]
    assert 'href="/admin"' not in foot, "a public Admin link in the footer"
    assert 'href="/admin"' not in company, "an admin link on a company page"
    assert 'href="/admin"' not in home.split("<nav>", 1)[1].split("</nav>", 1)[0]
    # The page itself still serves, and its data route still refuses without the secret.
    assert client.get("/admin").status_code == 200
    assert client.get("/admin.json").status_code in (401, 403, 503)


def test_the_landing_page_states_its_scale_above_the_fold(client):
    from src.company.stats import identity

    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")
    identity(max_age_s=0.0)

    body = client.get("/").text
    # The proof strip under the hero: same figures, each counted live.
    # Figures sit in <span class="num"> for the motion layer, so compare text.
    proof = body.split('class="bproof"', 1)[1].split("</section>", 1)[0]
    flat = " ".join(re.sub(r"<[^>]+>", " ", proof).split())
    assert "12 facts" in flat, "the fact count"
    assert "2 companies covered" in flat, "the company count"
    assert "reconcile" in flat and "flagged with a reason" in flat
    assert "100.0%" not in proof, "no rate may be published"
    assert body.index('class="proof"') < body.index('id="how"')


def test_the_status_strip_drops_cells_it_cannot_fill(client):
    """An empty database gets a short strip, not a boastful one about nothing.
    A figure that cannot be counted is omitted rather than shown as zero."""
    # The breakdown is a module-level cache; without this reset a previous
    # test's counts would be read for this empty database.
    from src.company.stats import reset_breakdown_cache

    reset_breakdown_cache()
    body = client.get("/").text

    strip = body.split('class="proof"', 1)[1].split("</div></div>", 1)[0]
    for absent in ("facts", "covered", "reconcile"):
        assert absent not in strip, f"{absent} must be dropped, not zeroed"
    assert "SEC EDGAR" in strip, "what is always true still shows"
    assert 'id="how"' in body, "the explanation always renders"


def test_a_million_facts_reads_as_1_2m(client):
    """The summary is read at a glance; the exact digits are on /about."""
    from src.report.home_page import _compact

    assert _compact(1_237_331) == "1.2M"
    assert _compact(2_000_000) == "2M"
    assert _compact(5_944) == "5,944"
    assert _compact(41_233) == "41k"


# ------------------------------------------------------------ the numbers
def test_the_scale_is_counted_live_not_written_down(client):
    """A hardcoded row count is wrong the first time the data reloads. The proof
    strip prints what the database returned, as static text: no count-up, so no
    animation frame can show a figure the server did not send (a mid-run
    capture once showed "1M facts / 3,514 companies" against 2M / 6,251)."""
    from src.company.stats import counts

    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")

    c = counts()
    body = client.get("/").text

    assert c["companies"] == 2
    assert c["facts"] == 12, "6 metrics x 2 companies"
    text = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    assert f"{c['facts']:,} facts" in text
    assert "data-countup" not in body and "countup.js" not in body


def test_drawable_is_narrower_than_present(client):
    """A company with facts but no positive total for assets has nothing to
    scale a drawing to. Counting it would overstate what the site can show."""
    from src.company.stats import counts

    _seed("JPM", _drawable())
    _seed("GHOST", {"cash": 5.0, "total_assets": 0.0})

    c = counts()

    assert c["companies"] == 2
    assert c["drawable"] == 1


def test_the_counts_are_absent_on_an_empty_database(client):
    """Better to say nothing than to print zeroes as if they were a scale.

    This used to guard a second counts section further down the page. That
    section printed facts, companies and latest-filing -- every one of which
    the status strip already carried a screen above it -- so it was deleted
    and the rule it existed for moved onto the strip, which drops a cell it
    cannot count rather than reporting a zero for it.
    """
    body = client.get("/").text

    assert "as-reported fact" not in body
    flat = body.replace("\n", "")
    assert ">Companies<" not in flat, "no count cell without a count"
    assert ">Facts<" not in flat


def test_the_identity_line_is_omitted_until_it_is_known(client, monkeypatch):
    """It is a claim about every company, so it is stated only once actually
    computed -- never guessed at, never defaulted to 100%."""
    import src.company.stats as stats

    _seed("JPM", _drawable(), sector="Financials")
    monkeypatch.setattr(stats, "identity", lambda *a, **k: None)

    body = client.get("/").text

    assert 'class="proof"' in body, "the strip still shows"
    assert "SEC EDGAR" in body
    assert "satisfy assets = liabilities + equity" not in body


def test_the_identity_line_counts_companies_and_publishes_no_rate(client):
    """JPM balances exactly; the ghost has no complete sheet so is not counted.

    The rate is still COMPUTED -- it is read internally and by
    `scripts/identity_failures.py` -- and deliberately not rendered. The audit
    in docs/internal/identity-failures.md established that it measures whether
    a filing balances against its OWN stated total rather than whether we
    recovered every component, so publishing it under the word "accuracy" says
    something the number does not support. What the page shows instead is the
    count it is entitled to: how many complete balance sheets were checked.
    """
    from src.company.stats import identity

    _seed("JPM", _drawable(), sector="Financials")
    _seed("GHOST", {"total_assets": 100.0})

    ident = identity(max_age_s=0.0)
    body = client.get("/").text

    assert ident["checkable"] == 1, "only companies with a full sheet count"
    assert ident["pass_rate_pct"] == 100.0, "still computed for internal use"
    assert "100.0%" not in body, "the rate must not be published"
    assert "checked against assets = liabilities + equity" in body


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
    # The strip's wording for the same fact. What matters is that a filing
    # DATE is what gets reported, never a count of period ends called
    # "quarters" -- see the docstring.
    assert "Latest filing" in body


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

    assert "Both columns are the same money, counted twice" in body
    assert "same money, counted twice" in body
    # The real filing is drawn. The made-up schematic that used to teach the
    # shape first was dropped: the real one teaches it just as well, and an
    # invented company has no place beside real figures.
    assert "hero-bs" in body
    assert 'class="example"' not in body


def test_nothing_drawn_on_the_home_page_is_a_made_up_company(client):
    """The schematic was labelled "not a real company", which was right while it
    was there. It is gone, so the guard is that nothing like it comes back:
    an illustration with round numbers sitting among real filings is exactly
    the kind of thing this project exists to avoid."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text

    assert 'class="example"' not in body and 'class="exband"' not in body


def test_the_fold_opens_on_a_real_balance_sheet(client):
    """Real output early: the full drawing follows the proof strip, before the
    explanation and the demo."""
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text
    assert body.index('hero-bs') < body.index('id="how"') < body.index('id="demo"')
    hero = body.split('hero-bs', 1)[1].split("</section>", 1)[0]
    assert 'class="stack"' in hero
    assert 'class="legend"' in hero, "the numbers, not only the shape"
    assert 'href="/company/JPM"' in hero


def test_the_hero_shows_nothing_rather_than_a_placeholder(client, monkeypatch):
    """A drawing that will not build is absent. A frame with nothing in it is a
    picture of nothing presented as a company."""
    import src.company.view1 as v1

    _seed("JPM", _drawable(), sector="Financials")
    monkeypatch.setattr(v1, "build_view1", lambda *a, **kw: None)

    r = client.get("/")

    assert r.status_code == 200
    assert 'hero-bs' not in r.text
    assert 'class="proof"' in r.text, "the rest of the page still stands"


def test_the_status_strip_has_no_updated_cell(client):
    """Freshness is carried by "Latest filing loaded" only. The old "Updated -
    Nd ago" cell reported the last scheduler load of any kind."""
    import src.report.home_page as hp

    _seed("JPM", _drawable(), sector="Financials")
    strip = client.get("/").text.split('class="proof"', 1)[1].split("</div></div>", 1)[0]
    assert "Updated" not in strip and "ago<" not in strip
    assert ">Latest filing loaded<" in strip
    assert not hasattr(hp, "_pipeline_age")


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

    # Five characters or fewer, because that is what a US ticker is. Longer
    # than that and the query is treated as a name -- see the test below.
    r = client.get("/search", params={"q": "NOSUX"}, follow_redirects=True)

    assert r.status_code == 404
    assert "Nothing to draw for NOSUX" in r.text
    assert 'href="/company/JPM"' in r.text, "offer one that exists"


def test_a_long_word_is_a_name_not_a_ticker(client):
    """The Walmart bug: "Walmart" is alphanumeric, so the ticker-shaped
    fallthrough sent it to /company/WALMART -- a page about a symbol that does
    not exist, presented as though the site had understood the reader."""
    _seed("JPM", _drawable(), sector="Financials")

    r = client.get("/search", params={"q": "Walmart"})

    assert r.status_code == 404
    assert "/company/WALMART" not in r.text
    assert r.headers.get("location") != "/company/WALMART"


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
    assert "No companies found matching" in r.text
    assert "not a real company plc" in r.text
    assert 'href="/company/WMT"' in r.text


def test_name_search_says_when_names_are_not_loaded(client):
    """Distinct from "no match": one is missing data, the other is the
    reader's query, and they have completely different fixes."""
    _seed("JPM", _drawable(), sector="Financials")  # fundamentals, no name

    r = client.get("/search", params={"q": "jpmorgan chase"})

    assert r.status_code == 404
    assert "Company names are not loaded" in r.text
    assert 'href="/company/JPM"' in r.text


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

    assert "BalanceProof" in client.get("/").text
    assert "BalanceProof" in client.get("/company/JPM").text
    assert client.get("/api.json").json()["service"] == "BalanceProof"


def test_the_homepage_actually_asks_for_something(client):
    """The ask is the form itself: one field and "Get free key", in the hero and
    again in the closing band, with the objection answered where it forms. The
    API docs button moved out of the hero to the demo (owner, 2026-10-19)."""
    body = client.get("/").text
    main = body.split("<main", 1)[1]

    hero = main.split("</header>", 1)[0]
    assert ">Get free key<" in hero, "the call to action is below the fold"
    assert "data-keyform" in hero
    assert "Talk to us" in hero
    assert "no card" in hero.lower()
    assert "no password" in hero.lower()
    assert main.count(">Get free key<") >= 2
    assert 'href="/api"' in main.split('id="demo"', 1)[1].split('id="pricing"', 1)[0]


def test_the_demo_comes_before_the_price(client):
    """Pricing used to sit two sections above the live demo, which asked a
    first-time reader for money before showing them the product working."""
    body = client.get("/").text
    main = body.split("<main", 1)[1]

    demo = main.find("Live demo")
    price = main.find("<h2>Pricing</h2>")
    assert demo != -1 and price != -1
    assert demo < price, "pricing appears before the demo again"


def test_the_page_does_not_end_on_what_it_cannot_do(client):
    """"What this doesn't do" is the right note to be honest on and the wrong
    one to strand a convinced reader on."""
    body = client.get("/").text
    main = body.split("<main", 1)[1]

    limits = main.find("What this doesn't do")
    closing = main.find('id="start"')
    assert limits != -1 and closing != -1
    assert limits < closing, "nothing to act on after the limits"


# ----------------------------------------------------- the API front door
# The home page leads with a free key and a real request and response. These
# tests defend what that promises: the headline names the API, the form is one
# field and works without script, the examples are real and name one company
# throughout, and nothing on the page is invented.


def _hero_of(body: str) -> str:
    return body.split('<header class="hero">', 1)[1].split("</header>", 1)[0]


def test_the_hero_leads_with_the_api_and_a_one_field_key_form(client):
    from src.config.settings import get_settings

    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    hero = _hero_of(body)
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S).group(1)

    # Each word is its own span for the kinetic rise-in; the text is the sentence.
    assert re.sub(r"<[^>]+>", "", h1) == "Balance-checked SEC financials for production data pipelines."
    assert body.count("<h1") == 1
    flat_hero = " ".join(hero.split())
    assert "assets = liabilities + equity" in flat_hero
    assert "the API returns the reason" in flat_hero
    assert 'href="/verification"' in hero

    form = hero.split("<form", 1)[1].split("</form>", 1)[0]
    assert form.count("<input") == 1 and 'type="email"' in form
    assert ">Get free key<" in form

    # Read from settings, never typed: production runs its own allowance.
    calls = get_settings().free_tier_monthly_calls
    assert f"{calls:,} calls a month. No card, no password." in hero
    assert ">API docs<" in hero and 'href="/api"' in hero
    # "Asked" is what the server requires of accept_terms: the sentence sits
    # next to the form, and the script only sends it from a submit made there.
    assert 'href="/terms"' in hero and 'href="/privacy"' in hero


def test_the_key_forms_work_without_script_and_the_key_panel_ships_hidden(client):
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text

    forms = re.findall(r"<form[^>]*data-keyform[^>]*>", body)
    assert len(forms) == 2, "the hero's and the closing call to action's"
    for tag in forms:
        # Blocked script: a GET to the dashboard, which has the sign-up form.
        assert 'action="/dashboard"' in tag and 'method="get"' in tag
    assert sorted(re.findall(r'id="((?:hero|cta)-email)"', body)) == [
        "cta-email", "hero-email",
    ]

    # Anything that needs script is hidden until script reveals it, so a
    # failed script leaves nothing dead on the page.
    for tag in re.findall(r"<button[^>]*data-copy-from[^>]*>", body):
        assert " hidden" in tag, tag
    for tag in re.findall(r'<div class="keyout"[^>]*>', body):
        assert " hidden" in tag, tag
    assert 'class="keyout"' in body and "YOUR_KEY" in body
    # No key is ever written into the page: it is issued once, to one reader.
    assert "data-keyvalue></code>" in body


def test_the_home_script_registers_with_terms_and_hands_the_key_over_once(client):
    # The form code lives in keyform.js, shared with the sign-up prompt.
    js = client.get("/static/keyform.js").text

    assert '"/api/auth/register"' in js
    assert "accept_terms: true" in js
    # Same store as the dashboard's own sign-up, so /dashboard finds the key.
    assert 'KEY_STORE = "toscale.api_key"' in js
    # The key goes into the page as text, never as markup.
    assert 'querySelector("[data-keyvalue]").textContent = key' in js
    # An address that already has a key is sent to sign in, and is never shown
    # the key: that would turn the open endpoint into a lookup.
    assert "r.status === 409" in js and '"/login"' in js
    # Somebody who has just taken a key is not offered one by the sign-in sheet.
    assert 'bp.prompt_after", "yes"' in js


def test_the_card_has_real_examples_for_one_company_in_four_tabs(client):
    from html import unescape

    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    card = unescape(body.split('class="codecard"', 1)[1].split("</section>", 1)[0])

    assert card.count('class="cc-radio"') == 4
    for tab in ("curl", "PowerShell", "Python", "MCP"):
        assert f">{tab}</label>" in card
    # One company throughout, so the tabs cannot disagree about the example.
    assert "curl https://balanceproof.dev/api/demo/JPM" in card
    assert 'requests.get("https://balanceproof.dev/api/demo/JPM", timeout=30)' in card
    assert "claude mcp add --transport http balanceproof https://balanceproof.dev/mcp" in card
    # The key is a placeholder; a real one is only ever handed over by the
    # sign-up, to the person who made it.
    assert 'X-API-Key: YOUR_KEY' in card
    # The MCP tab shows what the tool returns, which is where the verdict lives.
    assert "balances" in card.split("cc-p-mcp", 1)[1]


def test_with_nothing_loaded_the_card_is_left_out_not_faked(client):
    body = client.get("/").text

    assert 'class="codecard"' not in body
    assert "data-keyform" in body, "the sign-up does not depend on the data"


def test_the_steps_explain_the_flag_and_never_accuse(client):
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    steps = body.split('id="how"', 1)[1].split('id="pricing"', 1)[0]

    for needle in ("How a filing is checked", "What a flag means",
                   "What you get back", "not an accusation"):
        assert needle in steps, needle
    # Most flags are a gap in what WE read, so the copy must never frame a
    # flag as the company's fault (docs: Decisions, "not forensic red flags").
    for banned in ("red flag", "fraud", "misstat"):
        assert banned not in steps.lower(), banned


def test_the_home_page_invents_no_customers_ratings_filings_or_claims(client):
    _seed("JPM", _drawable(), sector="Financials")
    main = client.get("/").text.split("<main", 1)[1].lower()

    for invented in ("trusted by", "testimonial", "5-star", "as seen in",
                     "join thousands", "8-k", "1 in 5", "one time in five"):
        assert invented not in main, invented


def test_the_new_home_copy_has_no_em_dashes(client):
    _seed("JPM", _drawable(), sector="Financials")
    main = client.get("/").text.split("<main", 1)[1]
    # The drawing's aria-labels are older than this change and are not copy a
    # reader sees; everything else on the page is held to the site rule.
    main = re.sub(r'aria-label="[^"]*"', "", main)

    for dash in ("—", "&mdash;", "&#8212;", "&#x2014;"):
        assert dash not in main, repr(dash)

def test_the_home_page_sets_its_type_from_our_own_files_and_no_one_else(client):
    """Zodiak, Satoshi and Geist Mono are served from /static/media, so the home
    page makes no request to Google for type. Every other page still loads
    Jost and Space Mono as before."""
    from pathlib import Path

    import src.report.home_page as hp

    _seed("JPM", _drawable(), sector="Financials")
    home = client.get("/").text

    assert "fonts.googleapis.com" not in home and "fonts.gstatic.com" not in home
    for family in ("Zodiak", "Satoshi", "Geist Mono"):
        assert f'font-family:"{family}"' in home, family
    assert home.count('rel="preload"') >= 2, "the first screen's faces are preloaded"
    # Versioned, because /static is served immutable for a year.
    assert re.search(r'font-zodiak-300\.woff2\?v=\d+', home)

    # Every page shares the home page's type and theme since 2026-10-21, so
    # none of them asks Google for fonts either.
    for page in ("/about", "/pricing", "/company/JPM"):
        assert "fonts.googleapis.com" not in client.get(page).text, page

    media = Path(hp.__file__).parent / "static" / "media"
    for _fam, _w, _style, name in hp._FONT_FILES:
        data = (media / name).read_bytes()
        assert data[:4] == b"wOF2", f"{name} is not a woff2 file"

# ------------------------------------------------- the stage, the strip, the light
# The home page shows a real filing that balances beside a real one that does
# not. The second is never assumed: it is checked every time the page renders,
# because its caption makes a precise claim (the filer's own total balances; a
# line did not reach us) that is false the moment the filing is anything else.


def _seed_flagged_blank_cheque() -> None:
    """Artius II's 2026-06-30 balance sheet as the demo endpoint serves it: the
    filer states liabilities-plus-equity equal to its assets, but the components
    that reached us fall $232M short."""
    _seed("AACB", {
        "total_assets": 232_407_651.0,
        "current_assets": 161_879.0,
        "total_liabilities": 17_309_711.0,
        "liabilities_and_equity": 232_407_651.0,
        "current_liabilities": 4_709_711.0,
        "total_equity": -17_147_832.0,
    }, sector="Financials")


def test_a_real_flagged_filing_is_drawn_beside_the_one_that_balances(client):
    _seed("JPM", _drawable(), sector="Financials")
    _seed_flagged_blank_cheque()

    body = client.get("/").text
    stage = body.split('class="sec hero-bs stage two"', 1)[1].split("</section>", 1)[0]

    assert stage.count('class="bs-panel') == 2
    assert 'class="bs-panel ok"' in stage and ">Balances<" in stage
    assert 'class="bs-panel flag"' in stage and ">Flagged, and here is why<" in stage
    assert "Not read from this filing" in stage, "the gap is drawn, not hidden"
    flag_note = stage.split('class="bs-panel flag"', 1)[1].split("</figure>", 1)[0]
    flat = " ".join(flag_note.split())
    assert "the filer's own stated total balances" in flat
    assert "did not reach us" in flat
    # A gap in what we read is not an accusation about the company.
    for banned in ("fraud", "misstat", "red flag", "irregular"):
        assert banned not in flat.lower(), banned
    assert 'href="/company/AACB"' in stage


def test_the_flagged_panel_is_left_out_when_the_filing_balances(client):
    """Verified at render time, not assumed: a candidate that now balances must
    not sit under a caption that says it does not."""
    _seed("JPM", _drawable(), sector="Financials")
    _seed("AACB", {
        "total_assets": 232_407_651.0,
        "total_liabilities": 100_000_000.0,
        "total_equity": 132_407_651.0,
        "liabilities_and_equity": 232_407_651.0,
    }, sector="Financials")

    body = client.get("/").text

    assert 'class="bs-panel flag"' not in body
    assert "Flagged, and here is why" not in body
    assert 'class="sec hero-bs stage one"' in body


def test_the_flagged_panel_is_left_out_when_the_company_is_not_loaded(client):
    _seed("JPM", _drawable(), sector="Financials")

    body = client.get("/").text

    assert 'class="bs-panel flag"' not in body
    assert "One real filing, drawn" in body


def test_the_line_by_line_tables_are_folded_not_deleted(client):
    """They were twelve rows under one drawing and the longest block on the
    page. They are still in the page for search engines and screen readers."""
    _seed("JPM", _drawable(), sector="Financials")
    _seed_flagged_blank_cheque()

    body = client.get("/").text
    stage = body.split('class="sec hero-bs stage', 1)[1].split("</section>", 1)[0]

    assert stage.count('<details class="bs-lines">') == 2
    assert stage.count('<table class="legend">') == 4
    assert "<summary>Line by line</summary>" in stage
    assert "open" not in re.search(r'<details class="bs-lines"[^>]*>', stage).group(0)


def test_the_strip_sets_the_two_claims_large_and_the_plain_facts_small(client):
    """Each cell is a label and a value; the counts cell carries its date."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    proof = body.split('class="bproof"', 1)[1].split("</section>", 1)[0]
    labels = re.findall(r'<span class="pk">([^<]+)</span>', proof)
    assert labels == ["Source", "Latest filing loaded", "API", "Python", "MCP"], labels
    assert "SEC EDGAR" in proof
    lead = proof.split('class="proof-lead"', 1)[1].split("</p>", 1)[0]
    assert re.search(r"\d{4}-\d{2}-\d{2} \(UTC\)", lead), "the counts carry their date"
    assert ">Python client<" in proof and ">MCP server<" in proof and ">REST API<" in proof


def test_the_home_stylesheet_adds_no_blur_and_keeps_reduced_motion_honest(client):
    """The life added to this page is opacity and transform, once, and nothing
    that blurs: the page's one live blur (the strip) was removed."""
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    block = css.split("The home page's life: hierarchy, light and a little motion", 1)[1]

    assert "blur(" not in block, "no new blur"
    assert "backdrop-filter: none" in block, "the strip's blur is switched off"
    assert "animation-iteration-count" not in block and "infinite" not in block
    no_pref = block.split("@media (prefers-reduced-motion: no-preference)", 1)[1].split(
        "@media (prefers-reduced-motion: reduce)", 1)[0]
    assert "home-rise" in no_pref
    reduce = block.split("@media (prefers-reduced-motion: reduce)", 1)[1].split("/* ----", 1)[0]
    assert "home-fade" in reduce and "!important" in reduce
    assert "translateY" not in reduce, "reduced motion gets a fade, never movement"


def test_the_count_up_starts_when_the_strip_is_in_view_and_still_obeys_reduced_motion(client):
    js = client.get("/static/countup.js").text

    assert "IntersectionObserver" in js
    assert "unobserve" in js, "once per figure"
    assert "prefers-reduced-motion: reduce" in js
    assert not re.search(r"addEventListener\(\s*[\x22\x27]scroll", js), "no scroll listener"


def test_the_home_extras_are_memoised_and_cleared_with_the_panels(client):
    """The card sample, MCP verdict and flagged check read balance sheets, which
    `api.home` documents costing a 1.35s TTFB when done per request. They are
    built once and served from memory until the same call that clears the
    panels (`stats.clear_page_caches`) drops them."""
    from src.company import stats
    from src.report import home_page as hp

    _seed("JPM", _drawable(), sector="Financials")
    calls = {"n": 0}
    real = hp._api_sample

    def counting(ticker):
        calls["n"] += 1
        return real(ticker)

    hp._api_sample = counting
    try:
        client.get("/")
        after_first = calls["n"]
        client.get("/")
        client.get("/")
        assert after_first >= 1
        assert calls["n"] == after_first, "later visits must not re-read the sample"

        stats.clear_page_caches()
        client.get("/")
        assert calls["n"] > after_first, "clearing the page caches rebuilds it"
    finally:
        hp._api_sample = real


def test_the_home_navigation_does_not_blur_what_scrolls_under_it(client):
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    rule = css.split('body[data-bd="fluted"] nav {', 1)[1].split("}", 1)[0]

    assert "backdrop-filter: none" in rule
    life = css.split("The home page's life: hierarchy, light and a little motion", 1)[1]
    life = life.split("The backdrop: ONE fixed layer", 1)[0]
    # No CSS masks in the home page's additions: each one is an off-screen surface.
    assert not re.search(r"(^|[\s;{])(-webkit-)?mask(-image|-composite)?\s*:", life, re.M)


# --------------------------------------------------------------- review pass
def test_one_fixed_backdrop_sits_behind_every_page(client):
    """One picture, one layer, on every page that has a backdrop: WebP preferred
    and JPG as the fallback, 2000px wide and under 200 KB each. The home page is
    the exception since the light redesign (2026-10-19): white, no backdrop.
    Since 2026-10-21 every page is on that light paper too, so no page carries
    the backdrop; the picture's files stay, as the way back to it."""
    import struct
    from pathlib import Path

    import src.report.backdrop as bd

    _seed("JPM", _drawable(), sector="Financials")
    for page in ("/", "/about", "/pricing", "/company/JPM"):
        html = client.get(page).text
        assert 'class="backdrop-art"' not in html and 'data-theme="light"' in html, page
        if page != "/":
            assert "data-site" in html and "home-light.css" in html, page

    media = Path(bd.__file__).parent / "static" / "media"
    for name in ("backdrop-glass-2000.webp", "backdrop-glass-2000.jpg"):
        assert (media / name).stat().st_size < 200 * 1024, name
    assert not list(media.glob("backdrop-1200*")) and not list(media.glob("backdrop-2400*"))
    jpg = (media / "backdrop-glass-2000.jpg").read_bytes()
    i = 2
    while i < len(jpg):  # walk the JPEG markers to the frame header
        marker = jpg[i + 1]
        if marker in (0xC0, 0xC2):
            height, width = struct.unpack(">HH", jpg[i + 5:i + 9])
            break
        i += 2 + struct.unpack(">H", jpg[i + 2:i + 4])[0]
    assert width == 2000, width


def test_the_backdrop_css_is_fixed_isolated_masked_and_dims_on_a_phone(client):
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    block = css.split("The backdrop: ONE fixed layer behind every section of every page", 1)[1]

    assert "body { isolation: isolate; }" in block
    for needle in ("position: fixed;", "z-index: -1;", "pointer-events: none;",
                   "width: 60vw;", "background-size: cover;", "background-repeat: no-repeat;",
                   "background-position: right center;",
                   "mask-image: linear-gradient(90deg, transparent 0%, #000 32%);",
                   "#05060a"):
        assert needle in block, needle
    phone = block.split("@media (max-width: 640px)", 1)[1].split("}", 1)[0]
    assert "opacity: .35" in phone
    assert ".hero::before" in block and ".leadin::before" in block, "hero-only layers are off"


def test_the_nav_offers_a_key_and_the_demo_to_somebody_signed_out(client):
    _seed("JPM", _drawable(), sector="Financials")
    home = client.get("/").text.split("</nav>")[0]
    about = client.get("/about").text.split("</nav>")[0]

    assert ">Live demo<" in home and 'href="#demo"' in home
    assert 'href="#hero-email"' in home and home.count(">Get free key<") == 2, (
        "once in the bar (visible on a phone) and once in the links")
    assert 'href="/#demo"' in about and 'href="/dashboard"' in about
    # the sign-in pages do not offer to sign up
    login = client.get("/login").text.split("</nav>")[0]
    assert "Get free key" not in login


def test_the_footer_still_says_restatements(client):
    """The rename ships in its own commit; until then the label stays."""
    _seed("JPM", _drawable(), sector="Financials")
    footer = client.get("/").text.split("<footer>", 1)[1]
    assert ">Restatements<" in footer


def test_the_why_note_uses_only_what_the_published_report_says(client):
    """The three study numbers on the home page ("Why SEC data breaks
    pipelines", third column) are the report's own, checked against the post."""
    from pathlib import Path

    import src.report.blog as blog
    import src.report.home_page as hp

    post = next(p for p in blog.POSTS if p.slug == hp._REPORT_SLUG)
    assert f"{hp._REPORT_FAILED}" in post.title and "38,568" in post.title
    source = Path(blog.__file__).read_text(encoding="utf-8")
    assert f"{hp._REPORT_OURS} of the {hp._REPORT_FAILED}" in source

    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    why = body.split('id="pipelines"', 1)[1].split("</section>", 1)[0]
    assert f"/blog/{hp._REPORT_SLUG}" in why and "2026-10-09" in why
    assert "Dominique" not in why and "Church" not in why and "I'm" not in why and " I " not in why
    assert "995" in why and "38,568" in why and "971" in why
    for invented in ("customers", "testimonial", "trusted", "rated", "stars", "solo", "years old", "founder"):
        assert invented not in why.lower(), invented


def test_a_strip_after_how_it_works_offers_a_key_and_the_docs(client):
    """After the explanation the page offers the docs (in the demo) and ends on
    the key again, under the standing tagline."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    closing = body.split('id="start"', 1)[1].split("</section>", 1)[0]

    assert body.index('id="how"') < body.index('id="demo"') < body.index('id="pricing"') < body.index('id="start"')
    assert ">Get free key<" in closing and "data-keyform" in closing
    assert "prove they balance" in closing
    assert ">API docs<" in body.split('id="demo"', 1)[1].split('id="pricing"', 1)[0]


def test_the_counts_on_the_page_are_computed_from_the_database(client):
    """Nothing in the strip or the steps is a literal: seeding another company
    moves "All N companies" with it. The counts by reason, with their date and
    scope, are on /verification."""
    from src.company.stats import (
        reset_breakdown_cache,
        reset_counts_cache,
        reset_identity_cache,
    )

    reset_breakdown_cache()
    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")
    two = " ".join(client.get("/").text.split())
    assert "All 2 companies with a complete balance sheet" in two

    _seed("WMT", _drawable(), sector="Consumer Staples")
    reset_identity_cache()
    reset_counts_cache()
    reset_breakdown_cache()
    three = " ".join(client.get("/").text.split())
    assert "All 3 companies with a complete balance sheet" in three
    assert "3 companies covered" in " ".join(re.sub(r"<[^>]+>", " ", three).split())
    assert "genuinely fail" not in three
    ver = " ".join(re.sub(r"<[^>]+>", " ", client.get("/verification").text).split())
    assert "0 rounding, 0 a line item that did not reach us" in ver
    assert "0 a filing that does not balance" in ver


def test_the_card_label_says_illustrative_for_seeded_rows(client):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    _seed("JPM", _drawable(), sector="Financials")
    assert "real, for " in client.get("/").text

    from src.report import home_page as hp
    hp.reset_home_extras_cache()
    with session_scope() as s:
        s.query(Fundamental).filter(Fundamental.ticker == "JPM").update({"source": "demo"})
    assert "illustrative, for " in client.get("/").text


def test_the_search_box_is_back_on_the_home_page(client):
    """The ticker search lives in the live demo, out of the hero (owner,
    2026-10-19), so the hero asks for one thing."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    hero = body.split('<header class="hero">', 1)[1].split("</header>", 1)[0]
    demo = body.split('id="demo"', 1)[1].split('id="pricing"', 1)[0]
    assert 'id="q" name="q"' not in hero
    assert '<form class="search" action="/search" method="get" role="search">' in demo
    assert 'id="q" name="q"' in demo


def test_the_restored_sections_are_short_lists_from_live_counts(client):
    """Why pipelines break is three short columns; what we can state about the
    data and buying is one short list that still says what this doesn't do."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    why = body.split('id="pipelines"', 1)[1].split("</section>", 1)[0]
    trust = body.split('id="trust"', 1)[1].split("</section>", 1)[0]

    assert len(re.findall(r'class="pcol[ "]', why)) == 3
    for block in re.split(r'class="pcol[ "]', why)[1:]:
        text = re.sub(r"<[^>]+>", "", block.split("<p>", 1)[1].split("</p>", 1)[0])
        assert len(text) < 380, "a few lines, not a paragraph"
    assert 5 <= len(re.findall(r"<li[ >]", trust)) <= 7 and "No predictions" in trust
    assert "one time in five" not in " ".join(body.lower().split())


def test_the_drawing_is_explained_against_the_real_filing(client):
    """The explanation sits with the drawing itself rather than in a separate
    section a screen later."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    drawn = body.split("hero-bs", 1)[1].split("</section>", 1)[0]
    assert "Both columns are the same money, counted twice" in drawn
    assert "Left is what the company owns" in drawn


def test_the_home_footer_speaks_as_the_company(client):
    """Company voice: the footer names BalanceProof and the source, never a
    person, an age or "solo", and keeps the legal and contact links."""
    _seed("JPM", _drawable(), sector="Financials")
    footer = client.get("/").text.split("<footer>", 1)[1].split("</footer>", 1)[0]
    flat = " ".join(footer.split())

    assert flat.startswith("BalanceProof. Source:")
    assert "SEC Financial Statement Data Sets" in flat
    for gone in ("Founded by", "Dominique", "Church", "solo", "years old"):
        assert gone not in flat, gone
    for link in ('href="/terms"', 'href="/privacy"', "mailto:"):
        assert link in footer, link


def test_a_company_links_to_its_sector_hub_only_when_the_hub_exists(client):
    """/sector/{slug} 404s below sector_page.MIN_COMPANIES, so a company in a
    smaller sector gets plain text instead of a link to a missing page."""
    from src.report.company_facts import clear_cache
    from src.report.sector_page import MIN_COMPANIES, reset_hub_cache

    _seed("JPM", _drawable(), sector="Financials")
    page = client.get("/company/JPM").text
    assert 'href="/sector/financials"' not in page, "one company: no hub, no link"
    assert ">Financials<" in page, "the sector is still named"
    assert client.get("/sector/financials").status_code == 404

    for i in range(MIN_COMPANIES - 1):
        _seed(f"FIN{i}", _drawable(), sector="Financials")
    reset_hub_cache()
    clear_cache()
    page = client.get("/company/JPM").text
    assert 'href="/sector/financials"' in page
    assert client.get("/sector/financials").status_code == 200


def test_the_code_card_has_a_powershell_tab_with_curl_exe(client):
    """Windows PowerShell aliases curl to Invoke-WebRequest; the tab gives the
    two commands that work there."""
    _seed("JPM", _drawable(), sector="Financials")
    from html import unescape

    card = client.get("/").text.split('class="codecard"', 1)[1].split("</section>", 1)[0]
    assert 'id="cc-t-ps"' in card and '<label for="cc-t-ps">PowerShell</label>' in card
    ps = unescape(card.split('id="cc-ps-req">', 1)[1].split("</code>", 1)[0])
    assert "curl.exe https://balanceproof.dev/api/demo/" in ps
    assert 'curl.exe -H "X-API-Key: YOUR_KEY" https://balanceproof.dev/api/company/' in ps
    assert 'Invoke-RestMethod -Uri https://balanceproof.dev/api/company/' in ps
    assert '-Headers @{"X-API-Key"="YOUR_KEY"}' in ps
    # the curl tab is still the default without the script
    assert 'id="cc-t-curl"\n        aria-label="curl" checked' in card


def test_the_docs_say_to_use_curl_exe_on_windows_powershell(client):
    api = " ".join(client.get("/api").text.split())
    assert "On Windows PowerShell, use <code>curl.exe</code>" in api
    dash = " ".join(client.get("/dashboard").text.split())
    assert dash.count("On Windows PowerShell, use <code>curl.exe</code>") == 2


# ------------------------------------------------- sign-up prompt (nudge.js)
def test_the_signup_prompt_renders_only_on_company_pages_and_posts(client):
    from src.report.blog import POSTS

    _seed("JPM", _drawable(), sector="Financials")
    company = client.get("/company/JPM").text
    post = client.get(f"/blog/{POSTS[0].slug}").text
    for html in (company, post):
        assert 'id="signup-nudge"' in html and "data-nudge-end" in html
        assert "/static/nudge.js" in html and "/static/keyform.js" in html
    assert "Get JPM as JSON, free" in company
    assert "Get this data as JSON" in post
    for path in ("/", "/pricing", "/dashboard", "/login", "/dataset", "/api", "/about"):
        assert 'id="signup-nudge"' not in client.get(path).text, path


def test_the_signup_prompt_offers_no_discount_and_never_fires_on_a_timer(client):
    from pathlib import Path

    import src.report.nudge as nudge

    from src.config.settings import get_settings, price_label

    s = get_settings()
    raw = nudge.render_signup_nudge("JPM")
    html = raw.lower()
    # The free key is the main action; the one offer is the sign-in sheet's
    # existing one, hidden until the server reports it, worded from settings.
    offer = raw.split("data-nudge-offer", 1)[1].split("</p>", 1)[0]
    assert raw.index('id="nudge-email"') < raw.index("data-nudge-offer")
    assert "data-nudge-offer hidden" in raw
    assert "your first month of Pro" in offer and "monthly Pro only" in offer
    assert price_label(s.pro_price_usd) in offer
    assert f"{s.signin_offer_claim_minutes} minutes" in offer
    for word in ("hurry", "only today", "countdown", "expires in", "last chance"):
        assert word not in html, word
    assert "1,000 calls a month. no card, no password." in html or "calls a month. no card, no password." in html
    js = (Path(nudge.__file__).parent / "static" / "nudge.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in js.splitlines() if not line.strip().startswith(("/*", "*", "//")))
    for trigger in ("setTimeout", "setInterval", "beforeunload", "mouseout", "mouseleave", ".focus("):
        assert trigger not in code, trigger


def test_the_privacy_policy_mentions_the_prompt_flag(client):
    page = " ".join(client.get("/privacy").text.split())
    assert ("It also remembers, locally, whether you have closed the sign-up prompt on "
            "company pages and articles, so it is not shown again. That flag never "
            "leaves your device.") in page
    assert "Last updated 12 October 2026." in page


def test_nav_js_stands_down_where_the_signup_prompt_lives():
    from pathlib import Path

    import src.report.nudge as nudge

    js = (Path(nudge.__file__).parent / "static" / "nav.js").read_text(encoding="utf-8")
    assert 'if (document.getElementById("signup-nudge")) return;' in js


def test_a_bank_page_links_the_bank_post(client):
    _seed("JPM", _drawable(), sector="Financials")
    page = client.get("/company/JPM").text
    assert 'href="/blog/why-bank-balance-sheets-are-different"' in page


def test_the_report_is_linked_from_footer_company_methodology_about_and_compare(client):
    _seed("JPM", _drawable(), sector="Financials")
    for path in ("/company/JPM", "/methodology", "/about", "/compare/balanceproof-vs-intrinio"):
        assert 'href="/blog/which-sec-balance-sheets-fail-the-identity-check"' in client.get(path).text, path
    footer = client.get("/about").text.split("<footer>", 1)[1]
    assert '<a href="/blog/which-sec-balance-sheets-fail-the-identity-check">Data study</a>' in footer
    # the one-in-five post is still reachable from the company page and methodology
    assert "/blog/sec-xbrl-data-wrong-one-in-five" in client.get("/company/JPM").text
    assert "/blog/sec-xbrl-data-wrong-one-in-five" in client.get("/methodology").text


def test_the_footer_is_grouped_and_reaches_the_product_pages(client):
    footer = client.get("/about").text.split("<footer>", 1)[1].split("</footer>", 1)[0]
    heads = [h.split("</p>", 1)[0] for h in footer.split('class="foot-h">')[1:]]
    assert heads == ["Data", "Product", "Research", "Company"]
    for href in ("/companies", "/companies#by-sector", "/dataset", "/restatements", "/api",
                 "/mcp", "/pricing", "/free-sec-fundamentals-api", "/xbrl-balance-sheet-api",
                 "/compare", "/blog/which-sec-balance-sheets-fail-the-identity-check",
                 "/blog/sec-xbrl-data-wrong-one-in-five", "/blog", "/methodology", "/about",
                 "/terms", "/privacy"):
        assert f'href="{href}"' in footer, href
    for page in ("/dataset", "/mcp", "/xbrl-balance-sheet-api", "/free-sec-fundamentals-api"):
        assert client.get(page).status_code == 200, page


def _crumb_nav(html):
    return html.split('<nav class="crumbs"', 1)[1].split("</nav>", 1)[0]


def test_company_breadcrumb_links_the_sector_hub_only_when_it_exists(client):
    import json
    import re
    from src.report.sector_page import MIN_COMPANIES, reset_hub_cache

    _seed("JPM", _drawable(), sector="Financials")
    for i in range(MIN_COMPANIES - 1):
        _seed(f"BK{i}", _drawable(), sector="Financials")
    _seed("LONE", _drawable(), sector="Utilities")
    _seed("NOSEC", _drawable())
    reset_hub_cache()

    jpm = client.get("/company/JPM").text
    nav = _crumb_nav(jpm)
    assert '<a href="/">Home</a>' in nav and '<a href="/companies">Companies</a>' in nav
    hub = re.search(r'<a href="(/sector/[^"]+)">Financials</a>', nav)
    assert hub, nav
    assert client.get(hub.group(1)).status_code == 200
    assert '<span aria-current="page">' in nav and "(JPM)" in nav
    # the JSON-LD trail is the visible one
    ld = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', jpm, re.S)]
    trail = next(b for b in ld if b.get("@type") == "BreadcrumbList")["itemListElement"]
    assert [i["name"] for i in trail][:3] == ["Home", "Companies", "Financials"]
    assert trail[2]["item"].endswith(hub.group(1)) and trail[-1]["item"].endswith("/company/JPM")

    lone = _crumb_nav(client.get("/company/LONE").text)
    assert "<span>Utilities</span>" in lone and "/sector/" not in lone
    nosec = _crumb_nav(client.get("/company/NOSEC").text)
    assert nosec.count("<li>") == 3


def test_the_h1_full_stop_is_not_pushed_away_from_the_gradient_word():
    from pathlib import Path

    css = (Path(__file__).resolve().parents[1] / "src/report/static/glass.css").read_text(
        encoding="utf-8"
    )
    css = css.replace(chr(13), "")
    block = css.split(".htitle .h-ital {" + chr(10) + "    background", 1)[1].split("}", 1)[0]
    assert "padding-right: .08em" in block and "margin-right: -.08em" in block


def test_the_footer_columns_are_not_a_nav_element_which_the_header_rules_style(client):
    footer = client.get("/about").text.split("<footer>", 1)[1].split("</footer>", 1)[0]
    assert "<nav" not in footer
    assert 'role="navigation" aria-label="Site"' in footer
