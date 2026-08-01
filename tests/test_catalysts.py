"""Stage 3 scoring: SEC events, news themes, macro regime, risk screen."""

from __future__ import annotations

import datetime as dt

import pytest

from src.catalysts.macro import classify_regime
from src.catalysts.news import score_news
from src.catalysts.risk_screen import estimate_spread_bps, screen_name
from src.catalysts.sec_events import score_filings, score_insiders

AS_OF = dt.date(2025, 6, 2)


def _buy(person, role, days_ago=5):
    return {
        "person": person, "role": role, "code": "P", "shares": 1000,
        "price": 50.0, "value_usd": 50_000,
        "date": AS_OF - dt.timedelta(days=days_ago),
    }


# ---------------------------------------------------------------------------
# Insiders
# ---------------------------------------------------------------------------
def test_cluster_buy_is_the_strongest_insider_signal():
    cluster = [_buy("A", "director"), _buy("B", "director"), _buy("C", "cfo")]
    single = [_buy("A", "cfo")]

    cluster_pts, detail = score_insiders(cluster, AS_OF)
    single_pts, _ = score_insiders(single, AS_OF)

    assert detail["cluster"] is True
    assert cluster_pts > single_pts
    assert detail["distinct_buyers"] == 3
    assert "CLUSTER" in detail["summary"]


def test_ceo_cfo_buys_outweigh_directors():
    ceo, _ = score_insiders([_buy("A", "ceo")], AS_OF)
    director, _ = score_insiders([_buy("A", "director")], AS_OF)
    assert ceo > director


def test_sales_are_discounted_heavily_versus_buys():
    """Sales are mostly scheduled 10b5-1 and carry little information."""
    sell = {**_buy("A", "ceo"), "code": "S"}
    buy_pts, _ = score_insiders([_buy("A", "ceo")], AS_OF)
    sell_pts, _ = score_insiders([sell], AS_OF)
    assert abs(sell_pts) < abs(buy_pts) / 4
    assert sell_pts < 0


def test_buys_outside_the_30_day_window_do_not_form_a_cluster():
    old = [_buy(p, "director", days_ago=60) for p in "ABC"]
    pts, detail = score_insiders(old, AS_OF)
    assert detail["cluster"] is False
    assert pts == 0.0


def test_empty_insider_history_scores_zero():
    pts, detail = score_insiders([], AS_OF)
    assert pts == 0.0
    assert detail["buys"] == 0


# ---------------------------------------------------------------------------
# Filings
# ---------------------------------------------------------------------------
def test_shelf_registration_is_strongly_negative():
    pts, detail = score_filings(
        [{"form": "424B5", "filing_date": AS_OF, "items": None}]
    )
    assert pts < -2
    assert detail["has_dilution_filing"] is True


def test_activist_stake_is_strongly_positive():
    pts, _ = score_filings([{"form": "SC 13D", "filing_date": AS_OF, "items": None}])
    assert pts > 2


def test_8k_202_results_is_neutral_because_pead_scores_it():
    pts, _ = score_filings([{"form": "8-K", "items": "2.02", "filing_date": AS_OF}])
    assert pts == 0.0


def test_8k_101_material_agreement_is_positive():
    pts, detail = score_filings(
        [{"form": "8-K", "items": "1.01", "filing_date": AS_OF}]
    )
    assert pts > 0
    assert "1.01" in detail["flags"][0]


def test_cfo_departure_is_worse_than_a_generic_officer_change():
    cfo, _ = score_filings(
        [{"form": "8-K", "items": "5.02", "filing_date": AS_OF,
          "detail": {"note": "Chief Financial Officer resigned"}}]
    )
    other, _ = score_filings(
        [{"form": "8-K", "items": "5.02", "filing_date": AS_OF, "detail": {}}]
    )
    assert cfo < other < 0


# ---------------------------------------------------------------------------
# News themes -- the layoff distinction
# ---------------------------------------------------------------------------
def test_company_layoffs_read_positive_but_sector_layoffs_read_negative():
    """Layoffs at the company are a margin story. Sector-wide is a demand story."""
    company = score_news(
        {"themes": {"layoff": {"total": 10, "company": 8, "context": 2}}}
    )
    sector = score_news(
        {"themes": {"layoff": {"total": 10, "company": 1, "context": 9}}}
    )
    assert company[0] > 0
    assert sector[0] < 0
    assert "LAYOFF_COMPANY" in company[1]["themes"]
    assert "LAYOFF_SECTOR" in sector[1]["themes"]


def test_bankruptcy_theme_is_heavily_negative():
    pts, _ = score_news(
        {"themes": {"bankruptcy": {"total": 3, "company": 3, "context": 0}}}
    )
    assert pts < -3


def test_volume_spike_scores_regardless_of_direction():
    pts, detail = score_news({"volume_z": 2.5})
    assert pts > 0
    assert detail["volume_z"] == 2.5


def test_tone_slope_not_level_drives_the_score():
    """Improving-from-negative must beat deteriorating-from-positive."""
    improving = score_news({"tone_avg": -2.0, "tone_slope_7d": 0.4})
    deteriorating = score_news({"tone_avg": 3.0, "tone_slope_7d": -0.4})
    assert improving[0] > deteriorating[0]


