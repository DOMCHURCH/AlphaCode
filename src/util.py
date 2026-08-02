"""Small shared helpers with no dependency on the rest of the app."""

from __future__ import annotations

from typing import Any

import pandas as pd


def or_default(value: Any, default: Any) -> Any:
    """`value or default`, but NaN-safe.

    A value read from a pandas Series/DataFrame comes back as NaN for a missing
    cell, and NaN is TRUTHY -- so the common idiom `series.get(k) or default`
    never substitutes the default, and a NaN leaks downstream: as a dict key, or
    as ``str(nan) == "nan"``, or through ``float(nan)``. This returns `default`
    for None, NaN/NaT, and the falsy values `or` already covered ("", 0, False),
    and otherwise the value. Use it for ANY Series/DataFrame-derived value that
    has a fallback -- `x.get(k) or d` on a pandas object is a bug without it.
    """
    if value is None:
        return default
    try:
        if bool(pd.isna(value)):
            return default
    except (TypeError, ValueError):
        return value  # non-scalar (list/array/Series) -> treat as present
    return value if value else default
