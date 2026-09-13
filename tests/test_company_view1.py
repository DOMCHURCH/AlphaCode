"""View 1: the balance sheet drawn to scale.

The drawing's honesty is the thing under test. A missing component must be
absent and named rather than imputed; "other" must be a computed remainder that
says so; and negative equity must survive to the page rather than being
normalised into something that looks healthy.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.company.view1 import build_view1, describe_shape

PERIOD = dt.date(2025, 12, 31)
FILED = dt.date(2026, 2, 13)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'v1.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed(ticker: str, metrics: dict[str, float], name: str | None = None) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        for metric, value in metrics.items():
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=float(value),
                period_end=PERIOD, fiscal_period="FY", filing_date=FILED,
                source="sec", restated=False,
            ))
        if name:
            s.add(UniverseSnapshot(ticker=ticker, as_of_date=PERIOD, name=name))


FULL = {
    "total_assets": 1_000.0,
    "cash": 100.0,
    "receivables": 50.0,
    "inventory": 200.0,
    "property_plant_equipment": 400.0,
    "goodwill": 80.0,
    "intangibles": 20.0,
    "total_liabilities": 600.0,
    "accounts_payable": 150.0,
    "long_term_debt": 300.0,
    "total_equity": 400.0,
}


def test_no_fundamentals_returns_none(db):
    assert build_view1("NOPE") is None


def test_no_total_assets_returns_none(db):
    """Without a positive total there is no scale to draw to."""
    _seed("THIN", {"total_equity": 100.0})
    assert build_view1("THIN") is None


def test_zero_total_assets_returns_none(db):
    _seed("ZERO", {"total_assets": 0.0, "total_liabilities": 5.0})
    assert build_view1("ZERO") is None


def test_components_and_remainder_sum_to_the_total(db):
    _seed("FULL", FULL, name="Full Co")
    v = build_view1("FULL")

    assert v.mode == "detailed"
    assert v.company_name == "Full Co"
    assert sum(b.value for b in v.assets) == pytest.approx(v.total_assets)
    # 100+50+200+400+80+20 = 850, so the remainder is 150.
    other = [b for b in v.assets if b.is_remainder]
    assert len(other) == 1
    assert other[0].value == pytest.approx(150.0)


def test_claims_column_sums_to_the_same_total(db):
    """Equal height is the accounting identity, so the sums must match."""
    _seed("FULL", FULL)
    v = build_view1("FULL")
    assert sum(b.value for b in v.claims) == pytest.approx(v.total_assets)


def test_percentages_are_shares_of_total_assets(db):
    _seed("FULL", FULL)
    v = build_view1("FULL")
    assert sum(b.pct for b in v.assets) == pytest.approx(100.0)
    cash = next(b for b in v.assets if b.key == "cash")
    assert cash.pct == pytest.approx(10.0)


def test_a_missing_component_is_named_and_never_imputed(db):
    """Inventory absent for a bank is correct, not a gap to fill."""
    without = {k: v for k, v in FULL.items() if k != "inventory"}
    _seed("BANK", without)
    v = build_view1("BANK")

    assert "Inventory" in v.missing_components
    assert all(b.key != "inventory" for b in v.assets)
    # Its value is inside the remainder, and the remainder SAYS so rather than
    # letting it pass as a line the filer reported.
    other = next(b for b in v.assets if b.is_remainder)
    assert other.note and "1 line item" in other.note


def test_several_missing_components_are_counted_in_the_note(db):
    without = {
        k: v for k, v in FULL.items()
        if k not in ("inventory", "goodwill", "intangibles")
    }
    _seed("SOFT", without)
    v = build_view1("SOFT")
    other = next(b for b in v.assets if b.is_remainder)
    assert "3 line items" in other.note


def test_no_remainder_block_when_components_are_complete(db):
    exact = dict(FULL)
    exact["cash"] = 250.0  # 250+50+200+400+80+20 = 1000
    _seed("EXACT", exact)
    v = build_view1("EXACT")
    assert [b for b in v.assets if b.is_remainder] == []


def test_simplified_mode_only_when_nothing_is_broken_out(db):
    """A company with rendered components is not "simplified", whichever totals
    it happened to report."""
    _seed("SOME", {
        "total_assets": 1_000.0, "cash": 200.0, "inventory": 300.0,
        "liabilities_and_equity": 1_000.0, "total_equity": 400.0,
    })
    v = build_view1("SOME")
    assert v.mode == "detailed"
    assert len([b for b in v.assets if not b.is_remainder]) == 2


def test_totals_only_still_renders(db):
    """~89% of filers report totals without a full breakdown; that is a real
    answer, not a degraded one."""
    _seed("TOT", {
        "total_assets": 1_000.0, "total_liabilities": 600.0, "total_equity": 400.0,
    })
    v = build_view1("TOT")

    assert v.mode == "totals_only"
    assert v.total_assets == 1_000.0
    # One remainder block a side: the whole of assets, and liabilities + equity.
    assert len(v.assets) == 1 and v.assets[0].is_remainder
    assert sum(b.value for b in v.claims) == pytest.approx(1_000.0)


def test_missing_liability_total_is_derived_not_left_blank(db):
    """WMT's shape: no stated total liabilities, but the number is recoverable.

    Deriving it from the identity is arithmetic, not imputation -- the filer
    stated the other two terms, so the third is exact. Leaving half the drawing
    blank when the number is recoverable is the worse answer.
    """
    _seed("NOL", {"total_assets": 1_000.0, "cash": 400.0, "total_equity": 400.0})
    v = build_view1("NOL")

    assert v.total_liabilities == pytest.approx(600.0)
    assert v.liabilities_derived_from == "total assets, less equity"
    assert v.claims, "the claims column must not be empty when it is derivable"
    assert sum(b.value for b in v.claims) == pytest.approx(1_000.0)


def test_derivation_prefers_the_filers_own_stated_right_hand_side(db):
    """liabilities_and_equity has 93% coverage and is stated, not reconstructed."""
    _seed("RHS", {
        "total_assets": 1_000.0, "liabilities_and_equity": 1_000.0,
        "cash": 400.0, "total_equity": 350.0,
    })
    v = build_view1("RHS")
    assert v.total_liabilities == pytest.approx(650.0)
    assert v.liabilities_derived_from == "liabilities and equity, less equity"


def test_a_stated_liability_total_is_never_overwritten_by_a_derivation(db):
    _seed("STATED", FULL)
    v = build_view1("STATED")
    assert v.total_liabilities == 600.0
    assert v.liabilities_derived_from is None


def test_without_equity_there_is_nothing_to_derive_from(db):
    _seed("BARE", {"total_assets": 1_000.0, "cash": 400.0})
    v = build_view1("BARE")
    assert v.total_liabilities is None
    assert v.claims == []
    assert any("total for liabilities" in n for n in v.notes)


# ------------------------------------------------------------ negative equity
def test_negative_equity_is_kept_and_flagged(db):
    """AAL's real shape. Owing more than you own must look like it."""
    _seed("AAL", {
        "total_assets": 1_000.0,
        "property_plant_equipment": 600.0,
        "total_liabilities": 1_100.0,
        "long_term_debt": 500.0,
        "total_equity": -100.0,
    })
    v = build_view1("AAL")

    assert v.negative_equity is True
    eq = next(b for b in v.claims if b.kind == "equity")
    assert eq.value == -100.0
    # Drawn at its own magnitude, below the baseline.
    assert eq.pct == pytest.approx(10.0)
    # The claims column overruns the assets column, which is the point.
    assert v.claims_span_pct == pytest.approx(110.0)
    assert any("below the baseline" in n for n in v.notes)


