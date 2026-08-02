"""Vectorised technical indicators over a wide price panel.

Every function takes a DataFrame indexed by date with one column per ticker and
returns either a same-shaped frame or a per-ticker Series. No loops over
tickers -- 6000 names x 400 bars has to run in single-digit seconds.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_YEAR = 252
TRADING_DAYS_MONTH = 21


def ema(panel: pd.DataFrame, span: int) -> pd.DataFrame:
    return panel.ewm(span=span, adjust=False, min_periods=span).mean()


def sma(panel: pd.DataFrame, window: int) -> pd.DataFrame:
    return panel.rolling(window, min_periods=window).mean()


def slope(panel: pd.DataFrame, lookback: int) -> pd.Series:
    """Simple slope of the last `lookback` observations, per column.

    Normalised by the level so it is comparable across price scales. Requires a
    fully-formed window: if the panel is shorter than `lookback + 1`, or the
    window's first point is NaN (e.g. an SMA200 that has only just warmed up),
    the slope is NaN. That is deliberate -- a 200-day SMA with a handful of days
    of existence has a noise slope, and a name that cannot be evaluated on this
    gate must fail it, not pass on a fabricated trend.
    """
    if len(panel) < lookback + 1:
        return pd.Series(np.nan, index=panel.columns)
    tail = panel.tail(lookback + 1)
    first = tail.iloc[0]
    last = tail.iloc[-1]
    denom = first.abs().replace(0, np.nan)
    return (last - first) / denom


def total_return(panel: pd.DataFrame, lookback: int) -> pd.Series:
    """Return over the trailing `lookback` bars, per ticker."""
    if len(panel) < lookback + 1:
        return pd.Series(np.nan, index=panel.columns)
    start = panel.iloc[-(lookback + 1)]
    end = panel.iloc[-1]
    return (end / start.replace(0, np.nan)) - 1.0


def momentum_12_1(panel: pd.DataFrame) -> pd.Series:
    """Return from t-252 to t-21. Skipping the last month is not optional --
    short-term reversal contaminates raw 12-month momentum (Jegadeesh-Titman)."""
    need = TRADING_DAYS_YEAR + 1
    if len(panel) < need:
        return pd.Series(np.nan, index=panel.columns)
    start = panel.iloc[-need]
    end = panel.iloc[-(TRADING_DAYS_MONTH + 1)]
    return (end / start.replace(0, np.nan)) - 1.0


def daily_returns(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.pct_change(fill_method=None)


def realised_vol(panel: pd.DataFrame, window: int = 20, annualise: bool = True) -> pd.Series:
    rets = daily_returns(panel).tail(window)
    vol = rets.std(ddof=1)
    return vol * np.sqrt(TRADING_DAYS_YEAR) if annualise else vol


def momentum_quality(panel: pd.DataFrame) -> pd.Series:
    """12-1 momentum divided by daily-return volatility.

    Smooth trends outperform jumpy ones -- the Frog-in-the-Pan effect. A stock
    that ground out 40% beats one that gapped 40% in three sessions.
    """
    mom = momentum_12_1(panel)
    vol = daily_returns(panel).tail(TRADING_DAYS_YEAR).std(ddof=1)
    return mom / vol.replace(0, np.nan)


def atr(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, window: int = 14
) -> pd.Series:
    """Wilder ATR, last value per ticker."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).stack(future_stack=True),
            (high - prev_close).abs().stack(future_stack=True),
            (low - prev_close).abs().stack(future_stack=True),
        ],
        axis=1,
    ).max(axis=1).unstack()
    return tr.ewm(alpha=1 / window, adjust=False, min_periods=window).mean().iloc[-1]


def rsi_wilder(panel: pd.DataFrame, window: int = 14) -> pd.Series:
    """Wilder RSI (smoothed), last value per ticker."""
    delta = panel.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    # Zero average loss means an unbroken up-streak: RSI is 100 by definition.
    out = out.where(avg_loss.ne(0) | avg_gain.eq(0), 100.0)
    return out.iloc[-1]


