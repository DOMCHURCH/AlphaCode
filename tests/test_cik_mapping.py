"""Which ticker a filer's rows land under, and who can read them back.

Both halves of this were broken in a way that discarded real companies without
a single log line, so what is pinned here is the absence of silence as much as
the mapping itself.

  * SEC's `company_tickers.json` is NOT a complete list of filers. On
    11 September 2026 it held 10,407 entries and omitted AVB (AvalonBay, an
    S&P 500 REIT filing 10-Qs on schedule), WBS, SE, RMAX and LBRDK. Building
    the map from that file alone dropped every fact those companies filed.
  * `setdefault(cik, ticker)` kept one ticker per CIK, and the winner was
    routinely a symbol we do not cover: HLX lost to HOS, AREN to PAAI. The
    ingest wrote rows no page reads while the covered page rendered empty.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.backfill import canonical_of

PE = dt.date(2025, 12, 31)
FD = dt.date(2026, 2, 13)


# --- choosing the canonical symbol -------------------------------------------
def test_the_bare_symbol_beats_a_share_class_suffix():
    """`RDI.B` and `BRK-A` punctuate a class; the bare symbol is the common
    stock and the natural home for the filer's balance sheet."""
    assert canonical_of(["RDI", "RDIB"]) == "RDI"
    assert canonical_of(["BRK-A", "BRK"]) == "BRK"
    assert canonical_of(["RDI.B", "RDI"]) == "RDI"


def test_the_choice_is_stable_whatever_order_it_is_given():
    """A canonical ticker that moved between ingests would scatter one
    company's history across two symbols."""
    among = ["ATLQR", "JAB", "ATLQ", "JABRU", "ATLQW"]
    first = canonical_of(among)
    assert canonical_of(list(reversed(among))) == first
    assert canonical_of(sorted(among)) == first


def test_a_single_candidate_is_its_own_canonical():
    assert canonical_of(["AVB"]) == "AVB"


# --- the map itself, against a real database ---------------------------------
@pytest.fixture()
def db(tmp_path, monkeypatch):
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cik.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()
        reset_cache()


def _seed_sector_map(rows: list[tuple[str, str]]) -> None:
    from src.storage.db import session_scope
    from src.storage.models import SectorMap

    with session_scope() as s:
        for ticker, cik in rows:
            s.add(SectorMap(ticker=ticker, cik=cik, sector="Real Estate",
                            sector_source="sic"))


@pytest.mark.asyncio
async def test_cik_resolves_from_sector_map_when_sec_file_omits_it(db, monkeypatch):
    """AVB and WBS are in our sector_map and not in SEC's file. Before this
    fix both were unresolvable and every fact they filed was discarded."""
    from src import backfill

    async def sec_file_without_them():
        return [{"cik": 320193, "ticker": "AAPL"}]

    monkeypatch.setattr(backfill.sec_edgar, "fetch_company_tickers",
                        sec_file_without_them)
    _seed_sector_map([("AVB", "0000915912"), ("WBS", "0000801337")])

    mapping = await backfill._cik_to_ticker()

    assert mapping["915912"] == "AVB"
    assert mapping["801337"] == "WBS"
    # The SEC file still fills CIKs sector_map does not cover.
    assert mapping["320193"] == "AAPL"


@pytest.mark.asyncio
async def test_our_ticker_wins_a_cik_the_sec_file_assigns_elsewhere(db, monkeypatch):
    """HLX is ours; HOS is not. The old map handed CIK 866829 to HOS because
    SEC listed it first, so rows went to a symbol no page reads."""
    from src import backfill

    async def sec_prefers_hos():
        return [{"cik": 866829, "ticker": "HOS"}, {"cik": 866829, "ticker": "HLX"}]

    monkeypatch.setattr(backfill.sec_edgar, "fetch_company_tickers", sec_prefers_hos)
    _seed_sector_map([("HLX", "0000866829")])

    mapping = await backfill._cik_to_ticker()

    assert mapping["866829"] == "HLX", "our universe decides, not SEC's ordering"


@pytest.mark.asyncio
async def test_a_cik_is_never_mapped_to_two_tickers(db, monkeypatch):
    """One filer, one home for its rows. Storing it twice would make it count
    twice in every aggregate that groups by ticker."""
    from src import backfill

    async def empty():
        return []

    monkeypatch.setattr(backfill.sec_edgar, "fetch_company_tickers", empty)
    _seed_sector_map([("RDI", "0000716634"), ("RDIB", "0000716634")])

    mapping = await backfill._cik_to_ticker()

    assert mapping["716634"] == "RDI"
    assert list(mapping.values()).count("RDIB") == 0


# --- reading a sibling back ---------------------------------------------------
def _seed_balance_sheet(ticker: str) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker,
                               name=f"{ticker} Inc"))
        for metric, value in (("total_assets", 1_000e6),
                              ("total_liabilities", 600e6),
                              ("total_equity", 400e6)):
            s.add(Fundamental(ticker=ticker, metric=metric, value=value,
                              period_end=PE, fiscal_period="FY",
                              filing_date=FD, source="sec"))


def test_share_classes_both_resolve_to_the_same_balance_sheet(db):
    """RDI and RDIB are one filer with one CIK. Rows are stored once, under
    RDI, and BOTH symbols must return that same sheet -- the failure being
    fixed is /company/RDIB rendering empty while the data sits under RDI."""
    from src.company.balancesheet import get_balance_sheet

    _seed_sector_map([("RDI", "0000716634"), ("RDIB", "0000716634")])
    _seed_balance_sheet("RDI")

    primary = get_balance_sheet("RDI")
    sibling = get_balance_sheet("RDIB")

    assert primary is not None
    assert sibling is not None, "the sibling must not render an empty page"
    # Identical data, not merely both non-empty -- a guardrail against the
    # two symbols quietly diverging.
    assert sibling.assets["total_assets"].value == primary.assets["total_assets"].value
    assert sibling.period_end == primary.period_end


