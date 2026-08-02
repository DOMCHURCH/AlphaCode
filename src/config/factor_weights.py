"""Factor weights and rate-limit configuration.

Weights live in code (not the DB) so a run is always reproducible from a git SHA.
They are re-fit quarterly from the IC tracker (see src/validation/ic.py), never daily.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Stage 2 composite: category weights must sum to 1.0
# ---------------------------------------------------------------------------
CATEGORY_WEIGHTS: dict[str, float] = {
    "momentum": 0.30,
    "quality": 0.25,
    "revisions": 0.20,
    "pead": 0.15,
    "value": 0.10,
}

# Within-category weights. Each dict sums to 1.0.
# `sign` handling: factors where "low is good" are negated in composite.py via
# NEGATIVE_FACTORS, not by using a negative weight here.
MOMENTUM_WEIGHTS: dict[str, float] = {
    "mom_12_1": 0.50,
    "mom_quality": 0.25,
    "mom_residual": 0.25,
}

QUALITY_WEIGHTS: dict[str, float] = {
    "gross_profitability": 0.25,
    "roic": 0.20,
    "accruals": 0.15,
    "fcf_yield": 0.20,
    "debt_trend": 0.10,
    "piotroski": 0.10,
}

REVISION_WEIGHTS: dict[str, float] = {
    "eps_rev_4w": 0.35,
    "rev_rev_4w": 0.25,
    "up_down_ratio_90d": 0.20,
    "reco_trend_delta": 0.20,
}

PEAD_WEIGHTS: dict[str, float] = {
    "sue": 0.50,
    "pead_window": 0.20,
    "earnings_gap": 0.30,
}

VALUE_WEIGHTS: dict[str, float] = {
    "ev_ebit_inv": 0.40,
    "ev_sales_inv": 0.25,
    "fcf_price": 0.35,
}

CATEGORY_FACTORS: dict[str, dict[str, float]] = {
    "momentum": MOMENTUM_WEIGHTS,
    "quality": QUALITY_WEIGHTS,
    "revisions": REVISION_WEIGHTS,
    "pead": PEAD_WEIGHTS,
    "value": VALUE_WEIGHTS,
}

# Provenance of each non-price factor, so the completeness gate can say whether a
# 0% factor is a DATA-LOADING GAP (free -- SEC XBRL can fill it, run the backfill)
# or a SPEND DECISION (no free source; permanently 0% without a paid feed). The
# momentum factors are price-derived and always present, so they are in neither.
SEC_SUPPLIABLE_FACTORS = frozenset(
    {
        # quality + value (from SEC XBRL fundamentals)
        "gross_profitability", "roic", "accruals", "fcf_yield", "piotroski",
        "debt_trend", "ev_ebit_inv", "ev_sales_inv", "fcf_price",
        # pead (from as-reported earnings; needs an EarningsEvent writer, still no
        # paid feed required)
        "sue", "pead_window", "earnings_gap",
    }
)
PAID_ONLY_FACTORS = frozenset(
    {
        # estimate revisions + analyst signals: no free source (needs Finnhub/paid)
        "eps_rev_4w", "rev_rev_4w", "up_down_ratio_90d", "reco_trend_delta",
    }
)

# Factors where a LOW raw value is the good outcome. Their z-score is flipped.
NEGATIVE_FACTORS = frozenset(
    {
        "accruals",  # Sloan (1996): high accruals -> earnings not backed by cash
        "debt_trend",  # rising net_debt/EBITDA is a negative
    }
)

# ---------------------------------------------------------------------------
# Stage 1 trend gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrendGateConfig:
    rs_percentile_min: float = 70.0
    rs_percentile_min_risk_off: float = 50.0
    pct_of_52w_high_min: float = 0.75
    sma200_slope_lookback: int = 21
    min_survivors_before_relax: int = 300
    # RS composite weights (IBD-style): 3m/6m/9m/12m
    rs_weights: dict[str, float] = field(
        default_factory=lambda: {
            "ret_3m": 0.40,
            "ret_6m": 0.30,
            "ret_9m": 0.20,
            "ret_12m": 0.10,
        }
    )


TREND_GATE = TrendGateConfig()

# ---------------------------------------------------------------------------
# Stage 3 catalyst scoring. Points, later z-scored across the survivor set.
# ---------------------------------------------------------------------------
CATALYST_POINTS: dict[str, float] = {
    "insider_cluster_buy": 3.0,  # 3+ distinct insiders buying in 30d
    "insider_buy_ceo_cfo": 2.0,
    "insider_buy_director": 1.0,
    "insider_buy_other": 0.5,
    "insider_sale": -0.25,  # mostly 10b5-1 noise, discounted hard
    "form_8k_101": 1.0,  # material definitive agreement
    "form_8k_202": 0.0,  # results of operations - cross-ref with PEAD instead
    "form_8k_502_cfo": -1.5,
    "form_8k_502_other": -0.25,
    "shelf_or_secondary": -3.0,  # S-1 / S-3 / 424B5: dilution incoming
    "sc_13d": 2.5,  # activist stake
    "institutional_accumulation": 1.0,
    "news_volume_spike": 1.0,
    "news_tone_slope_positive": 1.0,
    "source_diversity_high": 0.5,
    "theme_merger": 2.0,
    "theme_company_layoff": 0.5,  # margin expansion read, short-term positive
    "theme_sector_layoff": -1.0,  # demand signal, negative
    "theme_bankruptcy": -4.0,
    "theme_strike": -1.0,
    "theme_recall": -1.5,
    "theme_legal": -1.0,
    "options_iv_rank_low": 0.5,
    "options_call_skew": 1.0,
    "options_unusual_oi_upside": 1.0,
}

# ---------------------------------------------------------------------------
# Stage 3 hard rejects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskScreenConfig:
    max_short_interest_pct: float = 20.0
    max_days_to_cover: float = 5.0
    min_days_to_earnings: int = 3
    max_vol_multiple_of_median: float = 3.0
    max_spread_bps: float = 50.0


RISK_SCREEN = RiskScreenConfig()

# ---------------------------------------------------------------------------
# Stage 5 rubric caps
# ---------------------------------------------------------------------------
RUBRIC_MAX: dict[str, int] = {
    "trend": 25,
    "fundamental": 20,
    "catalyst": 20,
    "news": 15,
    "macro": 10,
    "risk": 10,
}

# ---------------------------------------------------------------------------
# Macro regime -> sector tilt. Stored as multipliers around 1.0 (1.15 = favour,
# 0.85 = suppress) because that reads naturally, but they are applied ADDITIVELY
# as (tilt - 1) * REGIME_TILT_STRENGTH in stage3. A multiplicative application
# is sign-wrong: scores are z-scores and go negative, and multiplying a negative
# score by 1.15 makes it *more* negative, which would penalise a favoured
# sector. The additive form nudges every name the same direction regardless of
# sign, which is what "tilt the sector" actually means.
REGIME_TILT_STRENGTH: float = 1.0  # z-score units per unit of (tilt - 1)

REGIME_SECTOR_TILTS: dict[str, dict[str, float]] = {
    "RISK_ON": {
        "Technology": 1.15,
        "Consumer Cyclical": 1.10,
        "Industrials": 1.10,
        "Utilities": 0.90,
        "Consumer Defensive": 0.90,
    },
    "LATE_CYCLE": {
        "Energy": 1.15,
        "Healthcare": 1.10,
        "Consumer Defensive": 1.10,
        "Technology": 0.92,
        "Consumer Cyclical": 0.92,
    },
    "RISK_OFF": {
        "Utilities": 1.20,
        "Consumer Defensive": 1.15,
        "Healthcare": 1.05,
        "Technology": 0.85,
        "Consumer Cyclical": 0.80,
        "Energy": 0.90,
    },
    "RECOVERY": {
        "Financial Services": 1.15,
        "Consumer Cyclical": 1.12,
        "Industrials": 1.08,
        "Utilities": 0.90,
        "Consumer Defensive": 0.88,
    },
}

# In RISK_OFF we ship fewer names.
REGIME_FINAL_COUNT: dict[str, int] = {
    "RISK_ON": 10,
    "LATE_CYCLE": 10,
    "RECOVERY": 10,
    "RISK_OFF": 5,
}
