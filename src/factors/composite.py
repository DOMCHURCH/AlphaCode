"""Stage 2 -- the multi-factor composite. ~1200 -> 400.

Institutional layer. Everything cross-sectional and sector-neutral.

Categories and weights live in src/config/factor_weights.py. This module only
knows how to assemble the raw factor frame and run the z-score/composite math.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import structlog
from sqlalchemy.orm import Session

from src.config.factor_weights import (
    CATEGORY_FACTORS,
    CATEGORY_WEIGHTS,
    MODE_FULL,
    NEGATIVE_FACTORS,
    mode_config,
)
from src.factors import crosssection as xs
from src.factors.fundamentals import build_fundamental_factors
from src.storage.pit import get_estimate_revisions, get_last_earnings

log = structlog.get_logger(__name__)

ALL_FACTORS = [f for weights in CATEGORY_FACTORS.values() for f in weights]


@dataclass
class CompositeResult:
    scores: pd.DataFrame  # ticker-indexed: composite, categories, rank, completeness
    raw: pd.DataFrame  # raw factor values, for the report appendix
    z: pd.DataFrame  # sector-neutral z-scores
    selected: list[str]
    factor_coverage: dict[str, float] = field(default_factory=dict)
    # Which factor set produced `factor_composite`. MODE_MOMENTUM_ONLY rankings
    # are a separate artifact and must never be pooled with full runs.
    mode: str = MODE_FULL
    # Mean per-name completeness across ALL 19 factors, regardless of mode. In
    # momentum-only mode `scores.data_completeness` is measured against the 3
    # factors that mode uses (so it reads ~100%); this keeps the honest
    # full-composite number visible so nobody mistakes one for the other.
    full_completeness: float = 0.0


def assemble_raw_factors(
    session: Session,
    trend_features: pd.DataFrame,
    universe: pd.DataFrame,
    as_of: dt.date,
    *,
    revisions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build the raw (pre-z-score) factor frame for the Stage 1 survivors.

    Momentum comes from Stage 1's already-computed panel features -- no reason
    to recompute. Quality/value come from PIT fundamentals. Revisions and PEAD
    come from stored estimate snapshots and earnings events.
    """
    tickers = list(trend_features.index)
    uni = universe.set_index("ticker") if "ticker" in universe.columns else universe

    market_caps = uni.get("market_cap", pd.Series(dtype=float)).reindex(tickers)
    prices = trend_features["close"].reindex(tickers)

    raw = pd.DataFrame(index=pd.Index(tickers, name="ticker"))

    # --- Momentum (from Stage 1) ---
    raw["mom_12_1"] = trend_features["mom_12_1"].reindex(tickers)
    raw["mom_quality"] = trend_features["mom_quality"].reindex(tickers)
    raw["mom_residual"] = trend_features["mom_residual"].reindex(tickers)

    # --- Quality + Value (PIT fundamentals) ---
    fund = build_fundamental_factors(session, tickers, as_of, market_caps, prices)
    for col in (
        "gross_profitability",
        "roic",
        "accruals",
        "fcf_yield",
        "debt_trend",
        "piotroski",
        "ev_ebit_inv",
        "ev_sales_inv",
        "fcf_price",
    ):
        raw[col] = fund[col].reindex(tickers) if col in fund.columns else np.nan

    # --- Estimate revisions ---
    rev = (
        revisions
        if revisions is not None
        else get_estimate_revisions(session, tickers, as_of)
    )
    for col in ("eps_rev_4w", "rev_rev_4w", "up_down_ratio_90d", "reco_trend_delta"):
        if not rev.empty and col in rev.columns:
            raw[col] = rev.set_index("ticker")[col].reindex(tickers)
        else:
            raw[col] = np.nan

    # --- PEAD ---
    earn = get_last_earnings(session, tickers, as_of)
    if not earn.empty:
        e = earn.set_index("ticker")
        raw["sue"] = e["sue"].reindex(tickers)
        raw["pead_window"] = e["pead_window"].reindex(tickers)
        raw["earnings_gap"] = e["earnings_gap"].reindex(tickers)
        raw["days_since_earnings"] = e["days_since_earnings"].reindex(tickers)
    else:
        for col in ("sue", "pead_window", "earnings_gap", "days_since_earnings"):
            raw[col] = np.nan

    # PEAD runs roughly 1-60 days post-print, so SUE only counts inside that
    # window -- a great surprise from six months ago is not a live signal.
    raw["sue"] = raw["sue"] * raw["pead_window"].fillna(0.0)

    # Carry through the descriptive columns the later stages need.
    raw["sector"] = uni.get("sector", pd.Series(dtype=object)).reindex(tickers)
    raw["sector_source"] = uni.get(
        "sector_source", pd.Series(dtype=object)
    ).reindex(tickers)
    raw["market_cap"] = market_caps
    raw["close"] = prices
    return raw