def test_negative_equity_still_satisfies_the_identity(db):
    _seed("AAL", {
        "total_assets": 1_000.0, "total_liabilities": 1_100.0, "total_equity": -100.0,
    })
    v = build_view1("AAL")
    assert sum(b.value for b in v.claims) == pytest.approx(1_000.0)


def test_nci_inclusive_equity_is_preferred_when_reported(db):
    _seed("NCI", {
        "total_assets": 1_000.0, "total_liabilities": 700.0,
        "total_equity": 250.0, "total_equity_incl_nci": 300.0,
    })
    v = build_view1("NCI")
    assert v.total_equity == 300.0
    assert sum(b.value for b in v.claims) == pytest.approx(1_000.0)


# ------------------------------------------------------------- the description
def test_description_is_descriptive_not_a_judgement(db):
    _seed("FULL", FULL)
    sentences = " ".join(describe_shape(build_view1("FULL"))).lower()

    for word in ("good", "bad", "strong", "weak", "healthy", "risky",
                 "buy", "sell", "undervalued", "attractive", "should"):
        assert word not in sentences, f"{word!r} is judgement, not description"


def test_asset_heavy_company_is_described_as_physical(db):
    _seed("HEAVY", FULL)
    assert any("physical" in s for s in describe_shape(build_view1("HEAVY")))


