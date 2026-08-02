"""Stooq reconciliation — the split-adjustment heuristic (the one piece that is
pure and testable offline). Symbology/coverage/recency need real loaded data and
run on the deploy via `python -m src.reconcile`."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from src.reconcile import detect_split_adjustment, detect_split_adjustment_detailed


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


def test_detailed_reasoning_for_a_reverse_split():
    """AERT is a reverse-split candidate; the detail must carry the numbers a
    human needs to judge whether it's a genuine gap or a detector artifact."""
    # 1-for-10 reverse (ratio 0.1): unadjusted history jumps ~10x at the ex-date.
    close = _series([0.5] * 10 + [5.0] * 10)
    verdict, detail = detect_split_adjustment_detailed(close, [(dt.date(2024, 1, 11), 0.1)])
    assert verdict == "unadjusted"
    d = detail[0]
    assert d["kind"] == "reverse"
    assert d["ratio"] == 0.1
    assert d["expected_if_unadjusted"] == 10.0  # 1/0.1
    assert abs(d["observed_ratio"] - 10.0) < 0.5
    assert d["price_before"] == 0.5 and d["price_after"] == 5.0
    assert "UNADJUSTED" in d["reasoning"] and "reverse" in d["reasoning"]


def test_detailed_marks_no_data_when_split_outside_window():
    close = _series([10.0] * 10)  # all data BEFORE the ex-date
    verdict, detail = detect_split_adjustment_detailed(close, [(dt.date(2024, 6, 1), 2.0)])
    assert verdict == "inconclusive"
    assert detail[0]["verdict"] == "no_data"


def test_adjusted_reverse_split_is_not_flagged():
    """An adjusted reverse-split series is continuous — must NOT read unadjusted
    (the false-positive shape the detail exists to expose)."""
    close = _series([5.0] * 20)  # already scaled: no jump across the ex-date
    verdict, detail = detect_split_adjustment_detailed(close, [(dt.date(2024, 1, 11), 0.1)])
    assert verdict == "adjusted"
    assert abs(detail[0]["observed_ratio"] - 1.0) < 0.15


def test_reconcile_samples_names_that_actually_split(tmp_path, monkeypatch):
    """The rate must be measured on names with a known split (from FMP's bulk
    calendar), not a random draw — and a random control is reported beside it."""
    import asyncio

    from src.config.settings import get_settings
    from src.ingest import fmp, sec_edgar, yahoo
    from src.reconcile import reconcile
    from src.storage.db import init_db, reset_engine_cache, session_scope
    from src.storage.models import DailyBar

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'rec.db'}")
    monkeypatch.setenv("FMP_API_KEY", "fk")
    monkeypatch.setenv("POLYGON_API_KEY", "")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        as_of = dt.date(2024, 6, 1)
        ex = dt.date(2024, 3, 1)
        with session_scope() as s:
            # AERT: 1-for-10 reverse, UNADJUSTED (0.5 -> 5.0 at the ex-date).
            for i in range(60):
                s.add(DailyBar(ticker="AERT", date=ex - dt.timedelta(days=60 - i),
                               open=0.5, high=0.5, low=0.5, close=0.5, volume=1e6))
            for i in range(60):
                s.add(DailyBar(ticker="AERT", date=ex + dt.timedelta(days=i),
                               open=5, high=5, low=5, close=5.0, volume=1e6))
            # GOOD: 2:1 forward, ADJUSTED (flat, no jump).
            for i in range(120):
                s.add(DailyBar(ticker="GOOD", date=ex - dt.timedelta(days=60) + dt.timedelta(days=i),
                               open=50, high=50, low=50, close=50.0, volume=1e6))
            # CTRL: no split -> falls into the random control group.
            for i in range(120):
                s.add(DailyBar(ticker="CTRL", date=ex - dt.timedelta(days=60) + dt.timedelta(days=i),
                               open=20, high=20, low=20, close=20.0, volume=1e6))

        async def fake_tickers():
            return [{"ticker": t, "cik": str(i)} for i, t in enumerate(("AERT", "GOOD", "CTRL"))]

        async def fake_splits(start, end):
            return [{"ticker": "AERT", "date": ex, "ratio": 0.1},
                    {"ticker": "GOOD", "date": ex, "ratio": 2.0}]

        monkeypatch.setattr(sec_edgar, "fetch_company_tickers", fake_tickers)
        monkeypatch.setattr(fmp, "fetch_stock_splits", fake_splits)
        monkeypatch.setattr(yahoo, "fetch_corporate_actions", lambda *a, **k: [])

        rep = asyncio.run(reconcile(sample=25, as_of=as_of))
        adj = rep["adjustment"]

        assert adj["splits_source"] == "fmp"
        assert adj["tested_with_known_splits"] == 2  # only the two that split
        assert adj["verdicts"]["AERT"] == "unadjusted"
        assert adj["verdicts"]["GOOD"] == "adjusted"
        assert adj["unadjusted_names"] == ["AERT"]
        assert adj["summary"]["unadjusted"] == 1 and adj["summary"]["adjusted"] == 1
        # The detail carries the numbers to judge AERT's reverse-split case.
        d = adj["details"]["AERT"][0]
        assert d["kind"] == "reverse" and d["price_before"] == 0.5 and d["price_after"] == 5.0
        # Random control ran on the non-split name and is reported separately.
        assert adj["control"]["tested"] == 1
        assert adj["control"]["verdicts"].get("CTRL") == "inconclusive"
    finally:
        get_settings.cache_clear()
        reset_engine_cache()
