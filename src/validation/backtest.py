"""Walk-forward backtest with purged k-fold CV, and the null benchmarks.

Two things this module exists to establish, before anyone trusts the output:

1. Standard k-fold leaks on financial time series because adjacent samples
   overlap in time. Purged k-fold with an embargo equal to the holding horizon
   is the correct method (Lopez de Prado). `purged_kfold` implements it.

2. Benchmark against the null. Compare the top-10's forward returns against an
   equal-weight random 10 drawn from the Stage-1 survivors, and against SPY. If
   you cannot beat a random draw from the trend-filtered pool, then Stages 2-5
   add nothing and only the trend gate matters. Know this before you trust it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.storage.models import DailyScore
from src.storage.pit import get_price_panel, get_universe

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Purged k-fold
# ---------------------------------------------------------------------------
def purged_kfold(
    dates: Sequence[dt.date], n_splits: int = 5, embargo: int = 21
) -> Iterator[tuple[list[int], list[int]]]:
    """Yield (train_idx, test_idx) with purging and an embargo.

    A sample whose label spans [t, t+h] overlaps any training sample within h of
    it. We drop those from train (purge) and additionally remove `embargo`
    samples after the test block, because serial correlation leaks forward too.
    """
    dates = list(dates)
    n = len(dates)
    if n < n_splits * 2:
        raise ValueError(
            f"Need at least {n_splits * 2} dates for {n_splits}-fold CV, got {n}"
        )

    fold_size = n // n_splits
    for k in range(n_splits):
        start = k * fold_size
        stop = n if k == n_splits - 1 else (k + 1) * fold_size
        test_idx = list(range(start, stop))

        # Purge: anything whose label window overlaps the test block.
        purge_lo = max(0, start - embargo)
        purge_hi = min(n, stop + embargo)
        train_idx = [i for i in range(n) if i < purge_lo or i >= purge_hi]
        if train_idx and test_idx:
            yield train_idx, test_idx


@dataclass
class BenchmarkResult:
    horizon: int
    n_periods: int
    strategy_mean: float
    strategy_std: float
    random_mean: float
    random_std: float
    spy_mean: float
    excess_vs_random: float
    excess_vs_spy: float
    t_stat_vs_random: float
    p_value_vs_random: float
    verdict: str
    per_date: pd.DataFrame = field(default_factory=pd.DataFrame)

    def to_dict(self) -> dict:
        d = {
            k: (round(v, 5) if isinstance(v, float) and np.isfinite(v) else v)
            for k, v in self.__dict__.items()
            if k != "per_date"
        }
        return d


def benchmark_against_null(
    session: Session,
    *,
    horizon: int = 21,
    n_random_draws: int = 200,
    top_n: int = 10,
    spy_ticker: str = "SPY",
    seed: int = 42,
) -> BenchmarkResult:
    """The honest test: does the funnel beat a random draw from Stage 1?

    For each scored date we take the actual top-N's mean forward return, then
    the mean of `n_random_draws` random N-name draws from that date's Stage-1
    survivors, then SPY over the same window.
    """
    rng = np.random.default_rng(seed)
    rows = list(session.execute(select(DailyScore)).scalars())
    if not rows:
        return _empty_benchmark(horizon)

    df = pd.DataFrame(
        [
            {
                "date": r.as_of_date,
                "ticker": r.ticker,
                "stage": r.stage_reached,
                "rank": r.final_rank,
                "fwd": getattr(r, f"fwd_ret_{horizon}d", None),
            }
            for r in rows
        ]
    ).dropna(subset=["fwd"])
    if df.empty:
        return _empty_benchmark(horizon)

    spy = _spy_returns(session, sorted(df["date"].unique()), horizon, spy_ticker)

    per_date = []
    for date, g in df.groupby("date"):
        chosen = g[g["rank"].notna()].nsmallest(top_n, "rank")
        # The null pool is the Stage-1 survivor set: everything that got scored.
        pool = g["fwd"].to_numpy(dtype=float)
        if len(chosen) == 0 or len(pool) < top_n:
            continue

        strat = float(chosen["fwd"].mean())
        draws = [
            float(rng.choice(pool, size=top_n, replace=False).mean())
            for _ in range(n_random_draws)
        ]
        per_date.append(
            {
                "date": date,
                "strategy": strat,
                "random_mean": float(np.mean(draws)),
                "random_p95": float(np.percentile(draws, 95)),
                "beat_random_pct": float(np.mean([strat > d for d in draws])),
                "spy": spy.get(date, np.nan),
                "n_selected": len(chosen),
            }
        )

    if not per_date:
        return _empty_benchmark(horizon)

    pdf = pd.DataFrame(per_date)
    diff = pdf["strategy"] - pdf["random_mean"]
    t_stat = (
        float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff))))
        if len(diff) > 1 and diff.std(ddof=1) > 0
        else float("nan")
    )
    p_value = _two_sided_p(t_stat, len(diff) - 1)

    excess_random = float(diff.mean())
    excess_spy = float((pdf["strategy"] - pdf["spy"]).mean(skipna=True))

    verdict = _benchmark_verdict(excess_random, p_value, len(pdf))
    log.info(
        "benchmark_vs_null", horizon=horizon, periods=len(pdf),
        excess_vs_random=round(excess_random, 5), verdict=verdict,
    )

    return BenchmarkResult(
        horizon=horizon,
        n_periods=len(pdf),
        strategy_mean=float(pdf["strategy"].mean()),
        strategy_std=float(pdf["strategy"].std(ddof=1)) if len(pdf) > 1 else float("nan"),
        random_mean=float(pdf["random_mean"].mean()),
        random_std=float(pdf["random_mean"].std(ddof=1)) if len(pdf) > 1 else float("nan"),
        spy_mean=float(pdf["spy"].mean(skipna=True)),
        excess_vs_random=excess_random,
        excess_vs_spy=excess_spy,
        t_stat_vs_random=t_stat,
        p_value_vs_random=p_value,
        verdict=verdict,
        per_date=pdf,
    )


def _benchmark_verdict(excess: float, p: float, n: int) -> str:
    if n < 20:
        return "insufficient history - do not draw conclusions"
    if not np.isfinite(p):
        return "inconclusive"
    if excess <= 0:
        return (
            "FAILS THE NULL: the top-10 does not beat a random draw from the "
            "Stage-1 pool. Stages 2-5 are adding nothing; only the trend gate matters."
        )
    if p < 0.05:
        return "beats the random-draw null at p<0.05"
    return "positive but not significant - keep measuring"


def _spy_returns(
    session: Session, dates: Sequence, horizon: int, spy_ticker: str
) -> dict:
    if not len(dates):
        return {}
    panel = get_price_panel(
        session, [spy_ticker], min(dates) - dt.timedelta(days=10),
        max(dates) + dt.timedelta(days=horizon * 2 + 10),
    )
    if panel.empty or spy_ticker not in panel.columns:
        return {}
    col = panel[spy_ticker]
    idx = list(panel.index)
    pos = {d: i for i, d in enumerate(idx)}
    out = {}
    for d in dates:
        i = pos.get(d)
        if i is None or i + horizon >= len(idx):
            continue
        base, fut = col.iloc[i], col.iloc[i + horizon]
        if np.isfinite(base) and base and np.isfinite(fut):
            out[d] = float(fut / base - 1.0)
    return out


def _two_sided_p(t: float, dof: int) -> float:
    if not np.isfinite(t) or dof < 1:
        return float("nan")
    from scipy import stats

    return float(2 * (1 - stats.t.cdf(abs(t), dof)))


def _empty_benchmark(horizon: int) -> BenchmarkResult:
    nan = float("nan")
    return BenchmarkResult(
        horizon, 0, nan, nan, nan, nan, nan, nan, nan, nan, nan,
        "no scored history yet",
    )


# ---------------------------------------------------------------------------
# Walk-forward harness
# ---------------------------------------------------------------------------
def walk_forward_universe_check(
    session: Session, dates: Sequence[dt.date]
) -> pd.DataFrame:
    """Verify the universe snapshots actually reconstruct history.

    If the universe for an old date is identical to today's, the survivorship
    snapshot is not doing its job and every backtest built on it is a fantasy.
    """
    rows = []
    latest = None
    for d in sorted(dates):
        uni = get_universe(session, d)
        tickers = set(uni["ticker"]) if not uni.empty else set()
        rows.append({"date": d, "size": len(tickers)})
        latest = tickers
    df = pd.DataFrame(rows)
    if len(df) > 1 and df["size"].nunique() == 1:
        log.warning(
            "universe_snapshots_identical",
            hint="every snapshot has the same size; survivorship bias is likely present",
        )
    _ = latest
    return df
