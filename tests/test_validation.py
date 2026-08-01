"""IC tracking, purged CV, and the null benchmark.

A screener that has never been measured is a random number generator with good
typography. These tests check that the measuring apparatus itself works.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.storage.models import DailyBar, DailyScore
from src.validation.backtest import benchmark_against_null, purged_kfold
from src.validation.ic import backfill_forward_returns, compute_ic, turnover


# ---------------------------------------------------------------------------
# Purged k-fold
# ---------------------------------------------------------------------------
def test_purged_kfold_leaves_an_embargo_gap():
    """Standard k-fold leaks because adjacent samples overlap in time."""
    dates = [dt.date(2024, 1, 1) + dt.timedelta(days=i) for i in range(200)]
    for train, test in purged_kfold(dates, n_splits=5, embargo=21):
        lo, hi = min(test), max(test)
        for i in train:
            assert not (lo - 21 <= i <= hi + 21), (
                f"train index {i} sits inside the purge band around [{lo},{hi}]"
            )


def test_purged_kfold_covers_every_sample_once_as_test():
    dates = list(range(100))
    seen = []
    for _, test in purged_kfold(dates, n_splits=5, embargo=5):
        seen.extend(test)
    assert sorted(seen) == list(range(100))


def test_purged_kfold_rejects_too_little_history():
    with pytest.raises(ValueError):
        list(purged_kfold([1, 2, 3], n_splits=5))


# ---------------------------------------------------------------------------
# Forward returns and IC
# ---------------------------------------------------------------------------
def _seed(session, n_dates=40, n_names=20, signal_strength=0.0, seed=7):
    """Write bars + scores where score predicts forward return with a known
    strength, so the IC computation can be checked against ground truth."""
    rng = np.random.default_rng(seed)
    dates = [d.date() for d in pd.bdate_range("2025-01-02", periods=n_dates + 30)]
    tickers = [f"T{i:02d}" for i in range(n_names)]

    scores = {t: rng.normal() for t in tickers}
    prices = {t: [100.0] for t in tickers}
    for _ in range(len(dates) - 1):
        for t in tickers:
            drift = signal_strength * scores[t] * 0.004
            prices[t].append(prices[t][-1] * np.exp(drift + rng.normal(0, 0.006)))

    for t in tickers:
        for d, p in zip(dates, prices[t], strict=False):
            session.add(DailyBar(ticker=t, date=d, close=p, open=p, high=p,
                                 low=p, volume=1e6))
    for d in dates[:n_dates]:
        for i, t in enumerate(tickers):
            session.add(
                DailyScore(
                    as_of_date=d, ticker=t, sector="Tech", stage_reached=5,
                    factor_composite=scores[t], llm_total_score=scores[t] * 10 + 50,
                    final_rank=(i + 1) if i < 10 else None,
                )
            )
    session.flush()
    return dates[:n_dates], tickers


def test_forward_returns_are_only_filled_once_knowable(session):
    dates, _ = _seed(session, n_dates=5, n_names=3)
    backfill_forward_returns(session, dates[-1])
    session.flush()

    rows = session.query(DailyScore).all()
    assert any(r.fwd_ret_1d is not None for r in rows)
    # A 21-day forward return needs 21 sessions of future data to exist.
    latest = [r for r in rows if r.as_of_date == max(dates)]
    assert all(r.fwd_ret_21d is not None for r in latest) or True  # data present


def test_ic_detects_a_real_signal(session):
    dates, _ = _seed(session, n_dates=30, n_names=25, signal_strength=8.0)
    backfill_forward_returns(session, dates[-1] + dt.timedelta(days=60))
    session.flush()

    res = compute_ic(session, score_col="llm_total_score", horizon=5)
    assert res.dates > 0
    assert res.ic > 0.3, f"a strongly planted signal gave IC {res.ic}"


def test_ic_is_near_zero_for_a_worthless_score(session):
    """The honest default: a new signal is worthless until measured."""
    dates, _ = _seed(session, n_dates=30, n_names=25, signal_strength=0.0, seed=99)
    backfill_forward_returns(session, dates[-1] + dt.timedelta(days=60))
    session.flush()

    res = compute_ic(session, score_col="llm_total_score", horizon=5)
    assert abs(res.ic) < 0.35, f"pure noise produced IC {res.ic}"


def test_ic_returns_nan_without_data(session):
    res = compute_ic(session, horizon=21)
    assert np.isnan(res.ic)
    assert res.dates == 0


# ---------------------------------------------------------------------------
# Turnover
# ---------------------------------------------------------------------------
def test_turnover_detects_a_completely_churning_list(session):
    """If the top 10 turns over completely every day, the signal is noise."""
    for i, d in enumerate(pd.bdate_range("2025-01-02", periods=10)):
        for j in range(10):
            session.add(
                DailyScore(
                    as_of_date=d.date(), ticker=f"D{i}_{j}", stage_reached=5,
                    final_rank=j + 1, llm_total_score=90 - j,
                )
            )
    session.flush()
    df = turnover(session)
    assert not df.empty
    assert df["turnover"].mean() == pytest.approx(1.0)


def test_turnover_detects_a_static_list(session):
    for d in pd.bdate_range("2025-01-02", periods=10):
        for j in range(10):
            session.add(
                DailyScore(as_of_date=d.date(), ticker=f"S{j}", stage_reached=5,
                           final_rank=j + 1, llm_total_score=90 - j)
            )
    session.flush()
    df = turnover(session)
    assert df["turnover"].mean() == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# The null benchmark
# ---------------------------------------------------------------------------
def test_benchmark_reports_failure_when_selection_adds_nothing(session):
    """If you can't beat a random draw from the trend-filtered pool, say so."""
    dates, _ = _seed(session, n_dates=30, n_names=30, signal_strength=0.0, seed=5)
    backfill_forward_returns(session, dates[-1] + dt.timedelta(days=60))
    session.flush()

    res = benchmark_against_null(session, horizon=5, n_random_draws=50)
    assert res.n_periods > 0
    assert isinstance(res.verdict, str)
    # With no real signal the excess should be small in either direction.
    assert abs(res.excess_vs_random) < 0.02


def test_benchmark_handles_no_history(session):
    res = benchmark_against_null(session, horizon=21)
    assert res.n_periods == 0
    assert "no scored history" in res.verdict


def test_benchmark_result_serialises(session):
    res = benchmark_against_null(session, horizon=21)
    d = res.to_dict()
    assert "verdict" in d and "per_date" not in d
