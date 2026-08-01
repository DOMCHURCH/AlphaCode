"""Cross-sectional factor math. This is where most implementations break.

The rules, in order, for every raw factor:

1. Winsorize at the 1st/99th percentile. One bad datapoint creates a fake
   40-sigma outlier that dominates the composite.
2. Z-score WITHIN sector. Without this the "top 10" is just whichever sector is
   currently hot or cheap.
3. Handle missing explicitly: z=0 (sector-neutral) for a missing factor, and
   track data_completeness per ticker. Never forward-fill fundamentals across a
   reporting gap. Never impute the universe mean.
4. Composite = weighted sum of category z-scores, then rank cross-sectionally.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import numpy as np
import pandas as pd

MIN_SECTOR_SIZE = 8  # below this, sector stats are noise -- fall back to universe


def winsorize(s: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """Clip to the [lower, upper] quantiles. NaNs are preserved."""
    valid = s.dropna()
    if valid.empty:
        return s
    lo = valid.quantile(lower)
    hi = valid.quantile(upper)
    if not np.isfinite(lo) or not np.isfinite(hi) or lo == hi:
        return s
    return s.clip(lower=lo, upper=hi)


def zscore(s: pd.Series) -> pd.Series:
    valid = s.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=s.index)
    mu = valid.mean()
    sd = valid.std(ddof=1)
    if not np.isfinite(sd) or sd == 0:
        return pd.Series(0.0, index=s.index).where(s.notna())
    return (s - mu) / sd


def sector_zscore(
    values: pd.Series,
    sectors: pd.Series,
    *,
    winsorize_first: bool = True,
    min_sector_size: int = MIN_SECTOR_SIZE,
) -> pd.Series:
    """Winsorize then z-score within each sector.

    Sectors with fewer than `min_sector_size` members (and names with no sector
    label at all) are pooled and z-scored against the whole universe -- a
    three-name sector produces a meaningless standard deviation.
    """
    values = values.astype(float)
    sectors = sectors.reindex(values.index)
    out = pd.Series(np.nan, index=values.index, dtype=float)

    counts = sectors.value_counts()
    big = {s for s, n in counts.items() if n >= min_sector_size}

    for sec in big:
        mask = sectors == sec
        chunk = values[mask]
        if winsorize_first:
            chunk = winsorize(chunk)
        out.loc[mask] = zscore(chunk)

    residual_mask = ~sectors.isin(big)
    if residual_mask.any():
        chunk = values[residual_mask]
        if winsorize_first:
            chunk = winsorize(chunk)
        out.loc[residual_mask] = zscore(chunk)

    return out


def build_factor_zscores(
    raw: pd.DataFrame,
    sectors: pd.Series,
    factors: Iterable[str],
    negative_factors: Iterable[str] = (),
) -> tuple[pd.DataFrame, pd.Series]:
    """Sector-neutral z-scores for a set of raw factor columns.

    Returns (z_frame, coverage) where coverage is the fraction of requested
    factors that were actually present (not NaN) for each ticker. Missing
    factors are left NaN here and only collapsed to 0 at composite time, so
    coverage stays honest.
    """
    factors = list(factors)
    negative = set(negative_factors)
    z = pd.DataFrame(index=raw.index)
    for f in factors:
        if f not in raw.columns:
            z[f] = np.nan
            continue
        col = sector_zscore(raw[f], sectors)
        z[f] = -col if f in negative else col
    coverage = z.notna().sum(axis=1) / max(len(factors), 1)
    return z, coverage


def weighted_category_score(
    z: pd.DataFrame, weights: Mapping[str, float]
) -> pd.Series:
    """Weighted mean of available factor z-scores within a category.

    Missing factors contribute z=0 (sector-neutral) rather than dropping the
    ticker. We renormalise by the total weight so that a name with half the
    factors present is not mechanically penalised toward zero -- it is simply
    less differentiated, which is the honest outcome.
    """
    cols = [c for c in weights if c in z.columns]
    if not cols:
        return pd.Series(0.0, index=z.index)
    sub = z[cols]
    w = pd.Series({c: weights[c] for c in cols}, dtype=float)
    present = sub.notna()
    filled = sub.fillna(0.0)
    num = filled.mul(w, axis=1).sum(axis=1)
    den = present.mul(w, axis=1).sum(axis=1)
    return (num / den.replace(0, np.nan)).fillna(0.0)


def composite_score(
    category_scores: pd.DataFrame, weights: Mapping[str, float]
) -> pd.Series:
    cols = [c for c in weights if c in category_scores.columns]
    w = pd.Series({c: weights[c] for c in cols}, dtype=float)
    total = float(w.sum()) or 1.0
    return category_scores[cols].mul(w, axis=1).sum(axis=1) / total


def cross_sectional_rank(s: pd.Series, ascending: bool = False) -> pd.Series:
    """1 = best. NaNs sort last."""
    return s.rank(ascending=ascending, method="first", na_option="bottom").astype(int)


def sector_percentile(values: pd.Series, sectors: pd.Series) -> pd.Series:
    """Percentile of each value within its own sector, 0-100. For the report."""
    sectors = sectors.reindex(values.index)
    out = pd.Series(np.nan, index=values.index, dtype=float)
    for _sector, group in values.groupby(sectors):
        if len(group) >= 2:
            out.loc[group.index] = group.rank(pct=True) * 100.0
        else:
            out.loc[group.index] = 50.0
    return out
