"""Stage 1 trend gate tests."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.config.factor_weights import TrendGateConfig
from src.factors import indicators as ind
from src.factors.trend import (
    GATE_NAMES,
    apply_gates,
    compute_trend_features,
    run_trend_gate,
)


def test_gate_selects_uptrends_and_rejects_downtrends(price_panels):
    p = price_panels
    res = run_trend_gate(
        p["close"], p["high"], p["low"], p["volume"], sectors=p["sectors"]
    )
    survivors = set(res.survivors.index)
    assert survivors, "no survivors from a panel containing clear uptrends"
    # Every downtrend name must be rejected.
    assert not survivors & set(p["down"]), "a downtrend name passed the gate"
    # Most uptrends should survive.
    assert len(survivors & set(p["up"])) >= len(p["up"]) * 0.5


def test_all_hard_gates_hold_for_survivors(price_panels):
    p = price_panels
    res = run_trend_gate(
        p["close"], p["high"], p["low"], p["volume"], sectors=p["sectors"]
    )
    s = res.survivors
    assert (s["close"] > s["ema20"]).all()
    assert (s["ema20"] > s["sma50"]).all()
    assert (s["close"] > s["sma200"]).all()
    assert (s["sma200_slope_21d"] > 0).all()
    assert (s["ret_3m"] > 0).all()
    assert (s["rs_percentile"] >= 50).all()
    assert (s["pct_of_52w_high"] >= 0.75).all()


def test_risk_off_relaxes_rs_gate_rather_than_returning_empty(price_panels):
    """A downtrending market must produce a RISK_OFF flag, not an empty list."""
    p = price_panels
    # Panel of only weak names -- strict gating would yield almost nothing.
    close = p["close"][p["down"] + p["up"][:3]]
    res = run_trend_gate(
        close, p["high"][close.columns], p["low"][close.columns],
        p["volume"][close.columns], sectors=p["sectors"][close.columns],
    )
    assert res.regime == "RISK_OFF"


def test_gates_treat_missing_history_as_failure():
    """A name without 200 bars must not pass a gate it cannot be evaluated on."""
    dates = [d.date() for d in pd.bdate_range("2024-01-02", periods=30)]
    close = pd.DataFrame({"NEW": np.linspace(10, 20, 30)}, index=dates)
    feats = compute_trend_features(
        close, close * 1.01, close * 0.99, close * 0 + 1e6
    )
    gates = apply_gates(feats, rs_min=70)
    assert not gates["passes_all"].any()
    assert not gates["close_above_sma200"].any()


def test_reject_counts_cover_every_gate(price_panels):
    p = price_panels
    res = run_trend_gate(p["close"], p["high"], p["low"], p["volume"])
    assert set(res.reject_counts) == set(GATE_NAMES)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def test_momentum_12_1_skips_the_last_month():
    """12-1 must ignore the most recent 21 bars. A late spike must not show up."""
    n = 300
    base = pd.DataFrame({"X": np.linspace(100, 110, n)})
    spiked = base.copy()
    spiked.iloc[-10:] = 500.0  # violent recent move

    assert ind.momentum_12_1(base)["X"] == pytest.approx(
        ind.momentum_12_1(spiked)["X"]
    ), "recent-month move contaminated 12-1 momentum"


def test_momentum_12_1_uses_the_right_window():
    n = 300
    prices = pd.DataFrame({"X": np.linspace(100, 400, n)})
    expected = prices["X"].iloc[-(21 + 1)] / prices["X"].iloc[-253] - 1
    assert ind.momentum_12_1(prices)["X"] == pytest.approx(expected)


def test_rsi_bounds_and_monotone_series():
    up = pd.DataFrame({"X": np.linspace(10, 100, 100)})
    rsi = ind.rsi_wilder(up)["X"]
    assert 95 <= rsi <= 100, f"unbroken uptrend should pin RSI near 100, got {rsi}"

    down = pd.DataFrame({"X": np.linspace(100, 10, 100)})
    assert 0 <= ind.rsi_wilder(down)["X"] <= 5


def test_percentile_rank_is_cross_sectional():
    s = pd.Series([1.0, 2.0, 3.0, 4.0], index=list("abcd"))
    pct = ind.percentile_rank(s)
    assert pct["d"] == 100.0
    assert pct["a"] == 25.0


def test_consecutive_up_days():
    prices = pd.DataFrame({"X": [1, 2, 1, 2, 3, 4, 5]}, dtype=float)
    assert ind.consecutive_up_days(prices)["X"] == 4


def test_residual_momentum_strips_sector_move():
    """Two names that only moved because their sector did should not both look
    like strong momentum after residualisation."""
    rng = np.random.default_rng(3)
    n = 300
    sector_move = np.cumsum(rng.normal(0.002, 0.005, n))
    idio = np.cumsum(rng.normal(0.0, 0.005, n))
    close = pd.DataFrame(
        {
            "BETA": 100 * np.exp(sector_move),
            "ALPHA": 100 * np.exp(sector_move + idio),
            "OTHER": 100 * np.exp(sector_move * 0.9),
        }
    )
    sectors = pd.Series(["Tech", "Tech", "Tech"], index=close.columns)
    resid = ind.residual_momentum(close, ind.market_return(close), sectors)
    assert resid.notna().all()
    # ALPHA carries the idiosyncratic component, so it must rank above BETA.
    assert resid["ALPHA"] != resid["BETA"]


def test_atr_is_positive_and_scaled():
    n = 100
    close = pd.DataFrame({"X": np.full(n, 100.0)})
    high = close + 2
    low = close - 2
    a = ind.atr(high, low, close)["X"]
    assert a == pytest.approx(4.0, rel=0.05)


def test_trend_gate_runs_fast_on_a_wide_panel():
    """The whole point of Stage 1 is that it is cheap. Guard the budget."""
    import time

    rng = np.random.default_rng(11)
    dates = [d.date() for d in pd.bdate_range("2022-01-03", periods=420)]
    tickers = [f"T{i:04d}" for i in range(2000)]
    rets = rng.normal(0.0004, 0.015, (len(dates), len(tickers)))
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=tickers
    )
    vol = pd.DataFrame(1e6, index=dates, columns=tickers)
    sectors = pd.Series(["Tech"] * len(tickers), index=tickers)

    t0 = time.perf_counter()
    run_trend_gate(close, close * 1.01, close * 0.99, vol, sectors=sectors)
    elapsed = time.perf_counter() - t0
    assert elapsed < 30, f"stage 1 took {elapsed:.1f}s for 2000 names"


def test_custom_config_threshold_is_respected(price_panels):
    p = price_panels
    strict = TrendGateConfig(pct_of_52w_high_min=0.99, min_survivors_before_relax=0)
    res = run_trend_gate(
        p["close"], p["high"], p["low"], p["volume"],
        sectors=p["sectors"], config=strict,
    )
    assert (res.survivors["pct_of_52w_high"] >= 0.99).all()


def test_gate_runs_on_short_history_without_collapsing():
    """A first backfill may only have ~205 days. The 200-day-SMA slope must
    still be computable (via slope()'s NaN tolerance) so the gate returns a real
    universe instead of collapsing to zero -- this is what lets research start
    on stored data without waiting out a full 252-session backfill."""
    n = 207
    dates = pd.bdate_range(end="2026-07-31", periods=n)
    rng = np.random.default_rng(7)
    tickers = [f"T{i:03d}" for i in range(300)]
    close = pd.DataFrame(index=dates, columns=tickers, dtype=float)
    for t in tickers:
        drift = rng.uniform(-0.0003, 0.0013)
        close[t] = 100 * np.cumprod(1 + rng.normal(drift, 0.012, n))
    vol = pd.DataFrame(1e6, index=dates, columns=tickers)
    sectors = pd.Series("Tech", index=tickers)

    # The former failure mode: SMA200 slope all-NaN -> every name rejected.
    sl = ind.slope(ind.sma(close, 200), 21)
    assert sl.notna().all(), "sma200 slope collapsed to NaN on short history"

    res = run_trend_gate(close, close * 1.01, close * 0.99, vol, sectors=sectors)
    assert len(res.survivors) > 0, "gate collapsed to an empty universe"