def test_source_diversity_separates_a_story_from_a_pr_cycle():
    broad = score_news({"source_diversity": 0.85})
    narrow = score_news({"source_diversity": 0.06})
    assert broad[0] > narrow[0]


def test_empty_news_scores_zero():
    assert score_news({})[0] == 0.0


# ---------------------------------------------------------------------------
# Macro regime
# ---------------------------------------------------------------------------
def _series(**latest):
    out = {}
    for k, v in latest.items():
        out[k] = [
            {"date": dt.date(2025, 1, 1) + dt.timedelta(days=i), "value": val}
            for i, val in enumerate(v if isinstance(v, list) else [v] * 30)
        ]
    return out


def test_wide_and_widening_credit_spreads_produce_risk_off():
    series = _series(
        BAMLH0A0HYM2=[3.0] * 10 + [6.0] * 20,  # blown out, still widening
        T10Y2Y=-0.5, NFCI=0.6, VIXCLS=35.0,
        ICSA=[220000] * 10 + [280000] * 20,
    )
    state = classify_regime(series)
    assert state.regime == "RISK_OFF"
    assert state.score < 0
    # Fewer names ship in a risk-off tape.
    assert state.final_count() == 5


def test_benign_conditions_produce_risk_on():
    series = _series(
        BAMLH0A0HYM2=[3.4] * 30, T10Y2Y=1.4, NFCI=-0.5, VIXCLS=13.0,
        ICSA=[230000] * 30,
    )
    state = classify_regime(series)
    assert state.regime == "RISK_ON"
    assert state.final_count() == 10


def test_regime_tilts_sectors_but_returns_neutral_for_unknown():
    series = _series(BAMLH0A0HYM2=[3.0] * 30, T10Y2Y=1.5, NFCI=-0.5, VIXCLS=12.0)
    state = classify_regime(series)
    assert state.sector_tilt("Technology") > 1.0
    assert state.sector_tilt("Utilities") < 1.0
    assert state.sector_tilt(None) == 1.0
    assert 0.0 <= state.regime_fit("Technology") <= 1.0


def test_missing_macro_series_still_classifies():
    state = classify_regime({})
    assert state.regime in {"RISK_ON", "RISK_OFF", "LATE_CYCLE", "RECOVERY"}


# ---------------------------------------------------------------------------
# Risk screen
# ---------------------------------------------------------------------------
def test_crowded_short_requires_both_conditions():
    """High SI alone is not a reject; high SI AND high days-to-cover is."""
    both = screen_name(
        "X", short_interest_pct=25, days_to_cover=8, next_earnings=None,
        as_of=AS_OF, realised_vol_20d=0.3, universe_median_vol=0.3,
        spread_bps=10,
    )
    si_only = screen_name(
        "X", short_interest_pct=25, days_to_cover=1.5, next_earnings=None,
        as_of=AS_OF, realised_vol_20d=0.3, universe_median_vol=0.3,
        spread_bps=10,
    )
    assert not both.passed
    assert si_only.passed


def test_imminent_earnings_is_a_reject():
    soon = screen_name(
        "X", short_interest_pct=2, days_to_cover=1, as_of=AS_OF,
        next_earnings=AS_OF + dt.timedelta(days=2),
        realised_vol_20d=0.3, universe_median_vol=0.3, spread_bps=10,
    )
    far = screen_name(
        "X", short_interest_pct=2, days_to_cover=1, as_of=AS_OF,
        next_earnings=AS_OF + dt.timedelta(days=30),
        realised_vol_20d=0.3, universe_median_vol=0.3, spread_bps=10,
    )
    assert not soon.passed
    assert "earnings" in soon.reasons[0]
    assert far.passed


def test_missing_data_never_rejects():
    """'Unknown short interest' is not evidence of crowding."""
    res = screen_name(
        "X", short_interest_pct=None, days_to_cover=None, next_earnings=None,
        as_of=AS_OF, realised_vol_20d=None, universe_median_vol=None,
        spread_bps=None,
    )
    assert res.passed
    assert res.reasons == []


def test_extreme_vol_is_a_reject():
    res = screen_name(
        "X", short_interest_pct=1, days_to_cover=1, next_earnings=None,
        as_of=AS_OF, realised_vol_20d=1.5, universe_median_vol=0.3,
        spread_bps=10,
    )
    assert not res.passed


def test_spread_estimate_falls_with_liquidity():
    thin = estimate_spread_bps(20.0, 21.0, 19.0, 2_000_000)
    thick = estimate_spread_bps(20.0, 21.0, 19.0, 200_000_000)
    assert thin is not None and thick is not None
    assert thin > thick
    assert thick < 10
    assert estimate_spread_bps(20.0, 21.0, 19.0, None) is None


def test_spread_estimate_respects_the_tick_floor():
    """A $3 stock cannot have a 1bp spread; the tick size dominates."""
    # One cent on a $3 stock is 33bps, however much volume trades.
    tick_floor_bps = 10_000 * 0.01 / 3.0
    cheap = estimate_spread_bps(3.0, 3.1, 2.9, 500_000_000)
    assert cheap == pytest.approx(tick_floor_bps, rel=0.01)
    assert cheap > 30
    # The same liquidity on a $300 stock is not floored.
    assert estimate_spread_bps(300.0, 305.0, 295.0, 500_000_000) < tick_floor_bps
