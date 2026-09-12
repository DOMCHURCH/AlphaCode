"""The pre-rendered company-page sections: what they say, and what they cost.

Two things are being defended here.

WHAT THEY SAY. The point of an introduction generated for 6,000 pages is that
it is different on each of them. A paragraph assembled from a template with a
name substituted in would satisfy "there is prose on the page" and fail the
only thing that makes prose worth having, so the uniqueness assertion below is
not decoration -- it is the feature. Equally, a sentence whose figure is
missing must vanish rather than degrade into "N/A" or "0".

WHAT THEY COST. The reason this feature is a table and not three queries is
that the page renders 6,169 times. `test_the_page_costs_one_extra_query` pins
that: it counts the SELECTs a render issues against `company_page_extras` and
fails if the number ever moves.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from src.report.page_extras import (
    _named_lines,
    _rank_phrase,
    build_company_ld,
    build_filings_html,
    build_intro,
    build_peers_html,
    filing_url,
    money,
)

Q = dt.date(2025, 12, 31)
PRIOR = dt.date(2024, 12, 31)
FILED = dt.date(2026, 2, 13)


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


# ---------------------------------------------------------------------------
# Pure rendering: no database required
# ---------------------------------------------------------------------------
def test_money_reaches_for_the_unit_a_reader_holds():
    assert money(4_424_900_000_000) == "$4.42T"
    assert money(62_600_000_000) == "$62.6B"
    assert money(-3_900_000_000) == "-$3.9B"
    assert money(None) == "—"


def test_the_article_agrees_with_the_sector_it_introduces():
    a = _text(build_intro(
        ticker="X", company_name="X Inc", sector="Industrials",
        assets=100.0, liabilities=60.0, equity=40.0,
    ))
    assert "an industrials company" in a
    b = _text(build_intro(
        ticker="Y", company_name="Y Inc", sector="Technology",
        assets=100.0, liabilities=60.0, equity=40.0,
    ))
    assert "a technology company" in b


def test_two_companies_never_get_the_same_paragraph():
    one = _text(build_intro(
        ticker="AAA", company_name="Alpha Inc", sector="Technology",
        assets=100e9, liabilities=40e9, equity=60e9, period_end=Q,
    ))
    two = _text(build_intro(
        ticker="BBB", company_name="Beta Corp", sector="Energy",
        assets=250e9, liabilities=200e9, equity=50e9, period_end=Q,
    ))
    assert one != two
    assert "Alpha Inc" in one and "Beta Corp" in two


def test_a_sentence_without_its_figure_does_not_appear_at_all():
    """No "N/A", no zero standing in for a number nobody filed."""
    body = _text(build_intro(
        ticker="Z", company_name="Zeta", sector=None,
        assets=None, liabilities=None, equity=None,
    ))
    assert "N/A" not in body
    assert "None" not in body
    assert "$0" not in body
    assert "total assets at" not in body


def test_the_year_on_year_line_appears_only_with_a_previous_period():
    without = _text(build_intro(
        ticker="Z", company_name="Zeta", sector=None,
        assets=110e9, liabilities=60e9, equity=50e9, period_end=Q,
    ))
    assert "against 2024-12-31" not in without

    with_prior = _text(build_intro(
        ticker="Z", company_name="Zeta", sector=None,
        assets=110e9, liabilities=60e9, equity=50e9, period_end=Q,
        prior_assets=100e9, prior_period_end=PRIOR,
    ))
    assert "grew 10.0%" in with_prior
    assert "2024-12-31" in with_prior


def test_a_shrinking_balance_sheet_is_said_to_shrink():
    body = _text(build_intro(
        ticker="Z", company_name="Zeta", sector=None,
        assets=90e9, liabilities=60e9, equity=30e9, period_end=Q,
        prior_assets=100e9, prior_period_end=PRIOR,
    ))
    assert "shrank 10.0%" in body


def test_negative_equity_is_named_rather_than_glossed():
    body = _text(build_intro(
        ticker="AAL", company_name="American Airlines", sector="Industrials",
        assets=62.6e9, liabilities=66.5e9, equity=-3.9e9,
        negative_equity=True,
    ))
    assert "Equity is negative" in body
    # And the leverage phrasing, which divides by equity, is suppressed.
    assert "for every dollar of equity" not in body


def test_the_identity_sentence_names_the_term_that_closed_it():
    for basis, phrase in (
        ("", "reconciles directly"),
        ("nci", "noncontrolling interest is included"),
        ("mezzanine", "mezzanine block is included"),
        ("nci+mezzanine", "all three are included"),
    ):
        body = _text(build_intro(
            ticker="T", company_name="T Inc", sector=None,
            assets=100.0, liabilities=60.0, equity=40.0,
            balances=True, identity_basis=basis,
        ))
        assert phrase in body, basis


def test_a_filing_that_does_not_reconcile_says_so_with_its_drift():
    body = _text(build_intro(
        ticker="T", company_name="T Inc", sector=None,
        assets=100.0, liabilities=60.0, equity=20.0,
        balances=False, imbalance_pct=20.0,
    ))
    assert "does not reconcile by 20.0%" in body
    assert "flagged rather than adjusted" in body


def test_the_remainder_is_never_reported_as_the_largest_line():
    """"Other assets" is our coverage gap, not a fact about the company."""
    lines = [
        {"label": "Other assets", "value": 900.0, "is_remainder": True, "kind": "asset"},
        {"label": "Goodwill", "value": 100.0, "is_remainder": False, "kind": "asset"},
    ]
    assert [c["label"] for c in _named_lines(lines)] == ["Goodwill"]


def test_the_largest_liability_is_a_liability_and_not_the_equity():
    body = _text(build_intro(
        ticker="B", company_name="Bank", sector=None,
        assets=1000.0, liabilities=900.0, equity=100.0,
        claim_lines=[
            {"label": "Shareholders' equity", "value": 100.0, "kind": "equity"},
            {"label": "Customer deposits", "value": 800.0, "kind": "liability"},
        ],
    ))
    assert "largest single liability is Customer deposits" in body
    assert "largest single liability is Shareholders" not in body


def test_the_rank_phrase_reads_as_english():
    assert _rank_phrase(1) == "the largest"
    assert _rank_phrase(2) == "the 2nd largest"
    assert _rank_phrase(3) == "the 3rd largest"
    assert _rank_phrase(11) == "the 11th largest"
    assert _rank_phrase(21) == "the 21st largest"


def test_a_sector_of_one_gets_no_ranking_sentence():
    body = _text(build_intro(
        ticker="T", company_name="T Inc", sector="Technology",
        assets=100.0, liabilities=60.0, equity=40.0,
        sector_rank=1, sector_total=1,
    ))
    assert "largest of the" not in body


# ---------------------------------------------------------------------------
# Peers, filings, JSON-LD
# ---------------------------------------------------------------------------
def test_peers_render_as_links_and_escape_their_names():
    html = build_peers_html([
        {"ticker": "AAA", "name": 'Alpha & Sons <script>', "assets": 1e9},
    ])
    assert '<a href="/company/AAA">' in html
    assert "&amp; Sons &lt;script&gt;" in html
    assert "<script>" not in html


def test_no_peers_means_no_section_rather_than_an_empty_heading():
    assert build_peers_html([]) == ""
    assert build_filings_html("X", []) == ""


def test_a_filing_without_every_part_gets_no_link_rather_than_a_broken_one():
    assert filing_url(None, "0000320193-26-000001", "a.htm") is None
    assert filing_url("320193", None, "a.htm") is None
    assert filing_url("320193", "0000320193-26-000001", None) is None
    url = filing_url("0000320193", "0000320193-26-000001", "aapl.htm")
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000001/aapl.htm"
    )


def test_a_filing_with_no_url_is_still_listed_as_text():
    html = build_filings_html("X", [
        {"form": "10-K", "filing_date": "2026-02-13", "url": None},
    ])
    assert "10-K" in html and "2026-02-13" in html
    assert "<a href" not in html


def test_the_company_jsonld_describes_the_company_not_the_publisher():
    block = build_company_ld(
        ticker="JPM", company_name="JPMorgan Chase & Co.",
        sector="Financial Services", origin="https://toscale.pro",
        assets=4.42e12, period_end=Q,
    )
    assert '"@type":"Organization"' in block
    assert '"tickerSymbol":"JPM"' in block
    assert "https://toscale.pro/company/JPM" in block
    # A dated figure, so it cannot age into an undated claim.
    assert '"observationDate":"2025-12-31"' in block


def test_the_jsonld_cannot_close_its_own_script_tag():
    block = build_company_ld(
        ticker="X", company_name="</script><script>alert(1)</script>",
        sector=None, origin="https://toscale.pro",
    )
    assert "</script><script>" not in block[:-9]
    assert block.endswith("</script>")


# ---------------------------------------------------------------------------
# End to end, against a real database
# ---------------------------------------------------------------------------
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'extras.db'}")
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


def _seed_sector(tickers: list[tuple[str, float]], sector: str) -> None:
    """Companies in one sector, each with a balance sheet that balances."""
    from src.storage.db import session_scope
    from src.storage.models import (
        FilingEvent,
        Fundamental,
        SectorMap,
        UniverseSnapshot,
    )

    with session_scope() as s:
        for ticker, assets in tickers:
            s.add(UniverseSnapshot(
                as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Inc",
                sector=sector,
            ))
            s.add(SectorMap(ticker=ticker, sector=sector, sector_source="sic"))
            for metric, value in (
                ("total_assets", assets),
                ("total_liabilities", assets * 0.6),
                ("total_equity", assets * 0.4),
            ):
                s.add(Fundamental(
                    ticker=ticker, metric=metric, value=value, period_end=Q,
                    fiscal_period="FY", filing_date=FILED, source="sec",
                ))
            # A previous period, so the year-on-year sentence has something.
            s.add(Fundamental(
                ticker=ticker, metric="total_assets", value=assets * 0.9,
                period_end=PRIOR, fiscal_period="FY",
                filing_date=dt.date(2025, 2, 13), source="sec",
            ))
            s.add(FilingEvent(
                ticker=ticker, cik="0000123456", form="10-K",
                filing_date=FILED, accession="0000123456-26-000001",
                primary_doc=f"{ticker.lower()}.htm",
            ))


def test_the_page_carries_every_section_once_it_is_backfilled(client):
    from src.report.page_extras_store import compute_and_store

    _seed_sector(
        [("AAA", 100e9), ("BBB", 90e9), ("CCC", 80e9), ("DDD", 70e9), ("EEE", 60e9)],
        "Technology",
    )
    assert compute_and_store("AAA", "https://toscale.pro") is True

    html = client.get("/company/AAA").text
    assert "company-intro" in html
    assert "company-peers" in html
    assert "company-filings" in html
    assert '"@type":"Organization"' in html
    # Peers are real links to real pages, and never to the company itself.
    assert '<a href="/company/BBB">' in html
    assert '<a href="/company/AAA">' not in html
    # The filing links straight to EDGAR.
    assert "sec.gov/Archives/edgar/data/123456" in html
    # The year-on-year sentence found its previous period.
    assert "grew 11.1%" in html
    # It is the largest of the five seeded.
    assert "the largest of the 5 technology companies" in html


def test_a_page_that_was_never_backfilled_still_renders(client):
    """A missing row costs sections, never the page."""
    _seed_sector([("AAA", 100e9)], "Technology")
    r = client.get("/company/AAA")
    assert r.status_code == 200
    assert "company-intro" not in r.text
    # The drawing itself is untouched.
    assert "What it owns, and who has a claim on it" in r.text


def test_the_page_costs_one_extra_query(client):
    """The whole reason this is a table. If this number moves, the 6,169-page
    render budget moved with it."""
    from sqlalchemy import event

    from src.report.page_extras_store import compute_and_store
    from src.storage.db import get_engine

    _seed_sector([("AAA", 100e9), ("BBB", 90e9)], "Technology")
    compute_and_store("AAA", "https://toscale.pro")

    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        # Scoped to statements that bind THIS ticker. The refresh hook that
        # runs after a fundamentals reload sweeps the whole table with an
        # unparameterised `SELECT ticker, source_period_end ...`, and if one is
        # still in flight from another test it lands inside this window and
        # counts as a read the render never issued. Binding on the ticker keeps
        # the measurement about this request rather than about test ordering.
        if "company_page_extras" not in statement.lower():
            return
        flat = str(params)
        if "AAA" in flat:
            seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        assert client.get("/company/AAA").status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", _record)

    assert len(seen) == 1, f"expected exactly one read, got {len(seen)}: {seen}"
    assert seen[0].lower().lstrip().startswith("select")


# ---------------------------------------------------------------------------
# The meta description, which has 155 characters before a result snippet cuts
# it off. 224 of the 6,184 company pages were over, all of them because the
# SEC registered name is long: the sentence around the name spends 111
# characters, and the names run to 60.
# ---------------------------------------------------------------------------


def _description(name, ticker="ZZ", total=673.8e9, period="2025-06-30"):
    import html as _html

    from src.report.company_page import _company_meta

    tags = _company_meta({
        "company_name": name, "ticker": ticker,
        "period_end": period, "total_assets": total,
    })
    found = re.search(r'<meta name="description" content="(.*?)">', tags)
    assert found, tags
    # Decoded, because "&amp;" is one ampersand to whatever reads the snippet.
    return _html.unescape(found.group(1))


# Every one of these was over 155 in production.
LONGEST_REAL_NAMES = [
    ("TLK", "PERUSAHAAN PERSEROAN PERSERO PT TELEKOMUNIKASI INDONESIA TBK"),
    ("HCAI", "Huachen AI Parking Management Technology Holding Co., Ltd"),
    ("VLRS", "Controladora Vuela Compania de Aviacion, S.A.B. de C.V."),
    ("NRUC", "NATIONAL RURAL UTILITIES COOPERATIVE FINANCE CORP /DC/"),
    ("FREVS", "FIRST REAL ESTATE INVESTMENT TRUST OF NEW JERSEY, INC."),
    ("ZION", "ZIONS BANCORPORATION, NATIONAL ASSOCIATION /UT/"),
    ("FNMA", "FEDERAL NATIONAL MORTGAGE ASSOCIATION FANNIE MAE"),
    ("KCA-UN", "Kensington Capital Acquisition Corp. VI"),
]


@pytest.mark.parametrize("ticker,name", LONGEST_REAL_NAMES)
def test_the_description_fits_a_snippet_however_long_the_name_is(ticker, name):
    desc = _description(name, ticker)
    assert len(desc) <= 155, f"{len(desc)}: {desc}"


def test_the_bound_holds_at_the_widest_the_figure_can_print():
    """The name is fitted against what the rest of the sentence left, so a
    trillion-dollar filer cannot push the description over on its own."""
    name = "PERUSAHAAN PERSEROAN PERSERO PT TELEKOMUNIKASI INDONESIA TBK"
    for total in (5.02e12, 673.8e9, 12.3e6, None):
        desc = _description(name, "ABCDEF", total)
        assert len(desc) <= 155, f"{total}: {len(desc)}: {desc}"


def test_a_name_that_already_fits_is_never_truncated():
    """The legal form goes from every description, not only the ones that are
    over, so that a snippet and the title above it name the company the same
    way. What a name that already fits does NOT get is an ellipsis."""
    desc = _description("NVIDIA CORP", "NVDA", 125.5e9)
    assert desc.startswith("NVIDIA (NVDA) balance sheet,")
    assert "\u2026" not in desc


def test_the_legal_form_goes_before_any_character_is_truncated():
    """Dropping ", INC." costs a reader nothing. Cutting the name costs them
    the word they searched for, so it happens last and only if it must."""
    desc = _description("FIRST REAL ESTATE INVESTMENT TRUST OF NEW JERSEY, INC.",
                        "FREVS", 400e6)
    assert "FIRST REAL ESTATE INVESTMENT TRUST" in desc


def test_a_truncated_name_is_cut_on_a_word_boundary():
    """A name cut mid-word reads as a bug rather than as an abbreviation."""
    name = "PERUSAHAAN PERSEROAN PERSERO PT TELEKOMUNIKASI INDONESIA TBK"
    desc = _description(name, "TLK", 17.2e9)
    shown = desc.split(" (TLK)", 1)[0]
    assert shown.endswith("\u2026")
    assert name.startswith(shown[:-1])
    assert name[len(shown) - 1] == " ", f"cut mid-word: {shown!r}"


def test_the_description_still_says_what_the_page_is_and_what_was_checked():
    """Shorter, not emptier. The identity check is the claim being made."""
    desc = _description("NVIDIA CORP", "NVDA", 125.5e9)
    assert "balance sheet" in desc
    assert "Total assets $125.5B." in desc
    assert "A = L + E" in desc


def test_the_rendered_page_carries_a_description_inside_the_limit(client):
    """End to end, through the real route rather than the helper."""
    import html as _html

    _seed_sector([("AAA", 100e9), ("BBB", 90e9)], "Technology")
    page = client.get("/company/AAA")
    assert page.status_code == 200
    found = re.search(r'<meta name="description" content="(.*?)">', page.text)
    assert found, "the company page carries no meta description"
    desc = _html.unescape(found.group(1))
    assert len(desc) <= 155, f"{len(desc)}: {desc}"
