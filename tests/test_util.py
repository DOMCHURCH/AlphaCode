"""The NaN-safe `or` helper -- the fix for the most common pandas trap here."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.util import or_default


def test_or_default_substitutes_for_nan():
    # The bug this exists for: NaN is truthy, so `nan or default` returns NaN.
    assert or_default(np.nan, "Unknown") == "Unknown"
    assert or_default(float("nan"), "Unknown") == "Unknown"
    assert or_default(pd.NA, "Unknown") == "Unknown"


def test_or_default_matches_plain_or_for_normal_falsy():
    assert or_default(None, "d") == "d"
    assert or_default("", "d") == "d"
    assert or_default(0, "d") == "d"
    assert or_default(False, "d") == "d"


def test_or_default_passes_real_values_through():
    assert or_default("Energy", "Unknown") == "Energy"
    assert or_default(3.5, 0) == 3.5


def test_or_default_on_a_series_get():
    # The exact call shape from the codebase: Series.get on a missing key/NaN.
    s = pd.Series({"AAPL": "Technology", "XYZ": np.nan})
    assert or_default(s.get("AAPL"), "Unknown") == "Technology"
    assert or_default(s.get("XYZ"), "Unknown") == "Unknown"   # NaN cell
    assert or_default(s.get("MISSING"), "Unknown") == "Unknown"  # absent key


def test_or_default_leaves_nonscalars_present():
    assert or_default([1, 2], "d") == [1, 2]
    assert or_default({"a": 1}, "d") == {"a": 1}
