"""Whole-universe validation: identity distribution, scale jumps, sectors.

Five companies catch a parser reading the wrong fact. These catch whether the
parse is right across the file -- and in particular whether a failure is
concentrated in one class of filer, which is a structural tag problem rather
than noise.
"""

from __future__ import annotations

import datetime as dt

import pytest

from src.company.universe_check import run_universe_check

Q4 = dt.date(2025, 12, 31)
Q3 = dt.date(2025, 9, 30)
FILED = dt.date(2026, 2, 13)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'u.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed(rows: list[tuple], sectors: dict[str, str] | None = None) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap

    with session_scope() as s:
        for ticker, metric, value, period_end in rows:
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=period_end,
                fiscal_period="FY", filing_date=FILED, source="sec", restated=False,
            ))
        for ticker, sector in (sectors or {}).items():
            s.add(SectorMap(ticker=ticker, sic="6021", sector=sector,
                            sector_source="sic"))


def _balanced(ticker: str, assets: float, period=Q4) -> list[tuple]:
    return [
        (ticker, "total_assets", assets, period),
        (ticker, "total_liabilities", assets * 0.7, period),
        (ticker, "total_equity", assets * 0.3, period),
    ]


def test_a_balanced_company_lands_in_the_within_1pct_bucket(db):
    _seed(_balanced("AAA", 1_000.0))
    r = run_universe_check()
    assert r["identity"]["checkable"] == 1
    assert r["identity"]["buckets"]["within_1pct"] == 1
    assert r["identity"]["pass_rate_pct"] == 100.0
    assert r["worst"] == []


def test_drift_lands_in_the_right_bucket(db):
    # 1000 assets vs 700 + 230 = 930 -> 7% drift
    _seed([
        ("MID", "total_assets", 1_000.0, Q4),
        ("MID", "total_liabilities", 700.0, Q4),
        ("MID", "total_equity", 230.0, Q4),
    ])
    r = run_universe_check()
    assert r["identity"]["buckets"]["5_to_10pct"] == 1
    assert r["worst"][0]["ticker"] == "MID"
    assert r["worst"][0]["drift_pct"] == 7.0


def test_a_company_missing_a_total_is_not_checkable_not_a_failure(db):
    """No reported total liabilities means the identity cannot be evaluated.

    Counting that as a failure would invent a problem; counting it as a pass
    would hide one. It is its own category.
    """
    _seed([
        ("THIN", "total_assets", 1_000.0, Q4),
        ("THIN", "total_equity", 300.0, Q4),
    ])
    r = run_universe_check()
    assert r["identity"]["checkable"] == 0
    assert r["identity"]["not_checkable"] == 1


# ------------------------------------------------------ noncontrolling interests
def test_nci_gap_is_identified_not_left_as_mystery_drift(db):
    """The LNG/APD/CMTL pattern: parent-only equity vs consolidated assets.

    Assets include the whole of a partly-owned subsidiary; StockholdersEquity
    excludes the outside investors' share. The gap IS the NCI, and saying so
    stops it being re-investigated as a parser bug.
    """
    _seed([
        ("NCICO", "total_assets", 1_000.0, Q4),
        ("NCICO", "total_liabilities", 700.0, Q4),
        ("NCICO", "total_equity", 250.0, Q4),        # parent only
        ("NCICO", "minority_interest", 50.0, Q4),    # the missing 5%
    ])
    r = run_universe_check()
    w = r["worst"][0]
    assert w["ticker"] == "NCICO"
    assert w["drift_pct"] == 5.0
    assert w["explained_by"] == "noncontrolling_interest"
    assert w["drift_pct_with_nci"] == 0.0
    assert r["identity"]["explained_by_nci"] == 1


def test_nci_inclusive_equity_is_preferred_and_balances(db):
    """When the filer reports the NCI-inclusive total, use it: no drift at all."""
    _seed([
        ("NCICO", "total_assets", 1_000.0, Q4),
        ("NCICO", "total_liabilities", 700.0, Q4),
        ("NCICO", "total_equity", 250.0, Q4),
        ("NCICO", "total_equity_incl_nci", 300.0, Q4),
    ])
    r = run_universe_check()
    assert r["identity"]["buckets"]["within_1pct"] == 1
    assert r["identity"]["equity_basis_incl_nci"] == 1
    assert r["worst"] == []


# ------------------------------------------------------------------ scale jumps
def test_assets_moving_more_than_10x_between_quarters_is_flagged(db):
    """No per-period check can see this: each quarter is self-consistent."""
    _seed(_balanced("SCALE", 1_000.0, Q3) + _balanced("SCALE", 1_000_000.0, Q4))
    r = run_universe_check()
    assert r["scale_jumps_total"] == 1
    j = r["scale_jumps"][0]
    assert j["ticker"] == "SCALE"
    assert j["ratio"] == 1000.0
    # Both periods balance internally, so the identity check sees nothing wrong.
    assert r["identity"]["buckets"]["within_1pct"] == 1


def test_a_downward_scale_error_is_flagged_too(db):
    _seed(_balanced("DROP", 1_000_000.0, Q3) + _balanced("DROP", 1_000.0, Q4))
    r = run_universe_check()
    assert r["scale_jumps_total"] == 1
    assert r["scale_jumps"][0]["ratio"] == 0.0 or r["scale_jumps"][0]["ratio"] < 0.01


