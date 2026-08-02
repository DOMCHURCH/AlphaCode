"""Stage 2 completeness gate + per-factor coverage.

A composite scored on mostly-NaN factors is not a defensible ranking, so the run
must abort with the per-factor breakdown rather than ship it. This was the run at
15.7% mean completeness (no FINNHUB revisions, fundamentals not joining)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.factors.composite import ALL_FACTORS, score_composite
from src.pipeline import DataQualityError, _completeness_gate


def _raw(present: list[str], n: int = 30) -> pd.DataFrame:
    idx = [f"T{i}" for i in range(n)]
    raw = pd.DataFrame(index=idx)
    raw["sector"] = "Information Technology"
    raw["sector_source"] = "sic"
    for f in ALL_FACTORS:
        raw[f] = np.nan
    for f in present:
        raw[f] = np.linspace(1.0, 2.0, n)
    return raw


def test_factor_coverage_reflects_nan_fraction():
    # Only one factor populated -> most factors 0 coverage, low mean completeness.
    raw = _raw(present=[ALL_FACTORS[0]])
    res = score_composite(raw)
    assert res.scores["data_completeness"].mean() < 0.4


def test_gate_aborts_on_low_completeness_and_names_empty_factors():
    raw = _raw(present=[ALL_FACTORS[0]])
    res = score_composite(raw)
    res.factor_coverage = {
        f: (1.0 if f == ALL_FACTORS[0] else 0.0) for f in ALL_FACTORS
    }
    with pytest.raises(DataQualityError) as ei:
        _completeness_gate(res, 0.4)
    msg = str(ei.value)
    assert "completeness" in msg.lower()
    # The empty factors are named so the operator can see WHAT is missing.
    assert ALL_FACTORS[1] in msg
    assert "not tuning the floor down" in msg


def test_gate_message_splits_free_vs_paid_gaps():
    """The operator must be able to tell a fixable data gap (SEC XBRL, free) from
    a spend decision (paid feed) at a glance."""
    raw = _raw(present=["mom_12_1"])
    res = score_composite(raw)
    res.factor_coverage = {
        "mom_12_1": 1.0,
        "roic": 0.0, "fcf_yield": 0.0,          # SEC-suppliable, free
        "eps_rev_4w": 0.0, "reco_trend_delta": 0.0,  # paid only
    }
    with pytest.raises(DataQualityError) as ei:
        _completeness_gate(res, 0.4)
    msg = str(ei.value)
    assert "FREE" in msg and "roic" in msg and "fcf_yield" in msg
    assert "PAID" in msg and "eps_rev_4w" in msg and "reco_trend_delta" in msg
    assert "fundamentals=true" in msg  # tells them the free fix


def test_gate_passes_when_data_is_present():
    raw = _raw(present=ALL_FACTORS)  # every factor has a value
    res = score_composite(raw)
    res.factor_coverage = dict.fromkeys(ALL_FACTORS, 1.0)
    mean_comp = _completeness_gate(res, 0.4)
    assert mean_comp >= 0.4  # no raise
