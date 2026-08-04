"""Views 2 and 3: the revenue flow, and the company against national GDP."""

from __future__ import annotations

import datetime as dt

import pytest

from src.company.view2 import build_view2, gdp_table
from src.company.view3 import build_view3, describe_flow

FY = dt.date(2025, 12, 31)
FILED = dt.date(2026, 2, 13)
QUARTERS = [dt.date(2025, 12, 31), dt.date(2025, 9, 30),
            dt.date(2025, 6, 30), dt.date(2025, 3, 31)]


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'v23.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed(ticker, metrics, period=FY, fp="FY"):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        for metric, value in metrics.items():
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=float(value),
                period_end=period, fiscal_period=fp, filing_date=FILED,
                source="sec", restated=False,
            ))


SOFTWARE = {
    "revenue": 1_000.0, "cogs": 320.0, "gross_profit": 680.0,
    "operating_income": 450.0, "income_tax": 80.0, "net_income": 361.0,
}
RETAIL = {
    "revenue": 1_000.0, "cogs": 750.0, "gross_profit": 250.0,
    "operating_income": 43.0, "income_tax": 9.0, "net_income": 29.0,
}


# ------------------------------------------------------------------- view 3
def test_a_software_and_a_retail_flow_are_visibly_different(db):
    _seed("SOFT", SOFTWARE)
    _seed("SHOP", RETAIL)
    soft, shop = build_view3("SOFT"), build_view3("SHOP")

    assert soft.margin_pct == pytest.approx(36.1, abs=0.1)
    assert shop.margin_pct == pytest.approx(2.9, abs=0.1)
    soft_cogs = next(s for s in soft.stages if s.key == "cogs")
    shop_cogs = next(s for s in shop.stages if s.key == "cogs")
    assert soft_cogs.pct == pytest.approx(32.0)
    assert shop_cogs.pct == pytest.approx(75.0)


def test_stages_and_profit_account_for_all_revenue(db):
    _seed("SOFT", SOFTWARE)
    v = build_view3("SOFT")
    assert sum(s.value for s in v.stages) + v.net_income == pytest.approx(v.revenue)


def test_no_revenue_means_no_view(db):
    _seed("NOREV", {"net_income": 100.0})
    assert build_view3("NOREV") is None


def test_no_net_income_means_no_view(db):
    _seed("NONI", {"revenue": 1_000.0, "cogs": 400.0})
    assert build_view3("NONI") is None


def test_a_dominant_remainder_skips_the_view(db):
    """A bank reports no cost of sales because it has none.

    Drawing revenue -> one giant "everything else" -> profit would invite the
    reader to infer a breakdown that is not in the filing, so it is skipped.
    """
    _seed("BANKY", {
        "revenue": 1_000.0, "income_tax": 90.0, "net_income": 330.0,
    })
    assert build_view3("BANKY") is None


def test_cost_of_sales_is_derived_from_stated_gross_profit(db):
    """Revenue minus stated gross profit is exact arithmetic, not an estimate."""
    _seed("DER", {
        "revenue": 1_000.0, "gross_profit": 680.0,
        "operating_income": 450.0, "income_tax": 80.0, "net_income": 361.0,
    })
    v = build_view3("DER")
    cogs = next(s for s in v.stages if s.key == "cogs")
    assert cogs.value == pytest.approx(320.0)
    assert cogs.derived is True


def test_a_loss_is_carried_through_not_flattened(db):
    _seed("LOSS", {
        "revenue": 1_000.0, "cogs": 700.0, "gross_profit": 300.0,
        "operating_income": -50.0, "income_tax": 0.0, "net_income": -120.0,
    })
    v = build_view3("LOSS")
    assert v.loss_making is True
    assert v.margin_pct < 0
    assert any("loss" in s.lower() for s in describe_flow(v))


def test_quarterly_filings_are_summed_to_a_full_year(db):
    """A single quarter presented as a year overstates by ~4x."""
    for q in QUARTERS:
        _seed("Q", {"revenue": 250.0, "cogs": 80.0, "gross_profit": 170.0,
                    "operating_income": 112.0, "income_tax": 20.0,
                    "net_income": 90.0}, period=q, fp="Q1")
    v = build_view3("Q")
    assert v.period_basis == "ttm"
    assert v.periods_used == 4
    assert v.revenue == pytest.approx(1_000.0)


def test_fewer_than_four_quarters_is_not_a_year(db):
    for q in QUARTERS[:3]:
        _seed("PART", {"revenue": 250.0, "cogs": 80.0, "net_income": 90.0},
              period=q, fp="Q1")
    assert build_view3("PART") is None


def test_flow_description_is_descriptive_not_a_judgement(db):
    _seed("SOFT", SOFTWARE)
    text = " ".join(describe_flow(build_view3("SOFT"))).lower()
    for word in ("good", "bad", "strong", "weak", "healthy", "buy", "sell",
                 "attractive", "should", "impressive"):
        assert word not in text, f"{word!r} is judgement, not description"


# ------------------------------------------------------------------- view 2
def test_gdp_table_is_committed_and_sorted():
    t = gdp_table()
    assert t["countries"], "the GDP file must be committed to the repo"
    values = [c["gdp_usd"] for c in t["countries"]]
    assert values == sorted(values, reverse=True)
    assert t["year"] and t["source"]


def test_company_is_placed_among_its_nearest_economies():
    v = build_view2("WMT", 680_900_000_000, "annual", FY, company_name="Walmart")
    kinds = [r.kind for r in v.rows]
    assert kinds.count("company") == 1
    # Sorted by size, so the company sits in the middle of its neighbours.
    values = [r.value for r in v.rows]
    assert values == sorted(values, reverse=True)
    me = next(r for r in v.rows if r.kind == "company")
    assert 0 < v.rows.index(me) < len(v.rows) - 1


def test_bars_are_shares_of_the_largest_row():
    v = build_view2("X", 500_000_000_000, "annual", FY)
    assert max(r.pct_of_max for r in v.rows) == pytest.approx(100.0)


def test_zero_or_negative_revenue_has_no_scale_view():
    assert build_view2("X", 0, "annual", FY) is None
    assert build_view2("X", -5, "annual", FY) is None


def test_a_company_larger_than_every_listed_economy_is_handled():
    v = build_view2("HUGE", 99_000_000_000_000, "annual", FY)
    assert v is not None
    assert v.rows[0].kind == "company"
    assert "larger than every economy" in v.rank_note
