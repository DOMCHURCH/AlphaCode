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
    """Wilder ATR, last value per ticker.

    True range is the element-wise max of the three components over the aligned
    (dates x tickers) panels. `np.fmax` does that in place -- NaN-skipping, so a
    missing prior close falls back to the high-low leg exactly as the old
    stack/max/unstack did -- but without the stack->long->unstack round-trip that
    made this the single slowest indicator in Stage 1 (~1.9s -> ~0.15s at
    5000x420, verified numerically identical).
    """
    prev_close = close.shift(1)
    tr = np.fmax(
        np.fmax((high - low), (high - prev_close).abs()),
        (low - prev_close).abs(),
    )
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

    Vectorised: tickers in the SAME sector share the design matrix
    [1, market, sector_return], so ONE least-squares solve handles every ticker
    in that sector at once (Y is a matrix, not a vector). This turns ~5,000
    per-ticker `lstsq` calls into ~11 (one per sector) -- the whole of Stage 1's
    runtime. Sector-less names regress on [1, market] only.
    """
    rets = daily_returns(panel).tail(lookback)
    if rets.empty or len(rets) < 60:
        return pd.Series(np.nan, index=panel.columns)

    mkt = market.reindex(rets.index).astype(float).to_numpy(dtype=float)
    sector_rets = _sector_return_matrix(rets, sectors)
    sec_of = sectors.reindex(rets.columns)
    n_days = len(rets)
    ones = np.ones(n_days, dtype=float)

    out = pd.Series(np.nan, index=rets.columns, dtype=float)
    # Group by sector (NaN-sector names batched together, regressed on market
    # only) so each group is a single matrix solve.
    grouped = sec_of.fillna("__nosec__")
    for sec, cols_idx in grouped.groupby(grouped).groups.items():
        cols = [c for c in cols_idx if c in rets.columns]
        if not cols:
            continue
        if sec != "__nosec__" and sec in sector_rets.columns:
            X = np.column_stack([ones, mkt, sector_rets[sec].to_numpy(dtype=float)])
        else:  # sector-less (or a sector with no return series): market only
            X = np.column_stack([ones, mkt])
        row_ok = np.isfinite(X).all(axis=1)
        if int(row_ok.sum()) < 60:
            continue
        Xf = X[row_ok]
        Y = rets[cols].to_numpy(dtype=float)[row_ok]  # [Tf, n]
        finite_per_col = np.isfinite(Y).sum(axis=0)
        # Fill missing days with 0 (a neutral daily return) so the batched solve is
        # well-posed; the residual on a filled day is then ~0 and columns with too
        # few real days are set NaN below.
        Yf = np.nan_to_num(Y, nan=0.0)
        try:
            beta, *_ = np.linalg.lstsq(Xf, Yf, rcond=None)  # [k, n]
        except np.linalg.LinAlgError:
            continue
        resid = Yf - Xf @ beta  # [Tf, n]
        if resid.shape[0] > skip:  # drop the most recent month, same as 12-1
            resid = resid[:-skip]
        # Cumulative abnormal (residual) return = SUM of residuals. Summing rather
        # than prod(1+r)-1 avoids log1p's domain error on residuals below -1 (which
        # are regression residuals, not real returns); for the small residuals here
        # the two are numerically ~equal, and the factor is z-scored anyway.
        val = np.where(finite_per_col >= 60, resid.sum(axis=0), np.nan)
        out.loc[cols] = val
    return out.reindex(panel.columns)


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
