"""Stooq reconciliation — the split-adjustment heuristic (the one piece that is
pure and testable offline). Symbology/coverage/recency need real loaded data and
run on the deploy via `python -m src.reconcile`."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from src.reconcile import detect_split_adjustment


def _series(prices: list[float], start=dt.date(2024, 1, 1)) -> pd.Series:
    idx = [start + dt.timedelta(days=i) for i in range(len(prices))]
    return pd.Series(prices, index=idx, dtype=float)


def test_detects_unadjusted_series():
    # 10 days at ~200, then a 4:1 split -> price drops to ~50 (UNADJUSTED).
    close = _series([200.0] * 10 + [50.0] * 10)
    split = [(dt.date(2024, 1, 11), 4.0)]
    assert detect_split_adjustment(close, split) == "unadjusted"


def test_detects_adjusted_series():
    # Split-adjusted: the historical prices are already divided, so no jump.
    close = _series([50.0] * 20)
    split = [(dt.date(2024, 1, 11), 4.0)]
    assert detect_split_adjustment(close, split) == "adjusted"


def test_inconclusive_without_split_or_data():
    assert detect_split_adjustment(_series([10.0] * 5), []) == "inconclusive"
    assert detect_split_adjustment(pd.Series(dtype=float), [(dt.date(2024, 1, 2), 2.0)]) == "inconclusive"


def test_one_unadjusted_split_dominates():
    # A messy series with one clearly-unadjusted split must flag unadjusted.
    close = _series([100.0] * 10 + [50.0] * 10)  # 2:1 unadjusted
    splits = [(dt.date(2024, 1, 11), 2.0), (dt.date(2023, 1, 1), 3.0)]  # 2nd out of range
    assert detect_split_adjustment(close, splits) == "unadjusted"


def test_ignores_noise_that_isnt_a_split():
    rng = np.random.default_rng(0)
    close = _series(list(100 + rng.normal(0, 0.5, 20)))
    assert detect_split_adjustment(close, [(dt.date(2024, 1, 11), 2.0)]) in {"adjusted", "inconclusive"}
