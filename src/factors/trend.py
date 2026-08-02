"""Stage 1 -- the trend gate. 6000 -> ~1200. Zero API calls.

"Is it going up or down right now? Remove the ones that aren't going up."
Implemented as a strict trend-following stack.

Everything is vectorised over a single wide panel. Target runtime: under 5s.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import structlog

from src.config.factor_weights import TREND_GATE, TrendGateConfig
from src.factors import indicators as ind

log = structlog.get_logger(__name__)

GATE_NAMES = [
    "close_above_ema20",
    "ema20_above_sma50",
    "close_above_sma200",
    "sma200_slope_positive",
    "ret_3m_positive",
    "rs_percentile_ok",
    "near_52w_high",
]


@dataclass
class TrendResult:
    survivors: pd.DataFrame  # ticker-indexed, gates + soft features
    all_names: pd.DataFrame  # everything scored, for diagnostics/backtests
    regime: str  # NORMAL | RISK_OFF
    reject_counts: dict[str, int]


def compute_trend_features(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    sectors: pd.Series | None = None,
    config: TrendGateConfig = TREND_GATE,
) -> pd.DataFrame:
    """All Stage 1 hard gates and soft signals, one row per ticker."""
    if close.empty:
        return pd.DataFrame()

    close = close.sort_index()
    last = close.iloc[-1]

    ema20 = ind.ema(close, 20).iloc[-1]
    sma50 = ind.sma(close, 50).iloc[-1]
    sma200_panel = ind.sma(close, 200)
    sma200 = sma200_panel.iloc[-1]
    sma200_slope = ind.slope(sma200_panel, config.sma200_slope_lookback)

    ret_3m = ind.total_return(close, 63)
    ret_6m = ind.total_return(close, 126)
    ret_9m = ind.total_return(close, 189)
    ret_12m = ind.total_return(close, 252)

    w = config.rs_weights
    rs_raw = (
        ret_3m.fillna(0) * w["ret_3m"]
        + ret_6m.fillna(0) * w["ret_6m"]
        + ret_9m.fillna(0) * w["ret_9m"]
        + ret_12m.fillna(0) * w["ret_12m"]
    )
    # A name with no 3m history has no meaningful RS; keep it NaN so it fails
    # the gate rather than ranking off a zero-filled composite.
    rs_raw = rs_raw.where(ret_3m.notna())
    rs_percentile = ind.percentile_rank(rs_raw)

    high_52w = ind.rolling_max(high if not high.empty else close, 252)
    low_52w = ind.rolling_min(low if not low.empty else close, 252)
    pct_of_high = last / high_52w.replace(0, np.nan)

    atr14 = (
        ind.atr(high, low, close)
        if not high.empty and not low.empty
        else pd.Series(np.nan, index=close.columns)
    )
    atr_pct = atr14 / last.replace(0, np.nan) * 100.0
    dist_to_high_atr = (high_52w - last) / atr14.replace(0, np.nan)

    feats = pd.DataFrame(
        {
            "close": last,
            "ema20": ema20,
            "sma50": sma50,
            "sma200": sma200,
            "sma200_slope_21d": sma200_slope,
            "ret_3m": ret_3m,
            "ret_6m": ret_6m,
            "ret_9m": ret_9m,
            "ret_12m": ret_12m,
            "rs_raw": rs_raw,
            "rs_percentile": rs_percentile,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "pct_of_52w_high": pct_of_high,
            "atr14": atr14,
            "atr_pct": atr_pct,
            "dist_to_high_atr": dist_to_high_atr,
            "rsi14": ind.rsi_wilder(close),
            "consecutive_up_days": ind.consecutive_up_days(close),
            "vol_expansion": (
                ind.volume_expansion(volume)
                if not volume.empty
                else pd.Series(np.nan, index=close.columns)
            ),
            "donchian_breakout": (
                ind.donchian_breakout(close, high if not high.empty else close)
            ),
            "realised_vol_20d": ind.realised_vol(close, 20),
        }
    )

    if sectors is not None and not sectors.empty:
        feats["mom_residual"] = ind.residual_momentum(
            close, ind.market_return(close), sectors
        )
    else:
        feats["mom_residual"] = np.nan

    feats["mom_12_1"] = ind.momentum_12_1(close)
    feats["mom_quality"] = ind.momentum_quality(close)
    feats["bars_available"] = close.notna().sum()
    return feats


def apply_gates(
    feats: pd.DataFrame, *, rs_min: float, config: TrendGateConfig = TREND_GATE
) -> pd.DataFrame:
    """Attach one boolean column per hard gate, plus `passes_all`."""
    g = pd.DataFrame(index=feats.index)
    g["close_above_ema20"] = feats["close"] > feats["ema20"]
    g["ema20_above_sma50"] = feats["ema20"] > feats["sma50"]
    g["close_above_sma200"] = feats["close"] > feats["sma200"]
    g["sma200_slope_positive"] = feats["sma200_slope_21d"] > 0
    g["ret_3m_positive"] = feats["ret_3m"] > 0
    g["rs_percentile_ok"] = feats["rs_percentile"] >= rs_min
    g["near_52w_high"] = feats["pct_of_52w_high"] >= config.pct_of_52w_high_min

    # NaN comparisons yield False, which is the behaviour we want: a name
    # without enough history does not pass a gate it cannot be evaluated on.
    g = g.fillna(False).astype(bool)
    g["passes_all"] = g[GATE_NAMES].all(axis=1)
    return g


def run_trend_gate(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    sectors: pd.Series | None = None,
    config: TrendGateConfig = TREND_GATE,
) -> TrendResult:
    """Stage 1 entry point.

    If fewer than `min_survivors_before_relax` names pass, the market is in a
    downtrend: we flag REGIME: RISK_OFF and relax the RS gate to 50 rather than
    returning an empty list.
    """
    feats = compute_trend_features(
        close, high, low, volume, sectors=sectors, config=config
    )
    if feats.empty:
        return TrendResult(pd.DataFrame(), pd.DataFrame(), "NORMAL", {})

    rs_min = config.rs_percentile_min
    gates = apply_gates(feats, rs_min=rs_min, config=config)
    regime = "NORMAL"

    if int(gates["passes_all"].sum()) < config.min_survivors_before_relax:
        regime = "RISK_OFF"
        rs_min = config.rs_percentile_min_risk_off
        log.warning(
            "trend_gate_risk_off",
            survivors_at_strict=int(gates["passes_all"].sum()),
            relaxed_rs_min=rs_min,
        )
        gates = apply_gates(feats, rs_min=rs_min, config=config)

    merged = feats.join(gates)
    reject_counts = {
        name: int((~gates[name]).sum()) for name in GATE_NAMES
    }
    survivors = merged[merged["passes_all"]].copy()

    log.info(
        "trend_gate_complete",
        entry=len(merged),
        exit=len(survivors),
        regime=regime,
        rs_min=rs_min,
    )
    return TrendResult(survivors, merged, regime, reject_counts)


def load_panels(
    session, tickers: list[str], as_of: dt.date, lookback_days: int = 600
) -> dict[str, pd.DataFrame]:
    """Load 400+ trading days of bars for the universe into wide panels.

    Streamed in ticker chunks and pivoted incrementally (see
    ``pit.load_price_panels``), so a universe-wide load never holds the full
    ~2.4M-row long frame in memory -- that unbounded read was OOM-killing the
    container mid-Stage-1. Panels come back float32.
    """
    from src.storage.pit import load_price_panels

    start = as_of - dt.timedelta(days=lookback_days)
    fields = ("close", "high", "low", "volume")
    return load_price_panels(session, tickers, start, as_of, fields)


def stage1_payload(result: TrendResult) -> dict[str, Any]:
    """Checkpoint-serialisable form."""
    return {
        "regime": result.regime,
        "reject_counts": result.reject_counts,
        "survivors": result.survivors.reset_index()
        .rename(columns={"index": "ticker"})
        .to_dict(orient="records"),
    }
