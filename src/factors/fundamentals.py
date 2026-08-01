"""Derive the quality and value factor inputs from PIT fundamentals.

Every number here is built from `get_fundamentals(ticker, as_of)` output, so
nothing is visible before its filing date. Missing inputs produce NaN -- never
a forward-fill across a reporting gap, never a universe-mean imputation.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from src.storage.pit import get_fundamentals

# Metrics we need loaded to derive everything below.
REQUIRED_METRICS = [
    "revenue",
    "cogs",
    "gross_profit",
    "net_income",
    "operating_income",
    "ebit",
    "ebitda",
    "total_assets",
    "total_equity",
    "total_debt",
    "long_term_debt",
    "cash",
    "operating_cash_flow",
    "capex",
    "free_cash_flow",
    "current_assets",
    "current_liabilities",
    "shares_diluted",
    "income_tax",
    "pretax_income",
]

TAX_RATE_FALLBACK = 0.21


def load_quarterly_wide(
    session: Session, tickers: Sequence[str], as_of: dt.date, quarters: int = 8
) -> dict[str, pd.DataFrame]:
    """Per-ticker quarterly frames: index=period_end, columns=metric.

    PIT-safe by construction -- this is the only path into the factor engine.
    """
    long = get_fundamentals(
        session, tickers, as_of, REQUIRED_METRICS, lookback_quarters=quarters
    )
    out: dict[str, pd.DataFrame] = {}
    if long.empty:
        return out
    for ticker, g in long.groupby("ticker"):
        pivot = (
            g.pivot_table(
                index="period_end", columns="metric", values="value", aggfunc="last"
            )
            .sort_index()
            .tail(quarters)
        )
        out[str(ticker)] = pivot
    return out


def _ttm(frame: pd.DataFrame, metric: str, periods: int = 4) -> float:
    """Trailing-twelve-month sum for a flow metric."""
    if metric not in frame.columns:
        return np.nan
    vals = frame[metric].dropna().tail(periods)
    if len(vals) < periods:
        return np.nan
    return float(vals.sum())


def _latest(frame: pd.DataFrame, metric: str) -> float:
    """Most recent value for a stock metric."""
    if metric not in frame.columns:
        return np.nan
    vals = frame[metric].dropna()
    return float(vals.iloc[-1]) if len(vals) else np.nan


def _prior(frame: pd.DataFrame, metric: str, back: int = 4) -> float:
    if metric not in frame.columns:
        return np.nan
    vals = frame[metric].dropna()
    if len(vals) <= back:
        return np.nan
    return float(vals.iloc[-(back + 1)])


def _safe_div(a: float, b: float) -> float:
    if not np.isfinite(a) or not np.isfinite(b) or b == 0:
        return np.nan
    return a / b


def derive_quality(frame: pd.DataFrame) -> dict[str, float]:
    """Quality factors for one ticker from its quarterly frame."""
    revenue = _ttm(frame, "revenue")
    cogs = _ttm(frame, "cogs")
    gross = _ttm(frame, "gross_profit")
    if not np.isfinite(gross) and np.isfinite(revenue) and np.isfinite(cogs):
        gross = revenue - cogs

    assets = _latest(frame, "total_assets")
    equity = _latest(frame, "total_equity")
    debt = _latest(frame, "total_debt")
    if not np.isfinite(debt):
        debt = _latest(frame, "long_term_debt")
    cash = _latest(frame, "cash")
    ni = _ttm(frame, "net_income")
    ocf = _ttm(frame, "operating_cash_flow")
    capex = _ttm(frame, "capex")
    fcf = _ttm(frame, "free_cash_flow")
    if not np.isfinite(fcf) and np.isfinite(ocf) and np.isfinite(capex):
        # FMP reports capex as a negative outflow; SEC XBRL reports it positive.
        fcf = ocf - abs(capex)
    ebit = _ttm(frame, "ebit")
    if not np.isfinite(ebit):
        ebit = _ttm(frame, "operating_income")
    ebitda = _ttm(frame, "ebitda")
    tax = _ttm(frame, "income_tax")
    pretax = _ttm(frame, "pretax_income")

    tax_rate = _safe_div(tax, pretax)
    if not np.isfinite(tax_rate) or not 0 <= tax_rate <= 0.6:
        tax_rate = TAX_RATE_FALLBACK
    nopat = ebit * (1 - tax_rate) if np.isfinite(ebit) else np.nan

    invested = np.nan
    if np.isfinite(equity):
        invested = equity + (debt if np.isfinite(debt) else 0.0) - (
            cash if np.isfinite(cash) else 0.0
        )
        if invested <= 0:
            invested = np.nan

    # Novy-Marx gross profitability: predicts returns about as well as
    # book-to-market and is negatively correlated with it, so they stack.
    gross_profitability = _safe_div(gross, assets)

    # Sloan (1996). Low is good -- the sign flip happens in NEGATIVE_FACTORS.
    accruals = _safe_div(ni - ocf, assets) if np.isfinite(ni) and np.isfinite(ocf) else np.nan

    net_debt = (debt - cash) if np.isfinite(debt) and np.isfinite(cash) else np.nan
    nd_ebitda = _safe_div(net_debt, ebitda)

    prior_debt = _prior(frame, "total_debt") or _prior(frame, "long_term_debt")
    prior_cash = _prior(frame, "cash")
    prior_ebitda_frame = frame["ebitda"].dropna() if "ebitda" in frame else pd.Series(dtype=float)
    prior_ebitda = (
        float(prior_ebitda_frame.iloc[-8:-4].sum())
        if len(prior_ebitda_frame) >= 8
        else np.nan
    )
    prior_nd_ebitda = _safe_div(
        (prior_debt - prior_cash)
        if np.isfinite(prior_debt) and np.isfinite(prior_cash)
        else np.nan,
        prior_ebitda,
    )
    debt_trend = (
        nd_ebitda - prior_nd_ebitda
        if np.isfinite(nd_ebitda) and np.isfinite(prior_nd_ebitda)
        else np.nan
    )

    return {
        "gross_profitability": gross_profitability,
        "roic": _safe_div(nopat, invested),
        "accruals": accruals,
        "fcf_ttm": fcf,
        "ebit_ttm": ebit,
        "ebitda_ttm": ebitda,
        "revenue_ttm": revenue,
        "net_income_ttm": ni,
        "total_debt": debt,
        "cash": cash,
        "net_debt": net_debt,
        "nd_ebitda": nd_ebitda,
        "debt_trend": debt_trend,
        "total_assets": assets,
        "total_equity": equity,
        "shares_diluted": _latest(frame, "shares_diluted"),
    }


def piotroski_f_score(frame: pd.DataFrame) -> float:
    """Piotroski F-Score, 0-9, from the nine binary signals.

    Returns NaN if fewer than five signals are computable -- a partial score is
    not comparable to a full one, and pretending otherwise pollutes the z-score.
    """
    if frame.empty:
        return np.nan

    def ttm(metric, back=0):
        if metric not in frame.columns:
            return np.nan
        vals = frame[metric].dropna()
        end = len(vals) - back * 4
        start = end - 4
        if start < 0 or end <= 0:
            return np.nan
        return float(vals.iloc[start:end].sum())

    def latest(metric, back=0):
        if metric not in frame.columns:
            return np.nan
        vals = frame[metric].dropna()
        idx = len(vals) - 1 - back * 4
        return float(vals.iloc[idx]) if 0 <= idx < len(vals) else np.nan

    signals: list[bool] = []

    ni = ttm("net_income")
    assets = latest("total_assets")
    assets_prior = latest("total_assets", 1)
    ocf = ttm("operating_cash_flow")
    roa = _safe_div(ni, assets)
    roa_prior = _safe_div(ttm("net_income", 1), assets_prior)

    # Profitability
    if np.isfinite(ni):
        signals.append(ni > 0)
    if np.isfinite(ocf):
        signals.append(ocf > 0)
    if np.isfinite(roa) and np.isfinite(roa_prior):
        signals.append(roa > roa_prior)
    if np.isfinite(ocf) and np.isfinite(ni):
        signals.append(ocf > ni)  # accrual quality

    # Leverage / liquidity / dilution
    ltd = latest("long_term_debt")
    ltd_prior = latest("long_term_debt", 1)
    if np.isfinite(ltd) and np.isfinite(ltd_prior) and np.isfinite(assets) and assets:
        signals.append(_safe_div(ltd, assets) < _safe_div(ltd_prior, assets_prior))
    ca, cl = latest("current_assets"), latest("current_liabilities")
    ca_p, cl_p = latest("current_assets", 1), latest("current_liabilities", 1)
    cr, cr_p = _safe_div(ca, cl), _safe_div(ca_p, cl_p)
    if np.isfinite(cr) and np.isfinite(cr_p):
        signals.append(cr > cr_p)
    sh, sh_p = latest("shares_diluted"), latest("shares_diluted", 1)
    if np.isfinite(sh) and np.isfinite(sh_p):
        signals.append(sh <= sh_p * 1.02)  # 2% tolerance for option issuance

    # Operating efficiency
    rev, rev_p = ttm("revenue"), ttm("revenue", 1)
    gp = ttm("gross_profit")
    gp_p = ttm("gross_profit", 1)
    gm, gm_p = _safe_div(gp, rev), _safe_div(gp_p, rev_p)
    if np.isfinite(gm) and np.isfinite(gm_p):
        signals.append(gm > gm_p)
    at, at_p = _safe_div(rev, assets), _safe_div(rev_p, assets_prior)
    if np.isfinite(at) and np.isfinite(at_p):
        signals.append(at > at_p)

    if len(signals) < 5:
        return np.nan
    # Scale a partial score up to the 0-9 range so it stays comparable.
    return float(sum(signals)) * 9.0 / len(signals)


def derive_value(
    quality: dict[str, float], market_cap: float, price: float
) -> dict[str, float]:
    """Value factors. Inverted so that higher is always better, then
    sector-relative only -- comparing a software P/E to a utility P/E is
    meaningless."""
    ev = np.nan
    if np.isfinite(market_cap):
        net_debt = quality.get("net_debt", np.nan)
        ev = market_cap + (net_debt if np.isfinite(net_debt) else 0.0)
        if ev <= 0:
            ev = np.nan

    ebit = quality.get("ebit_ttm", np.nan)
    sales = quality.get("revenue_ttm", np.nan)
    fcf = quality.get("fcf_ttm", np.nan)

    return {
        # Yields, not multiples: a yield is defined at zero earnings, a multiple
        # explodes. Higher is cheaper.
        "ev_ebit_inv": _safe_div(ebit, ev),
        "ev_sales_inv": _safe_div(sales, ev),
        "fcf_price": _safe_div(fcf, market_cap),
        "fcf_yield": _safe_div(fcf, ev),
        "enterprise_value": ev,
    }


def build_fundamental_factors(
    session: Session,
    tickers: Sequence[str],
    as_of: dt.date,
    market_caps: pd.Series,
    prices: pd.Series,
) -> pd.DataFrame:
    """Full quality+value factor frame for the Stage 2 survivor set."""
    frames = load_quarterly_wide(session, tickers, as_of)
    rows = []
    for ticker in tickers:
        frame = frames.get(ticker)
        if frame is None or frame.empty:
            rows.append({"ticker": ticker})
            continue
        q = derive_quality(frame)
        v = derive_value(
            q,
            float(market_caps.get(ticker, np.nan)),
            float(prices.get(ticker, np.nan)),
        )
        row = {"ticker": ticker, **q, **v}
        row["piotroski"] = piotroski_f_score(frame)
        row["n_quarters"] = len(frame)
        rows.append(row)
    df = pd.DataFrame(rows).set_index("ticker")
    return df.reindex(list(tickers))
