"""Cross-sectional math, fundamentals derivation, and the Stage 2 composite."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.config.factor_weights import CATEGORY_WEIGHTS, NEGATIVE_FACTORS
from src.factors import crosssection as xs
from src.factors.composite import score_composite
from src.factors.fundamentals import derive_quality, derive_value, piotroski_f_score


# ---------------------------------------------------------------------------
# Winsorization
# ---------------------------------------------------------------------------
def test_winsorize_kills_the_fake_40_sigma_outlier():
    """One bad datapoint must not be allowed to dominate the composite."""
    clean = pd.Series(np.random.default_rng(0).normal(0, 1, 500))
    dirty = pd.concat([clean, pd.Series([1e9])], ignore_index=True)

    z_dirty = xs.zscore(dirty)
    assert abs(z_dirty.iloc[-1]) > 20, "sanity: the raw outlier really is extreme"

    z_winsorized = xs.zscore(xs.winsorize(dirty))
    assert abs(z_winsorized.iloc[-1]) < 4, "winsorization failed to tame the outlier"


def test_winsorize_preserves_nan():
    s = pd.Series([1.0, np.nan, 3.0, 100.0])
    out = xs.winsorize(s)
    assert out.isna().sum() == 1


def test_winsorize_handles_degenerate_input():
    assert xs.winsorize(pd.Series([], dtype=float)).empty
    constant = pd.Series([5.0] * 10)
    assert (xs.winsorize(constant) == 5.0).all()


# ---------------------------------------------------------------------------
# Sector neutrality
# ---------------------------------------------------------------------------
def test_sector_zscore_removes_the_sector_effect():
    """Without sector-neutralising, the top-10 is just whichever sector is hot."""
    n = 40
    tickers = [f"T{i}" for i in range(2 * n)]
    # Energy is uniformly "cheap": every value is 10x the tech values.
    values = pd.Series(
        list(np.random.default_rng(1).normal(1, 0.1, n))
        + list(np.random.default_rng(2).normal(10, 1.0, n)),
        index=tickers,
    )
    sectors = pd.Series(["Technology"] * n + ["Energy"] * n, index=tickers)

    raw_top = values.nlargest(10).index
    assert all(t.startswith("T") and sectors[t] == "Energy" for t in raw_top), (
        "sanity: unneutralised ranking is entirely one sector"
    )

    z = xs.sector_zscore(values, sectors)
    top = z.nlargest(10).index
    n_energy = sum(sectors[t] == "Energy" for t in top)
    assert 2 <= n_energy <= 8, f"sector effect survived neutralisation ({n_energy}/10)"

    # Each sector's z-scores are centred on zero.
    for sec in ("Technology", "Energy"):
        assert abs(z[sectors == sec].mean()) < 0.2


def test_tiny_sectors_pool_into_the_universe():
    """A three-name sector has a meaningless stdev; it must not be z-scored alone."""
    tickers = [f"T{i}" for i in range(20)]
    values = pd.Series(np.arange(20, dtype=float), index=tickers)
    sectors = pd.Series(["Big"] * 17 + ["Tiny"] * 3, index=tickers)
    z = xs.sector_zscore(values, sectors, min_sector_size=8)
    assert z.notna().all()
    # The tiny sector's members were scored against the pooled residual, so
    # their spread is not artificially collapsed.
    assert z[sectors == "Tiny"].std(ddof=1) > 0


def test_unmapped_sectors_are_not_z_scored_as_their_own_bucket():
    """Names with no real sector (NaN) must fall into the universe-wide residual,
    NOT form an 'Unknown' peer group and get neutralized against each other.
    Here 8 NaN-sector names (values 1..8) are pooled with a high-value small
    sector; the top NaN name must NOT read as 'best in class' (which is what an
    isolated Unknown bucket would produce)."""
    idx = [f"U{i}" for i in range(8)] + [f"E{i}" for i in range(3)] + [f"T{i}" for i in range(10)]
    vals = pd.Series(
        [1, 2, 3, 4, 5, 6, 7, 8] + [100, 100, 100] + [0.0] * 10,
        index=idx, dtype=float,
    )
    secs = pd.Series([np.nan] * 8 + ["Energy"] * 3 + ["Tech"] * 10, index=idx)
    z = xs.sector_zscore(vals, secs, winsorize_first=False, min_sector_size=8)
    # Pooled with the high-value Energy names, U7 (value 8) sits below the pool
    # mean -> z < 0. In the old fake-Unknown-bucket behaviour it was top of its
    # own group -> z > 0. This asserts the fix.
    assert z["U7"] < 0
    assert z[["U0", "U7"]].notna().all()  # still scored, just against real peers


def test_missing_sector_label_still_scores():
    tickers = list("abcdefghij")
    values = pd.Series(np.arange(10, dtype=float), index=tickers)
    sectors = pd.Series([None] * 10, index=tickers)
    z = xs.sector_zscore(values, sectors)
    assert z.notna().all()


# ---------------------------------------------------------------------------
# Missing data handling
# ---------------------------------------------------------------------------
def test_missing_factor_scores_neutral_not_imputed():
    """A missing factor contributes z=0 (sector-neutral), never the mean."""
    z = pd.DataFrame(
        {"a": [1.0, np.nan, -1.0], "b": [2.0, 2.0, 2.0]}, index=list("xyz")
    )
    out = xs.weighted_category_score(z, {"a": 0.5, "b": 0.5})
    # y has only b=2.0 available, renormalised -> 2.0, not (0 + 2)/2 = 1.0
    assert out["y"] == pytest.approx(2.0)
    assert out["x"] == pytest.approx(1.5)


def test_coverage_tracks_data_completeness():
    """Completeness reflects what is actually usable after z-scoring."""
    n = 20
    idx = [f"T{i:02d}" for i in range(n)]
    f1 = np.arange(n, dtype=float)
    f1[0] = np.nan  # T00 is missing f1
    raw = pd.DataFrame(
        {"f1": f1, "f2": np.arange(n, dtype=float), "f3": np.full(n, np.nan)},
        index=idx,
    )
    sectors = pd.Series(["S"] * n, index=idx)
    _, coverage = xs.build_factor_zscores(raw, sectors, ["f1", "f2", "f3"])
    # f3 is universally absent, so nobody can exceed 2/3.
    assert coverage["T00"] == pytest.approx(1 / 3)
    assert coverage["T01"] == pytest.approx(2 / 3)


def test_factor_with_a_single_observation_is_not_scored():
    """One observation has no cross-section; z-scoring it would be a fiction."""
    raw = pd.DataFrame({"f1": [1.0, np.nan, np.nan]}, index=list("abc"))
    sectors = pd.Series(["S"] * 3, index=list("abc"))
    z, coverage = xs.build_factor_zscores(raw, sectors, ["f1"])
    assert z["f1"].isna().all()
    assert (coverage == 0).all()


def test_negative_factors_are_sign_flipped():
    """High accruals means earnings not backed by cash. Low is good."""
    assert "accruals" in NEGATIVE_FACTORS
    raw = pd.DataFrame({"accruals": [0.5, -0.5]}, index=["bad", "good"])
    sectors = pd.Series(["S", "S"], index=["bad", "good"])
    z, _ = xs.build_factor_zscores(raw, sectors, ["accruals"], NEGATIVE_FACTORS)
    assert z.loc["good", "accruals"] > z.loc["bad", "accruals"]


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
def test_category_weights_sum_to_one():
    assert sum(CATEGORY_WEIGHTS.values()) == pytest.approx(1.0)


def test_composite_ranks_and_is_reproducible():
    rng = np.random.default_rng(5)
    n = 100
    idx = [f"T{i:03d}" for i in range(n)]
    raw = pd.DataFrame(
        {
            "mom_12_1": rng.normal(0, 1, n),
            "mom_quality": rng.normal(0, 1, n),
            "mom_residual": rng.normal(0, 1, n),
            "gross_profitability": rng.normal(0, 1, n),
            "roic": rng.normal(0, 1, n),
            "accruals": rng.normal(0, 1, n),
            "fcf_yield": rng.normal(0, 1, n),
            "debt_trend": rng.normal(0, 1, n),
            "piotroski": rng.integers(0, 10, n).astype(float),
            "eps_rev_4w": rng.normal(0, 1, n),
            "rev_rev_4w": rng.normal(0, 1, n),
            "up_down_ratio_90d": rng.normal(0, 1, n),
            "reco_trend_delta": rng.normal(0, 1, n),
            "sue": rng.normal(0, 1, n),
            "pead_window": rng.uniform(0, 1, n),
            "earnings_gap": rng.normal(0, 1, n),
            "ev_ebit_inv": rng.normal(0, 1, n),
            "ev_sales_inv": rng.normal(0, 1, n),
            "fcf_price": rng.normal(0, 1, n),
            "sector": rng.choice(["Tech", "Energy", "Healthcare"], n),
        },
        index=idx,
    )
    r1 = score_composite(raw)
    r2 = score_composite(raw)
    assert list(r1.scores.index) == list(r2.scores.index)
    assert set(r1.scores.columns) >= {
        "factor_composite", "data_completeness", "rank", *CATEGORY_WEIGHTS
    }
    assert r1.scores["rank"].min() == 1
    assert r1.scores["factor_composite"].is_monotonic_decreasing


def test_composite_survives_an_all_missing_category():
    raw = pd.DataFrame(
        {"mom_12_1": [1.0, -1.0, 0.5], "sector": ["A", "A", "A"]},
        index=list("xyz"),
    )
    res = score_composite(raw)
    assert res.scores["factor_composite"].notna().all()
    assert (res.scores["data_completeness"] < 0.2).all()


# ---------------------------------------------------------------------------
# Fundamentals derivation
# ---------------------------------------------------------------------------
def _quarterly(**series) -> pd.DataFrame:
    idx = [dt.date(2024, 3, 31), dt.date(2024, 6, 30),
           dt.date(2024, 9, 30), dt.date(2024, 12, 31)]
    return pd.DataFrame(series, index=idx * (len(next(iter(series.values()))) // 4))


def test_gross_profitability_and_accruals():
    idx = pd.to_datetime(
        ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    ).date
    frame = pd.DataFrame(
        {
            "revenue": [100.0] * 4,
            "cogs": [60.0] * 4,
            "total_assets": [1000.0] * 4,
            "net_income": [10.0] * 4,
            "operating_cash_flow": [20.0] * 4,
        },
        index=idx,
    )
    q = derive_quality(frame)
    # (400 revenue - 240 cogs) / 1000 assets
    assert q["gross_profitability"] == pytest.approx(0.16)
    # (40 NI - 80 OCF) / 1000 -> negative, which is the GOOD direction
    assert q["accruals"] == pytest.approx(-0.04)


def test_fcf_derived_from_ocf_minus_capex_regardless_of_sign():
    idx = pd.to_datetime(
        ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
    ).date
    for capex in (-25.0, 25.0):  # FMP signs it negative, SEC positive
        frame = pd.DataFrame(
            {"operating_cash_flow": [100.0] * 4, "capex": [capex] * 4},
            index=idx,
        )
        assert derive_quality(frame)["fcf_ttm"] == pytest.approx(300.0)


def test_value_uses_yields_so_zero_earnings_does_not_explode():
    q = {"ebit_ttm": 0.0, "revenue_ttm": 500.0, "fcf_ttm": 50.0, "net_debt": 100.0}
    v = derive_value(q, market_cap=1000.0, price=10.0)
    assert v["ev_ebit_inv"] == 0.0  # defined, not infinite
    assert v["enterprise_value"] == pytest.approx(1100.0)
    assert v["fcf_price"] == pytest.approx(0.05)


def test_piotroski_returns_nan_when_too_few_signals():
    frame = pd.DataFrame({"revenue": [100.0]}, index=[dt.date(2024, 3, 31)])
    assert np.isnan(piotroski_f_score(frame))


def test_piotroski_in_range_for_a_healthy_company():
    idx = [dt.date(2023, 3, 31), dt.date(2023, 6, 30), dt.date(2023, 9, 30),
           dt.date(2023, 12, 31), dt.date(2024, 3, 31), dt.date(2024, 6, 30),
           dt.date(2024, 9, 30), dt.date(2024, 12, 31)]
    frame = pd.DataFrame(
        {
            "net_income": [10, 11, 12, 13, 15, 16, 17, 18],
            "operating_cash_flow": [20, 21, 22, 23, 26, 27, 28, 29],
            "total_assets": [500] * 8,
            "long_term_debt": [100, 100, 100, 100, 90, 90, 90, 90],
            "current_assets": [200] * 8,
            "current_liabilities": [100, 100, 100, 100, 90, 90, 90, 90],
            "shares_diluted": [50] * 8,
            "revenue": [100, 102, 104, 106, 115, 117, 119, 121],
            "gross_profit": [40, 41, 42, 43, 50, 51, 52, 53],
        },
        index=idx,
    ).astype(float)
    score = piotroski_f_score(frame)
    assert 0 <= score <= 9
    assert score >= 6, f"a clearly improving company scored only {score}"


# ---------------------------------------------------------------------------
# PEAD windowing (get_last_earnings) -- the spec's explicit time-decay
# ---------------------------------------------------------------------------
def test_pead_computes_sue_and_decays_with_time(session):
    """SUE = (actual - consensus)/stdev(surprises); the drift weight decays
    linearly to zero over 60 days. A stale surprise must be discounted."""
    import datetime as dt

    import numpy as np

    from src.storage.models import EarningsEvent
    from src.storage.pit import get_last_earnings

    as_of = dt.date(2025, 6, 2)
    # Five reports for FRESH, the last one 10 trading-ish days ago.
    actuals = [1.0, 1.1, 0.9, 1.2, 1.3]  # consensus is 1.0 throughout
    for k, a in enumerate(actuals):
        # oldest first; newest (k=4) is 10 days before as_of
        report = as_of - dt.timedelta(days=10 + (len(actuals) - 1 - k) * 90)
        session.add(EarningsEvent(ticker="FRESH", report_date=report,
                                  actual_eps=a, consensus_eps=1.0,
                                  gap_pct=0.03))
    # STALE: identical surprise history but the last report is 90 days ago.
    for k, a in enumerate(actuals):
        report = as_of - dt.timedelta(days=90 + (len(actuals) - 1 - k) * 90)
        session.add(EarningsEvent(ticker="STALE", report_date=report,
                                  actual_eps=a, consensus_eps=1.0, gap_pct=0.03))
    # THIN: too few reports to estimate a surprise stdev.
    for k in range(2):
        session.add(EarningsEvent(ticker="THIN",
                                  report_date=as_of - dt.timedelta(days=10 + k * 90),
                                  actual_eps=1.2, consensus_eps=1.0, gap_pct=0.0))
    session.flush()

    out = get_last_earnings(session, ["FRESH", "STALE", "THIN"], as_of).set_index("ticker")

    surprises = np.array(actuals) - 1.0
    expected_sd = surprises.std(ddof=1)
    expected_sue = (actuals[-1] - 1.0) / expected_sd

    # FRESH: SUE matches the hand-computed value, window ~1 - 10/60.
    assert out.at["FRESH", "sue"] == pytest.approx(expected_sue, rel=1e-6)
    assert out.at["FRESH", "pead_window"] == pytest.approx(1 - 10 / 60, abs=0.02)
    assert out.at["FRESH", "days_since_earnings"] == 10

    # STALE: same SUE, but the drift window has fully decayed to zero.
    assert out.at["STALE", "sue"] == pytest.approx(expected_sue, rel=1e-6)
    assert out.at["STALE", "pead_window"] == 0.0

    # THIN: cannot form a stdev from two points -> SUE is NaN, not a fake number.
    assert np.isnan(out.at["THIN", "sue"])


def test_pead_window_zeroes_out_stale_sue_in_the_composite(session):
    """End-to-end: after the window multiply, a stale surprise contributes no
    PEAD signal even though its raw SUE is large."""
    import datetime as dt

    import numpy as np
    import pandas as pd

    from src.factors.composite import assemble_raw_factors
    from src.storage.models import EarningsEvent

    as_of = dt.date(2025, 6, 2)
    for k, a in enumerate([1.0, 1.1, 0.9, 1.5]):
        session.add(EarningsEvent(ticker="OLD",
                                  report_date=as_of - dt.timedelta(days=200 + (3 - k) * 90),
                                  actual_eps=a, consensus_eps=1.0, gap_pct=0.05))
    session.flush()

    trend = pd.DataFrame(
        {"close": [50.0], "mom_12_1": [0.1], "mom_quality": [0.5],
         "mom_residual": [0.0]},
        index=pd.Index(["OLD"], name="ticker"),
    )
    universe = pd.DataFrame(
        {"ticker": ["OLD"], "sector": ["Technology"], "market_cap": [1e9]}
    )
    raw = assemble_raw_factors(session, trend, universe, as_of)
    # 200+ days since the print: window is 0, so decayed SUE is 0 regardless of
    # how large the raw surprise was.
    assert raw.at["OLD", "sue"] == 0.0 or np.isnan(raw.at["OLD", "sue"])
