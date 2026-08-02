"""Fact-packet construction. This is the token discipline layer.

Never raw filings, never raw price series, never raw news text. Computed
statistics only. Target: under 200 tokens per ticker for triage.

The funnel exists so that the expensive stages see few names; the packets exist
so that each of those names is cheap.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from src.catalysts.macro import MacroState
from src.util import or_default


def _r(v: Any, nd: int = 2) -> float | None:
    """Round, dropping NaN/inf. Precision beyond 2dp is wasted tokens."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, nd)


def _i(v: Any) -> int | None:
    f = _r(v, 0)
    return int(f) if f is not None else None


def build_triage_packet(
    ticker: str,
    scores: pd.Series,
    trend: pd.Series,
    raw: pd.Series,
    detail: dict[str, Any],
    macro: MacroState,
) -> dict[str, Any]:
    """One compressed packet. Empty fields are dropped, not sent as null."""
    news = detail.get("news") or {}
    insiders = detail.get("insiders") or {}
    filings = detail.get("filings") or {}
    options = detail.get("options") or {}
    risk = detail.get("risk") or {}

    packet: dict[str, Any] = {
        "t": ticker,
        "sec": or_default(scores.get("sector"), "Unknown"),
        "px": _r(trend.get("close")),
        "rs": _i(trend.get("rs_percentile")),
        "mom_z": _r(scores.get("momentum")),
        "qual_z": _r(scores.get("quality")),
        "rev_z": _r(scores.get("revisions")),
        "val_z": _r(scores.get("value")),
        "pead_d": _i(raw.get("days_since_earnings")),
        "sue": _r(raw.get("sue")),
        "insider": insiders.get("summary"),
        "filings": filings.get("forms") or None,
        "news_vol_z": _r(news.get("volume_z"), 1),
        "news_tone": _r(news.get("tone_avg"), 1),
        "themes": news.get("themes") or None,
        "iv_rank": _i(options.get("iv_rank")),
        "si_pct": _r(risk.get("short_interest_pct"), 1),
        "atr_pct": _r(trend.get("atr_pct"), 1),
        "pct_52wh": _r(trend.get("pct_of_52w_high")),
        "regime_fit": _r(scores.get("regime_fit"), 1),
    }
    return {k: v for k, v in packet.items() if v is not None and v != []}