def test_financial_company_is_described_as_financial(db):
    _seed("BANKY", {
        "total_assets": 1_000.0, "cash": 500.0, "receivables": 200.0,
        "total_liabilities": 900.0, "total_equity": 100.0,
    })
    assert any("financial" in s for s in describe_shape(build_view1("BANKY")))


def test_an_even_funding_split_is_not_called_either_way(db):
    """48/52 is neither owner-funded nor creditor-funded; saying so would be
    editorialising past the number."""
    _seed("EVEN", {
        "total_assets": 1_000.0, "total_liabilities": 520.0, "total_equity": 480.0,
    })
    sentences = " ".join(describe_shape(build_view1("EVEN")))
    assert "roughly evenly" in sentences
    assert "mostly" not in sentences


def test_negative_equity_is_described_plainly(db):
    _seed("AAL", {
        "total_assets": 1_000.0, "total_liabilities": 1_100.0, "total_equity": -100.0,
    })
    assert any(
        "negative" in s for s in describe_shape(build_view1("AAL"))
    )


# ------------------------------------------------------- sector-shaped filers
def test_a_bank_gets_bank_line_items(db):
    """JPM files no InventoryNet and no AccountsPayableCurrent.

    Against the general component set it renders as one undifferentiated
    block -- accurate and useless. Its money is in loans and securities and it
    is funded by deposits, so those are the lines that make it legible.
    """
    from src.storage.db import session_scope
    from src.storage.models import SectorMap

    _seed("JPM", {
        "total_assets": 1_000.0,
        "loans": 320.0,
        "trading_securities": 145.0,
        "investment_securities": 154.0,
        "interbank_deposits": 156.0,
        "total_liabilities": 918.0,
        "deposits": 588.0,
        "short_term_borrowings": 70.0,
        "long_term_debt": 97.0,
        "total_equity": 82.0,
    })
    with session_scope() as s:
        s.add(SectorMap(ticker="JPM", sic="6021", sector="Financial Services",
                        sector_source="sic"))

    v = build_view1("JPM")
    keys = {b.key for b in v.assets}
    assert "loans" in keys and "trading_securities" in keys
    assert {b.key for b in v.claims} >= {"deposits", "long_term_debt"}
    # It must NOT be asked for line items a bank does not file.
    assert "Inventory" not in v.missing_components
    assert "Accounts payable" not in v.missing_components


def test_a_non_bank_keeps_the_general_line_items(db):
    from src.storage.db import session_scope
    from src.storage.models import SectorMap

    _seed("MSFT", FULL)
    with session_scope() as s:
        s.add(SectorMap(ticker="MSFT", sic="7372", sector="Technology",
                        sector_source="sic"))

    v = build_view1("MSFT")
    assert "inventory" in {b.key for b in v.assets}
    assert "deposits" not in {b.key for b in v.claims}