def test_ordinary_growth_is_not_flagged(db):
    _seed(_balanced("GROW", 1_000.0, Q3) + _balanced("GROW", 1_400.0, Q4))
    r = run_universe_check()
    assert r["scale_jumps_total"] == 0


# ---------------------------------------------------------------------- sectors
def test_a_systematically_failing_sector_is_called_structural(db):
    """Random noise spreads across sectors; a tag problem concentrates in one."""
    rows: list[tuple] = []
    sectors: dict[str, str] = {}
    # Financials: every one off by 20% -- the shape of a wrong equity tag.
    for i in range(10):
        t = f"BANK{i}"
        rows += [
            (t, "total_assets", 1_000.0, Q4),
            (t, "total_liabilities", 700.0, Q4),
            (t, "total_equity", 100.0, Q4),
        ]
        sectors[t] = "Financials"
    # Technology: all clean.
    for i in range(10):
        t = f"TECH{i}"
        rows += _balanced(t, 1_000.0)
        sectors[t] = "Technology"
    _seed(rows, sectors)

    r = run_universe_check()
    by_sector = {s["sector"]: s for s in r["by_sector"]}

    assert by_sector["Financials"]["pass_rate_pct"] == 0.0
    assert "STRUCTURAL" in by_sector["Financials"]["verdict"]
    assert by_sector["Technology"]["pass_rate_pct"] == 100.0
    assert by_sector["Technology"]["verdict"] == "ok"
    # Worst-first ordering puts the broken sector at the top.
    assert r["by_sector"][0]["sector"] == "Financials"


def test_tickers_without_a_sector_are_grouped_not_dropped(db):
    _seed(_balanced("NOSECTOR", 1_000.0))
    r = run_universe_check()
    assert {s["sector"] for s in r["by_sector"]} == {"unknown"}
    assert r["identity"]["checkable"] == 1


def test_worst_list_is_capped_and_totalled(db):
    from src.company.universe_check import WORST_N

    rows: list[tuple] = []
    for i in range(WORST_N + 20):
        t = f"BAD{i:03d}"
        rows += [
            (t, "total_assets", 1_000.0, Q4),
            (t, "total_liabilities", 700.0, Q4),
            (t, "total_equity", 100.0, Q4),
        ]
    _seed(rows)
    r = run_universe_check()
    assert r["worst_total"] == WORST_N + 20
    assert len(r["worst"]) == WORST_N


# ------------------------------------------------- the filer's own stated total
def test_stated_total_is_preferred_over_reconstructing_l_plus_e(db):
    """LiabilitiesAndStockholdersEquity is the filer's own right-hand side.

    Thousands report it directly. Where present it beats our reconstruction,
    which can only ever be as good as our choice of which equity tag to add.
    """
    _seed([
        ("STATED", "total_assets", 1_000.0, Q4),
        # A reconstruction from these would read 7% off...
        ("STATED", "total_liabilities", 700.0, Q4),
        ("STATED", "total_equity", 230.0, Q4),
        # ...but the filer states the total, and it balances.
        ("STATED", "liabilities_and_equity", 1_000.0, Q4),
    ])
    r = run_universe_check()

    assert r["identity"]["buckets"]["within_1pct"] == 1
    assert r["identity"]["basis_counts"]["liabilities_and_equity"] == 1
    st = r["stated_total"]
    assert st["used"] == 1
    assert st["comparable"] == 1
    assert st["moved_into_1pct"] == 1
    # And it reports what the reconstruction WOULD have scored, so the value of
    # the mapping is measured rather than asserted.
    assert st["buckets_if_reconstructed"]["5_to_10pct"] == 1


def test_stated_total_absent_falls_back_to_reconstruction(db):
    _seed(_balanced("NOSTATED", 1_000.0))
    r = run_universe_check()
    assert r["identity"]["basis_counts"]["total_equity"] == 1
    assert r["stated_total"]["used"] == 0
    assert r["stated_total"]["comparable"] == 0


# ------------------------------------------------------------ size of failures
def test_over_10pct_bucket_is_broken_down_by_company_size(db):
    """A shell company failing by 70,000% on $17k of assets is noise.

    Reporting the size profile separates "microcap garbage" from "the parser is
    wrong about real companies", which the raw count cannot distinguish.
    """
    rows: list[tuple] = []
    # Three tiny shells, wildly off.
    for i, assets in enumerate((17_000.0, 250_000.0, 4_000_000.0)):
        t = f"SHELL{i}"
        rows += [
            (t, "total_assets", assets, Q4),
            (t, "total_liabilities", assets * 50, Q4),
            (t, "total_equity", 0.0, Q4),
        ]
    # One real company, also off.
    rows += [
        ("BIGCO", "total_assets", 5_000_000_000.0, Q4),
        ("BIGCO", "total_liabilities", 3_000_000_000.0, Q4),
        ("BIGCO", "total_equity", 500_000_000.0, Q4),
    ]
    _seed(rows)
    r = run_universe_check()

    assert r["identity"]["buckets"]["over_10pct"] == 4
    size = r["over_10pct_by_size"]
    assert size["buckets"]["under_1m"] == 2
    assert size["buckets"]["1m_to_10m"] == 1
    assert size["buckets"]["over_100m"] == 1
    assert size["under_10m"] == 3
    assert size["under_10m_pct"] == 75.0

    # The individual entries carry the band too, so the worst list is readable.
    big = next(w for w in r["worst"] if w["ticker"] == "BIGCO")
    assert big["microcap"] is False
    assert big["size_band"] == "over_100m"
