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


def test_missing_liability_total_is_stated_not_guessed(db):
    _seed("NOL", {"total_assets": 1_000.0, "cash": 400.0, "total_equity": 400.0})
    v = build_view1("NOL")
    assert v.mode == "totals_only"
    assert any("total for liabilities" in n for n in v.notes)
    assert v.claims == []


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