def test_label_tiers_never_exceed_their_band(db):
    """A two-line label in a 21px band clips, which reads as a broken render."""
    from src.company.view1 import COMPACT_LABEL_MIN_PCT, FULL_LABEL_MIN_PCT, Block

    assert FULL_LABEL_MIN_PCT > COMPACT_LABEL_MIN_PCT
    # 3px per percent: a full two-line label needs ~27px, a compact one ~13px.
    assert FULL_LABEL_MIN_PCT * 3 >= 27
    assert COMPACT_LABEL_MIN_PCT * 3 >= 13

    b = lambda p: Block(key="k", label="L", value=1.0, pct=p, kind="asset")  # noqa: E731
    assert b(12.0).label_style == "full"
    assert b(7.0).label_style == "compact"
    assert b(1.0).label_style == "none"


# ---------------------------------------------------------------------------
# Resolving the symbol a reader actually has
# ---------------------------------------------------------------------------
# Two ways the URL's ticker is not the one the filings are stored under. The
# share-class case was already handled. The second is a registrant that
# renamed and took a new symbol: Equity Residential became Vivmark
# Residential, the filings stayed on CIK 906107, and the market symbol went
# from EQR to VMRK. /company/VMRK -- the only symbol a reader can now look up
# -- answered 404 while the balance sheet sat under EQR.


def _seed_filer(ticker, cik, *, in_sector_map=True, in_universe=False,
                name=None, assets=1000.0):
    import datetime as dt

    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    with session_scope() as s:
        if in_sector_map:
            s.add(SectorMap(ticker=ticker, cik=cik))
        if in_universe:
            s.add(UniverseSnapshot(
                as_of_date=dt.date(2026, 9, 7), ticker=ticker, name=name,
                cik=cik,
            ))
        if assets is not None:
            for metric, value in (("total_assets", assets),
                                  ("total_liabilities", assets * 0.6),
                                  ("total_equity", assets * 0.4)):
                s.add(Fundamental(
                    ticker=ticker, metric=metric, value=value,
                    period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
                    filing_date=dt.date(2026, 8, 1), source="sec",
                ))


def test_a_live_symbol_resolves_to_the_ticker_the_filings_are_under(db):
    """The EQR/VMRK case. VMRK is in `universe` with the same CIK and is not
    in `sector_map` at all, which is exactly why the old resolution missed
    it."""
    from src.company.lookup import canonical_ticker, reset_cache

    _seed_filer("EQR", "906107")
    _seed_filer("VMRK", "906107", in_sector_map=False, in_universe=True,
                name="VIVMARK RESIDENTIAL", assets=None)
    reset_cache()

    assert canonical_ticker("VMRK") == "EQR"
    assert canonical_ticker("vmrk") == "EQR"
    assert canonical_ticker("EQR") == "EQR", "the canonical must not move"


def test_the_page_renders_for_the_live_symbol(db):
    from src.company.lookup import reset_cache
    from src.company.view1 import build_view1

    _seed_filer("EQR", "906107")
    _seed_filer("VMRK", "906107", in_sector_map=False, in_universe=True,
                name="VIVMARK RESIDENTIAL", assets=None)
    reset_cache()

    view = build_view1("VMRK")
    assert view is not None, "the live symbol still has nothing to draw"
    assert view.ticker == "EQR"
    assert view.total_assets == 1000.0


def test_a_covered_ticker_is_never_moved_by_the_reverse_mapping(db):
    """A ticker `sector_map` knows is one we cover directly. If the universe
    pass could move it, a share class with its own page would start
    redirecting to its sibling."""
    from src.company.lookup import canonical_ticker, reset_cache

    _seed_filer("RDI", "1086222")
    _seed_filer("RDIB", "1086222")
    _seed_filer("RDI", "1086222", in_sector_map=False, in_universe=True,
                name="Reading International", assets=None)
    reset_cache()

    # Both are covered, so the share-class rule decides and the universe pass
    # leaves them alone.
    assert canonical_ticker("RDI") == "RDI"
    assert canonical_ticker("RDIB") == "RDI"