def score_composite(
    raw: pd.DataFrame,
    *,
    category_weights: dict[str, float] | None = None,
    category_factors: dict[str, dict[str, float]] | None = None,
) -> CompositeResult:
    """Winsorize -> sector z-score -> weighted category -> composite -> rank."""
    cat_w = category_weights or CATEGORY_WEIGHTS
    cat_f = category_factors or CATEGORY_FACTORS
    factors = [f for w in cat_f.values() for f in w]

    sectors = raw["sector"] if "sector" in raw.columns else pd.Series(
        np.nan, index=raw.index, dtype=object
    )
    # Names with no real sector (unmapped SIC, missing FMP) must NOT be pooled
    # into an "Unknown" bucket and z-scored against each other -- that neutralizes
    # against the wrong peers. Coerce non-sectors to NaN so `sector_zscore` drops
    # them into the universe-wide residual pool (excluded from sector-neutral).
    sectors = sectors.astype(object)
    _blank = sectors.isna() | sectors.astype(str).str.strip().isin(
        ["", "Unknown", "unknown", "N/A", "None", "nan"]
    )
    sectors = sectors.mask(_blank, other=np.nan)

    z, coverage = xs.build_factor_zscores(raw, sectors, factors, NEGATIVE_FACTORS)

    categories = pd.DataFrame(index=raw.index)
    cat_completeness = {}
    for cat, weights in cat_f.items():
        categories[cat] = xs.weighted_category_score(z, weights)
        cols = [c for c in weights if c in z.columns]
        cat_completeness[cat] = (
            z[cols].notna().sum(axis=1) / len(cols) if cols else 0.0
        )

    composite = xs.composite_score(categories, cat_w)

    scores = categories.copy()
    scores["factor_composite"] = composite
    scores["data_completeness"] = coverage
    scores["sector"] = sectors
    if "sector_source" in raw.columns:
        scores["sector_source"] = raw["sector_source"]
    for cat, cov in cat_completeness.items():
        scores[f"completeness_{cat}"] = cov
    scores["rank"] = xs.cross_sectional_rank(composite)
    scores = scores.sort_values("factor_composite", ascending=False)

    return CompositeResult(scores=scores, raw=raw, z=z, selected=[])


def run_stage2(
    session: Session,
    trend_features: pd.DataFrame,
    universe: pd.DataFrame,
    as_of: dt.date,
    *,
    take: int = 400,
    revisions: pd.DataFrame | None = None,
    mode: str = MODE_FULL,
) -> CompositeResult:
    """`mode` selects which factor set scores the composite.

    MODE_FULL is the real thing: all 19 factors, all five categories.
    MODE_MOMENTUM_ONLY scores on the price-derived momentum factors alone -- a
    separate, labelled artifact for use while fundamentals are still loading, not
    a degraded full run. It does not touch any gate or weight of the full mode.
    """
    cat_w, cat_f = mode_config(mode)
    raw = assemble_raw_factors(
        session, trend_features, universe, as_of, revisions=revisions
    )
    result = score_composite(raw, category_weights=cat_w, category_factors=cat_f)
    result.mode = mode
    result.selected = list(result.scores.head(take).index)
    # Per-factor coverage: fraction of the Stage-1 survivors with a REAL value
    # (not NaN) for each factor. A factor at ~0 contributes nothing and its
    # category weight is dead -- the thing that produces a low mean_completeness.
    n = max(1, len(raw))
    factor_cov = {
        f: round(float(raw[f].notna().sum()) / n, 3) if f in raw.columns else 0.0
        for f in ALL_FACTORS
    }
    result.factor_coverage = factor_cov
    # True 19-factor completeness, computed from `raw` (which always carries every
    # factor) so it is mode-independent and cannot be inflated by scoring on a
    # narrower set.
    present = [f for f in ALL_FACTORS if f in raw.columns]
    result.full_completeness = (
        float(raw[present].notna().sum(axis=1).mean()) / len(ALL_FACTORS)
        if present and len(raw) else 0.0
    )
    log.info(
        "stage2_complete",
        entry=len(trend_features),
        exit=len(result.selected),
        mode=mode,
        mean_completeness=float(result.scores["data_completeness"].mean()),
        full_completeness=round(result.full_completeness, 3),
        factor_coverage=factor_cov,
    )
    return result


def stage2_payload(result: CompositeResult) -> dict[str, Any]:
    df = result.scores.loc[result.selected]
    return {
        "selected": result.selected,
        "scores": df.reset_index().to_dict(orient="records"),
        "raw": result.raw.loc[result.selected].reset_index().to_dict(orient="records"),
    }
