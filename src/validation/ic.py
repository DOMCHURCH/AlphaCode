"""Information Coefficient tracking.

A screener that has never been measured is a random number generator with good
typography.

Every day we store the final scores. Once the forward returns exist we compute
the Spearman rank correlation between score and forward 1d/5d/21d return.

  21-day IC above 0.03 sustained  -> a real signal
  below 0.02                      -> dead weight, cut the weight

Weights are re-fit quarterly, never daily. Daily re-fitting overfits to noise.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import structlog
from scipy import stats
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.factor_weights import CATEGORY_WEIGHTS, MODE_FULL
from src.storage.models import DailyScore
from src.storage.pit import get_price_panel

log = structlog.get_logger(__name__)

HORIZONS = (1, 5, 21)
IC_ALIVE = 0.03
IC_DEAD = 0.02


@dataclass
class ICResult:
    horizon: int
    ic: float
    n: int
    p_value: float
    dates: int


def backfill_forward_returns(
    session: Session, as_of: dt.date, *, horizons: Sequence[int] = HORIZONS
) -> int:
    """Fill fwd_ret_* on stored scores once the returns exist.

    Called on every run for prior dates -- a 21-day return is knowable 21
    sessions after the score was made, not before.
    """
    rows = list(
        session.execute(
            select(DailyScore).where(DailyScore.as_of_date <= as_of)
        ).scalars()
    )
    pending = [
        r for r in rows if any(getattr(r, f"fwd_ret_{h}d") is None for h in horizons)
    ]
    if not pending:
        return 0

    tickers = sorted({r.ticker for r in pending})
    start = min(r.as_of_date for r in pending)
    panel = get_price_panel(session, tickers, start, as_of)
    if panel.empty:
        return 0

    dates = list(panel.index)
    pos = {d: i for i, d in enumerate(dates)}
    updated = 0

    for r in pending:
        i = pos.get(r.as_of_date)
        if i is None or r.ticker not in panel.columns:
            continue
        col = panel[r.ticker]
        base = col.iloc[i]
        if not np.isfinite(base) or base == 0:
            continue
        for h in horizons:
            if getattr(r, f"fwd_ret_{h}d") is not None:
                continue
            j = i + h
            if j >= len(dates):
                continue  # not knowable yet
            fut = col.iloc[j]
            if np.isfinite(fut):
                setattr(r, f"fwd_ret_{h}d", float(fut / base - 1.0))
                updated += 1
    log.info("forward_returns_backfilled", updated=updated)
    return updated


def compute_ic(
    session: Session,
    *,
    score_col: str = "llm_total_score",
    horizon: int = 21,
    since: dt.date | None = None,
    min_names: int = 5,
    mode: str = MODE_FULL,
) -> ICResult:
    """Spearman rank correlation of score vs forward return, pooled by date.

    We compute the IC per date and then average, rather than pooling all
    observations. Pooling would let a single day with many names dominate, and
    would conflate cross-sectional skill with time-series drift.
    """
    stmt = select(DailyScore).where(DailyScore.mode == mode)
    if since:
        stmt = stmt.where(DailyScore.as_of_date >= since)
    rows = list(session.execute(stmt).scalars())
    if not rows:
        return ICResult(horizon, float("nan"), 0, float("nan"), 0)

    df = pd.DataFrame(
        [
            {
                "date": r.as_of_date,
                "ticker": r.ticker,
                "score": getattr(r, score_col, None),
                "fwd": getattr(r, f"fwd_ret_{horizon}d", None),
            }
            for r in rows
        ]
    ).dropna(subset=["score", "fwd"])

    if df.empty:
        return ICResult(horizon, float("nan"), 0, float("nan"), 0)

    ics, ns = [], 0
    for _, g in df.groupby("date"):
        if len(g) < min_names:
            continue
        rho, _ = stats.spearmanr(g["score"], g["fwd"])
        if np.isfinite(rho):
            ics.append(float(rho))
            ns += len(g)

    if not ics:
        return ICResult(horizon, float("nan"), 0, float("nan"), 0)

    mean_ic = float(np.mean(ics))
    # t-stat on the series of daily ICs -- the standard IC significance test.
    t = mean_ic / (np.std(ics, ddof=1) / np.sqrt(len(ics))) if len(ics) > 1 else np.nan
    p = float(2 * (1 - stats.t.cdf(abs(t), len(ics) - 1))) if np.isfinite(t) else np.nan

    return ICResult(horizon, mean_ic, ns, p, len(ics))


def factor_decay_analysis(
    session: Session, *, horizon: int = 21, since: dt.date | None = None,
    mode: str = MODE_FULL,
) -> pd.DataFrame:
    """IC for each individual factor category.

    Drop or downweight the ones that don't earn their place. The output feeds
    the quarterly re-fit; it is deliberately not wired to change weights
    automatically.
    """
    stmt = select(DailyScore).where(DailyScore.mode == mode)
    if since:
        stmt = stmt.where(DailyScore.as_of_date >= since)
    rows = list(session.execute(stmt).scalars())
    if not rows:
        return pd.DataFrame()

    records = []
    for r in rows:
        fwd = getattr(r, f"fwd_ret_{horizon}d", None)
        if fwd is None:
            continue
        detail = r.factor_detail or {}
        rec = {"date": r.as_of_date, "fwd": fwd, "composite": r.factor_composite}
        rec.update({k: detail.get(k) for k in CATEGORY_WEIGHTS})
        records.append(rec)

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    out = []
    for factor in list(CATEGORY_WEIGHTS) + ["composite"]:
        if factor not in df.columns:
            continue
        ics = []
        for _, g in df.groupby("date"):
            sub = g[[factor, "fwd"]].dropna()
            if len(sub) < 5:
                continue
            rho, _ = stats.spearmanr(sub[factor], sub["fwd"])
            if np.isfinite(rho):
                ics.append(rho)
        if not ics:
            continue
        mean_ic = float(np.mean(ics))
        out.append(
            {
                "factor": factor,
                "ic": mean_ic,
                "ic_std": float(np.std(ics, ddof=1)) if len(ics) > 1 else np.nan,
                "ir": mean_ic / np.std(ics, ddof=1) if len(ics) > 1 else np.nan,
                "days": len(ics),
                "verdict": _verdict(mean_ic),
                "current_weight": CATEGORY_WEIGHTS.get(factor),
            }
        )
    return pd.DataFrame(out).sort_values("ic", ascending=False)


def _verdict(ic: float) -> str:
    if ic >= IC_ALIVE:
        return "alive"
    if ic < IC_DEAD:
        return "dead weight - cut"
    return "marginal"


def turnover(
    session: Session, *, days: int = 30, mode: str = MODE_FULL
) -> pd.DataFrame:
    """Day-over-day turnover of the final top-10.

    If the list turns over completely every day the signal is noise. Healthy
    turnover for a daily-rebalanced momentum system is roughly 20-40%/day.
    """
    rows = list(
        session.execute(
            select(DailyScore)
            .where(DailyScore.final_rank.isnot(None))
            .where(DailyScore.mode == mode)
        ).scalars()
    )
    if not rows:
        return pd.DataFrame()

    by_date: dict[dt.date, set[str]] = {}
    for r in rows:
        by_date.setdefault(r.as_of_date, set()).add(r.ticker)

    dates = sorted(by_date)[-days:]
    out = []
    for prev, cur in zip(dates, dates[1:], strict=False):
        a, b = by_date[prev], by_date[cur]
        if not b:
            continue
        out.append(
            {
                "date": cur,
                "turnover": len(b - a) / len(b),
                "held": len(a & b),
                "new": len(b - a),
            }
        )
    df = pd.DataFrame(out)
    if not df.empty:
        mean = float(df["turnover"].mean())
        verdict = (
            "healthy" if 0.2 <= mean <= 0.4
            else ("too sticky" if mean < 0.2 else "noise-level churn")
        )
        log.info("turnover", mean=round(mean, 3), verdict=verdict, days=len(df))
    return df


def ic_report(
    session: Session, since: dt.date | None = None, mode: str = MODE_FULL
) -> dict[str, Any]:
    """Everything the API's /validation endpoint returns.

    Scoped to ONE `mode`. A momentum-only score (3 factors) and a full-composite
    score (19) are different quantities; pooling them would measure neither, so
    every query below filters on mode and the result says which one it is.
    """
    out: dict[str, Any] = {"horizons": {}, "mode": mode}
    for h in HORIZONS:
        for col in ("llm_total_score", "factor_composite"):
            res = compute_ic(session, score_col=col, horizon=h, since=since, mode=mode)
            out["horizons"].setdefault(str(h), {})[col] = {
                "ic": None if np.isnan(res.ic) else round(res.ic, 4),
                "n_obs": res.n,
                "n_days": res.dates,
                "p_value": None if np.isnan(res.p_value) else round(res.p_value, 4),
                "verdict": _verdict(res.ic) if np.isfinite(res.ic) else "insufficient data",
            }
    decay = factor_decay_analysis(session, since=since, mode=mode)
    out["factor_decay"] = decay.to_dict(orient="records") if not decay.empty else []
    to = turnover(session, mode=mode)
    out["turnover"] = {
        "mean": round(float(to["turnover"].mean()), 3) if not to.empty else None,
        "series": to.to_dict(orient="records") if not to.empty else [],
    }

    # Survivorship-bias self-check: if every historical universe snapshot has the
    # same size, the snapshots are probably not reconstructing history and every
    # backtest built on them is a fantasy. This is the one guardrail the whole
    # PIT design exists to protect, so it belongs in the validation report.
    from src.storage.repository import iter_score_dates
    from src.validation.backtest import walk_forward_universe_check

    dates = list(iter_score_dates(session))
    if len(dates) >= 2:
        surv = walk_forward_universe_check(session, dates)
        identical = len(surv) > 1 and surv["size"].nunique() == 1
        out["survivorship_check"] = {
            "n_dates": int(len(surv)),
            "distinct_sizes": int(surv["size"].nunique()) if not surv.empty else 0,
            "warning": (
                "universe snapshots are identical in size across dates — likely "
                "survivorship bias; backtests over this window are unreliable"
                if identical else None
            ),
        }
    else:
        out["survivorship_check"] = {
            "n_dates": len(dates),
            "note": "need >=2 dated universe snapshots to check",
        }
    return out