def test_an_unknown_symbol_is_returned_unchanged(db):
    from src.company.lookup import canonical_ticker, reset_cache

    _seed_filer("EQR", "906107")
    reset_cache()
    assert canonical_ticker("NOSUCH") == "NOSUCH"
    assert canonical_ticker("") == ""


def test_the_page_says_the_two_symbols_are_one_filer(db):
    """Rendered rather than redirected: the fact worth conveying is that they
    are the same registrant, and a 301 hides exactly that."""
    from src.company.lookup import reset_cache
    from src.company.view1 import build_view1
    from src.report.company_page import render_company_page

    _seed_filer("EQR", "906107")
    _seed_filer("VMRK", "906107", in_sector_map=False, in_universe=True,
                name="VIVMARK RESIDENTIAL", assets=None)
    reset_cache()

    view = build_view1("VMRK")
    html = render_company_page(view, requested_ticker="VMRK")
    assert "same registrant" in html
    assert "VMRK" in html

    # And nothing is said when the URL already names the canonical ticker.
    plain = render_company_page(view, requested_ticker="EQR")
    assert "same registrant" not in plain


def test_the_page_shows_the_name_the_filings_were_filed_under(db):
    """A renamed registrant renders under SEC's current name. The former name
    is what makes the page recognisable to somebody looking for the company
    that actually filed it."""
    import datetime as dt

    from src.company.view1 import build_view1
    from src.report.company_page import render_company_page
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed_filer("EQR", "906107", in_sector_map=True)
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date(2026, 9, 7), ticker="EQR",
            name="VIVMARK RESIDENTIAL", cik="906107",
            former_name="EQUITY RESIDENTIAL",
            former_name_until=dt.date(2026, 8, 12),
        ))

    view = build_view1("EQR")
    assert view.company_name == "VIVMARK RESIDENTIAL"
    assert view.former_name == "EQUITY RESIDENTIAL"

    html = render_company_page(view)
    assert "VIVMARK RESIDENTIAL" in html
    assert "filed as" in html
    assert "EQUITY RESIDENTIAL" in html
    assert "2026-08-12" in html


def test_a_company_that_never_renamed_gets_no_former_line(db):
    from src.company.view1 import build_view1
    from src.report.company_page import render_company_page

    _seed_filer("AAPL", "320193")
    view = build_view1("AAPL")
    assert view.former_name is None
    assert "formerly" not in render_company_page(view)


def test_a_rename_that_predates_the_filing_is_not_mentioned(db):
    """SEC's formerNames covers the whole life of a CIK. Apple was "APPLE INC"
    until 2019 and NVIDIA "NVIDIA CORP/CA" until 2002 -- both true, neither
    anything to do with a 2026 balance sheet. The line exists to reconcile a
    heading with the filings under it, so it only earns its place when those
    filings were made under the old name."""
    import datetime as dt

    from src.company.view1 import build_view1
    from src.report.company_page import render_company_page
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed_filer("AAPL", "320193", in_sector_map=True)
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date(2026, 9, 7), ticker="AAPL", name="Apple Inc.",
            cik="320193", former_name="APPLE INC",
            former_name_until=dt.date(2019, 1, 1),
        ))

    view = build_view1("AAPL")
    assert view.former_name == "APPLE INC", "the record itself still stands"
    html = render_company_page(view)
    assert "filed as" not in html
    assert "APPLE INC" not in html


# ---------------------------------------------------------------------------
# The <title>, which has about 60 characters before a result listing cuts it
# ---------------------------------------------------------------------------
# 1,022 of 6,184 company pages were over it, because SEC's registered names
# carry their legal form and often a state marker. Only the title is
# shortened; the heading keeps the name exactly as filed.