def build_deep_packet(
    ticker: str,
    scores: pd.Series,
    trend: pd.Series,
    raw: pd.Series,
    detail: dict[str, Any],
    macro: MacroState,
    *,
    fundamentals: pd.DataFrame | None = None,
    sector_percentiles: dict[str, float] | None = None,
    filing_text: list[dict[str, str]] | None = None,
    next_earnings: dt.date | None = None,
) -> dict[str, Any]:
    """The Stage 5 packet. Larger context is fine here -- 25 calls total.

    Still no raw article bodies and no raw price arrays: headlines with tone
    scores, and a price-action summary rather than a series.
    """
    news = detail.get("news") or {}
    insiders = detail.get("insiders") or {}
    filings = detail.get("filings") or {}
    options = detail.get("options") or {}
    risk = detail.get("risk") or {}
    reco = detail.get("reco") or {}

    packet: dict[str, Any] = {
        "ticker": ticker,
        "sector": or_default(scores.get("sector"), "Unknown"),
        "factor_breakdown": {
            "momentum_z": _r(scores.get("momentum")),
            "quality_z": _r(scores.get("quality")),
            "revisions_z": _r(scores.get("revisions")),
            "pead_z": _r(scores.get("pead")),
            "value_z": _r(scores.get("value")),
            "composite": _r(scores.get("factor_composite")),
            "sector_percentiles": {
                k: _i(v) for k, v in (sector_percentiles or {}).items()
            },
            "data_completeness": _r(scores.get("data_completeness")),
        },
        "price_action": {
            "close": _r(trend.get("close")),
            "pct_of_52w_high": _r(trend.get("pct_of_52w_high")),
            "range_position": _range_position(trend),
            "ema20": _r(trend.get("ema20")),
            "sma50": _r(trend.get("sma50")),
            "sma200": _r(trend.get("sma200")),
            "atr_pct": _r(trend.get("atr_pct"), 1),
            "rsi14": _i(trend.get("rsi14")),
            "rs_percentile": _i(trend.get("rs_percentile")),
            "ret_3m": _r(trend.get("ret_3m")),
            "ret_12m": _r(trend.get("ret_12m")),
            "volume_expansion_5v50": _r(trend.get("vol_expansion")),
            "donchian_20d_breakout": bool(trend.get("donchian_breakout", False)),
            "realised_vol_20d": _r(trend.get("realised_vol_20d")),
        },
        "fundamentals_8q": _compact_fundamentals(fundamentals),
        "earnings": {
            "days_since_report": _i(raw.get("days_since_earnings")),
            "sue": _r(raw.get("sue")),
            "last_gap_pct": _r(raw.get("earnings_gap")),
            "pead_window_weight": _r(raw.get("pead_window")),
            "next_report": next_earnings.isoformat() if next_earnings else None,
        },
        "estimates": {
            "eps_revision_4w": _r(raw.get("eps_rev_4w"), 3),
            "revenue_revision_4w": _r(raw.get("rev_rev_4w"), 3),
            "reco_mean": _r(reco.get("reco_mean")),
            "reco_trend_delta": _r(reco.get("reco_trend_delta")),
        },
        "insiders": {
            "summary": insiders.get("summary"),
            "distinct_buyers_30d": insiders.get("distinct_buyers"),
            "cluster_buy": insiders.get("cluster"),
            "buy_value_usd": _i(insiders.get("buy_value_usd")),
            "sells": insiders.get("sells"),
        },
        "filings": {
            "forms_45d": filings.get("forms"),
            "flags": filings.get("flags"),
            # 1500 chars per filing, at most 3 filings. Never the full document.
            "excerpts": (filing_text or [])[:3],
        },
        "news": {
            "volume_z": _r(news.get("volume_z"), 1),
            "tone_avg": _r(news.get("tone_avg"), 1),
            "tone_slope_7d": _r(news.get("tone_slope_7d"), 2),
            "source_diversity": _r(news.get("source_diversity"), 2),
            "article_count": news.get("article_count"),
            "themes": news.get("themes"),
            # Headlines only, with tone. Never article bodies.
            "top_headlines": [
                {"h": h.get("title", "")[:180], "tone": _r(h.get("tone"), 1)}
                for h in (news.get("headlines") or [])[:5]
            ],
        },
        "options": {
            "iv_rank": _i(options.get("iv_rank")),
            "put_call_oi_ratio": _r(options.get("put_call_oi_ratio")),
            "skew_25d": _r(options.get("skew_25d"), 3),
            "upside_oi_share": _r(options.get("upside_oi_share")),
        },
        "risk": {
            "short_interest_pct": _r(risk.get("short_interest_pct"), 1),
            "days_to_cover": _r(risk.get("days_to_cover"), 1),
            "est_spread_bps": _i(risk.get("spread_bps")),
        },
        "macro": {
            "regime": macro.regime,
            "regime_score": _r(macro.score),
            "sector_fit": _r(macro.regime_fit(scores.get("sector")), 2),
            "hy_spread": _r(macro.levels.get("BAMLH0A0HYM2")),
            "yield_curve_10y2y": _r(macro.levels.get("T10Y2Y")),
            "vix": _r(macro.levels.get("VIXCLS"), 1),
        },
        "catalyst_score_components": {
            "insider": _r(scores.get("insider_points")),
            "filings": _r(scores.get("filing_points")),
            "news": _r(scores.get("news_points")),
            "options": _r(scores.get("options_points")),
        },
    }
    return _prune(packet)


def _range_position(trend: pd.Series) -> float | None:
    lo = _r(trend.get("low_52w"))
    hi = _r(trend.get("high_52w"))
    px = _r(trend.get("close"))
    if lo is None or hi is None or px is None or hi <= lo:
        return None
    return round((px - lo) / (hi - lo), 2)


def _compact_fundamentals(df: pd.DataFrame | None) -> list[dict[str, Any]] | None:
    """Last 8 quarters of revenue, EPS, FCF and margins as a compact table."""
    if df is None or df.empty:
        return None
    rows = []
    for period, r in df.tail(8).iterrows():
        rev = _r(r.get("revenue"), 0)
        row = {
            "q": str(period)[:10],
            "rev": _i(rev),
            "eps": _r(r.get("eps"), 2),
            "fcf": _i(r.get("free_cash_flow")),
            "gm": _margin(r.get("gross_profit"), rev),
            "om": _margin(r.get("operating_income"), rev),
        }
        rows.append({k: v for k, v in row.items() if v is not None})
    return rows or None


def _margin(numerator: Any, revenue: float | None) -> float | None:
    """Margin, or None when either side is missing. Never a fabricated zero."""
    num = _r(numerator)
    if num is None or not revenue:
        return None
    return _r(num / revenue, 3)


def _prune(obj: Any) -> Any:
    """Drop None/empty leaves recursively. Empty keys are pure token waste."""
    if isinstance(obj, dict):
        out = {k: _prune(v) for k, v in obj.items()}
        return {
            k: v for k, v in out.items() if v is not None and v != {} and v != []
        }
    if isinstance(obj, list):
        return [_prune(v) for v in obj if v is not None]
    return obj


def estimate_tokens(packet: dict[str, Any]) -> int:
    """Rough token count for the cost tracker. ~4 chars per token."""
    import json

    return len(json.dumps(packet, separators=(",", ":"))) // 4