def consecutive_up_days(panel: pd.DataFrame, max_lookback: int = 30) -> pd.Series:
    up = (panel.diff() > 0).tail(max_lookback)
    if up.empty:
        return pd.Series(0, index=panel.columns)
    # Count trailing True runs: reverse, cumprod stops at the first False.
    rev = up.iloc[::-1].astype(int)
    return rev.cumprod().sum()


def rolling_max(panel: pd.DataFrame, window: int) -> pd.Series:
    return panel.tail(window).max()


def rolling_min(panel: pd.DataFrame, window: int) -> pd.Series:
    return panel.tail(window).min()


def volume_expansion(volume: pd.DataFrame, short: int = 5, long: int = 50) -> pd.Series:
    recent = volume.tail(short).mean()
    baseline = volume.tail(long).mean()
    return recent / baseline.replace(0, np.nan)


def donchian_breakout(
    close: pd.DataFrame, high: pd.DataFrame, window: int = 20
) -> pd.Series:
    """True when today's close is at the high of the trailing `window` bars."""
    if len(high) < window:
        return pd.Series(False, index=close.columns)
    band = high.tail(window).max()
    return close.iloc[-1] >= band * 0.999  # tolerance for float/rounding


def percentile_rank(series: pd.Series) -> pd.Series:
    """Cross-sectional percentile rank, 0-100. NaNs stay NaN."""
    return series.rank(pct=True, na_option="keep") * 100.0


def residual_momentum(
    panel: pd.DataFrame,
    market: pd.Series,
    sectors: pd.Series,
    *,
    lookback: int = TRADING_DAYS_YEAR,
    skip: int = TRADING_DAYS_MONTH,
) -> pd.Series:
    """Momentum of the residual after regressing on market and sector returns.

    Cleaner signal than raw momentum and much less crowded, because it strips
    out the part of the move that is just "this sector went up".

    Implemented as a vectorised OLS per ticker on a two-factor design matrix.
    """
    rets = daily_returns(panel).tail(lookback)
    if rets.empty or len(rets) < 60:
        return pd.Series(np.nan, index=panel.columns)

    mkt = market.reindex(rets.index).astype(float)
    sector_rets = _sector_return_matrix(rets, sectors)

    out: dict[str, float] = {}
    mkt_v = mkt.to_numpy(dtype=float)
    for ticker in rets.columns:
        y = rets[ticker].to_numpy(dtype=float)
        sec_name = sectors.get(ticker)
        sec_v = (
            sector_rets[sec_name].to_numpy(dtype=float)
            if sec_name in sector_rets.columns
            else np.zeros_like(mkt_v)
        )
        X = np.column_stack([np.ones_like(mkt_v), mkt_v, sec_v])
        mask = np.isfinite(y) & np.isfinite(X).all(axis=1)
        if mask.sum() < 60:
            out[ticker] = np.nan
            continue
        try:
            beta, *_ = np.linalg.lstsq(X[mask], y[mask], rcond=None)
        except np.linalg.LinAlgError:
            out[ticker] = np.nan
            continue
        resid = y[mask] - X[mask] @ beta
        # Skip the most recent month of residuals, same logic as 12-1.
        if len(resid) > skip:
            resid = resid[:-skip]
        out[ticker] = float(np.expm1(np.log1p(resid).sum())) if len(resid) else np.nan
    return pd.Series(out)


def _sector_return_matrix(rets: pd.DataFrame, sectors: pd.Series) -> pd.DataFrame:
    """Equal-weight daily return per sector."""
    mapping = sectors.reindex(rets.columns)
    frames = {}
    for sec, group in mapping.dropna().groupby(mapping.dropna()):
        cols = [c for c in group.index if c in rets.columns]
        if cols:
            frames[sec] = rets[cols].mean(axis=1)
    return pd.DataFrame(frames, index=rets.index) if frames else pd.DataFrame(
        index=rets.index
    )


def market_return(panel: pd.DataFrame) -> pd.Series:
    """Equal-weight universe return, used as the market factor."""
    return daily_returns(panel).mean(axis=1)