TITLE_TAIL = " (XXXX) Balance Sheet \u2014 BalanceProof"


def _title_len(short, ticker):
    return len(f"{short} ({ticker}) Balance Sheet \u2014 BalanceProof")


def test_the_legal_form_is_dropped_before_anything_is_truncated():
    from src.report.company_page import title_name

    assert title_name("HORNBECK OFFSHORE SERVICES, INC.", "HLX") == (
        "HORNBECK OFFSHORE SERVICES"
    )
    assert title_name("Walmart Inc.", "WMT") == "Walmart"
    assert title_name("NVIDIA CORP", "NVDA") == "NVIDIA"
    assert title_name("Apple Inc.", "AAPL") == "Apple"


def test_a_stripped_suffix_never_leaves_a_dangling_conjunction():
    """"JPMORGAN CHASE & CO" minus "CO" is "JPMORGAN CHASE &", which reads as
    a truncation bug. Caught by running the rule over the real name list."""
    from src.report.company_page import title_name

    assert title_name("JPMORGAN CHASE & CO", "JPM") == "JPMORGAN CHASE"


def test_the_state_marker_goes_whichever_way_it_leans():
    """SEC writes it both ways, sometimes for one company."""
    from src.report.company_page import title_name

    assert title_name("CACI INTERNATIONAL INC /DE/", "CACI") == "CACI INTERNATIONAL"
    assert title_name("US BANCORP \\DE\\", "USB") == "US BANCORP"


def test_several_legal_forms_are_stripped_not_just_the_last():
    """"PLC HOLDINGS LTD" is three of them. "Group" is NOT one: it belongs to
    the trading name, and dropping it turns Marex Group into Marex and the
    Glimpse Group into Glimpse."""
    from src.report.company_page import title_name

    assert title_name("Marex Group plc", "MRX") == "Marex Group"
    assert title_name("VivoPower International PLC", "VIVO") == (
        "VivoPower International"
    )
    assert title_name("Arena Group Holdings, Inc.", "AREN") == "Arena Group"
    assert title_name("Alps Global Holding Pubco Ltd", "ALPS").endswith("Pubco")


def test_a_name_too_long_even_stripped_is_cut_on_a_word_boundary():
    """A title cut mid-word reads as a bug rather than an abbreviation."""
    from src.report.company_page import title_name

    long = "PERUSAHAAN PERSEROAN PERSERO PT TELEKOMUNIKASI INDONESIA TBK"
    short = title_name(long, "TLK")
    assert _title_len(short, "TLK") <= 60
    assert short.endswith("\u2026")
    assert " " not in short[-2:], "cut mid-word"
    assert long.startswith(short[:-1].rstrip())


def test_a_name_that_is_only_a_legal_form_keeps_it():
    """Stripping everything would leave a page headed by nothing."""
    from src.report.company_page import title_name

    assert title_name("INC", "ZZZ") == "INC"
    assert title_name("Holdings", "HLD") == "Holdings"
    assert title_name("", "X") == ""


def test_the_page_title_fits_and_the_heading_does_not_change(db):
    """The two are deliberately different: the heading is the name as filed."""
    import datetime as dt

    from src.company.view1 import build_view1
    from src.report.company_page import render_company_page
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    _seed_filer("HLX", "866829", in_sector_map=True)
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date(2026, 9, 7), ticker="HLX",
            name="HORNBECK OFFSHORE SERVICES, INC.", cik="866829",
        ))

    html = render_company_page(build_view1("HLX"))
    import re as _re

    title = _re.search(r"<title>(.*?)</title>", html).group(1)
    assert len(title) <= 60, f"{len(title)}: {title}"
    assert title.startswith("HORNBECK OFFSHORE SERVICES (HLX)")
    # The heading is untouched.
    assert "<h1 class=\"cname\">HORNBECK OFFSHORE SERVICES, INC.</h1>" in html