def test_a_ticker_with_no_sibling_is_returned_untouched(db):
    from src.company.lookup import canonical_ticker

    _seed_sector_map([("AVB", "0000915912")])
    assert canonical_ticker("AVB") == "AVB"
    assert canonical_ticker("avb") == "AVB"
    assert canonical_ticker("") == ""


def test_an_unknown_ticker_never_breaks_the_read_path(db):
    """Resolution is decoration. It must never be why a page fails."""
    from src.company.lookup import canonical_ticker

    assert canonical_ticker("NOSUCHTICKER") == "NOSUCHTICKER"


# --- the silence that hid all of this ----------------------------------------
def test_an_unresolvable_cik_is_logged_rather_than_dropped_silently(capsys):
    """AvalonBay lost its whole balance sheet to a counter nobody reads.

    Asserted against stdout rather than `caplog`: this app configures structlog
    with `PrintLogger`, which writes straight to stdout and never reaches the
    stdlib logging handlers pytest's `caplog` attaches to. A `caplog` assertion
    here passes vacuously whether or not anything is logged, which is the same
    class of mistake as the silence being fixed.
    """
    import pandas as pd

    from src.ingest.xbrl import extract_facts

    sub = pd.DataFrame([{"adsh": "X", "cik": "915912", "filed": "20260213",
                         "fp": "FY", "form": "10-K"}])
    num = pd.DataFrame([{"adsh": "X", "tag": "Assets", "ddate": "20251231",
                         "qtrs": "0", "uom": "USD", "value": 1000.0,
                         "coreg": "", "segments": ""}])

    rows, report = extract_facts(sub, num, {})
    out = capsys.readouterr().out

    assert not rows
    assert report.dropped_no_ticker >= 1
    assert "xbrl_cik_unresolved" in out
    assert "915912" in out, "the CIK must be named so it can be chased"


def test_one_unmapped_filer_logs_once_not_once_per_fact(capsys):
    """A filer with 900 facts must not produce 900 identical warnings, or the
    signal is lost in the noise it creates."""
    import pandas as pd

    from src.ingest.xbrl import extract_facts

    sub = pd.DataFrame([{"adsh": "X", "cik": "915912", "filed": "20260213",
                         "fp": "FY", "form": "10-K"}])
    num = pd.DataFrame([
        {"adsh": "X", "tag": t, "ddate": "20251231", "qtrs": "0", "uom": "USD",
         "value": 1000.0, "coreg": "", "segments": ""}
        for t in ("Assets", "Liabilities", "StockholdersEquity")
    ])

    extract_facts(sub, num, {})
    out = capsys.readouterr().out

    assert out.count("xbrl_cik_unresolved") == 1


# --- mezzanine tags found in real filings ------------------------------------
def test_redeemable_nci_at_fair_value_is_mapped():
    """CYH tags its redeemable NCI at FAIR VALUE, not carrying amount.

    Confirmed against CYH's 2026-03-31 companyfacts:
    `RedeemableNoncontrollingInterestEquityFairValue` is $260,000,000 and the
    gap our reconstruction left was $260,000,000 to the dollar. The three
    carrying-amount aliases already mapped do not reach it, so the filing read
    as an unexplained shortfall on our side.
    """
    from src.ingest.xbrl import CONCEPTS

    by_metric = {c.metric: c for c in CONCEPTS}
    tags = by_metric["redeemable_noncontrolling_interest"].tags
    assert "RedeemableNoncontrollingInterestEquityFairValue" in tags
    assert "RedeemableNoncontrollingInterestEquityOtherCarryingAmount" in tags
    # The carrying-amount alias must still lead: where a filer publishes both,
    # the carrying amount is the balance-sheet figure.
    assert tags[0] == "RedeemableNoncontrollingInterestEquityCarryingAmount"


def test_operating_partnership_units_are_mapped_and_readable():
    """UPREIT units held by outside partners sit outside permanent equity and
    `MinorityInterest` does not reach them."""
    from src.company.balancesheet import BALANCE_SHEET_CONCEPTS
    from src.ingest.xbrl import CONCEPTS

    by_metric = {c.metric: c for c in CONCEPTS}
    assert "minority_interest_operating_partnership" in by_metric
    assert by_metric["minority_interest_operating_partnership"].tags == (
        "MinorityInterestInOperatingPartnerships",
    )
    # Ingested is not enough -- a metric nothing reads is the bug that left
    # `redeemable_noncontrolling_interest` named but empty for a month.
    assert "minority_interest_operating_partnership" in BALANCE_SHEET_CONCEPTS


def test_every_mezzanine_metric_the_script_counts_is_actually_ingested():
    """The script's list and the ingest's concepts must not drift apart. This
    is the exact failure that made one category structurally empty."""
    from scripts.identity_failures import MEZZANINE_METRICS
    from src.ingest.xbrl import CONCEPTS

    produced = {c.metric for c in CONCEPTS}
    for metric in MEZZANINE_METRICS:
        assert metric in produced, f"{metric} is counted but never ingested"


def test_the_mezzanine_chain_reads_every_ingested_mezzanine_metric():
    """`view1._mezzanine` prefers rather than sums, so a metric missing from
    its chain is silently never consulted."""
    import inspect

    from scripts.identity_failures import MEZZANINE_METRICS
    from src.company import view1

    source = inspect.getsource(view1._mezzanine)
    for metric in MEZZANINE_METRICS:
        assert metric in source, f"{metric} is ingested but _mezzanine ignores it"
