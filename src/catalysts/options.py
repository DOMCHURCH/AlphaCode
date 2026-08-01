"""Options positioning signals from the Polygon snapshot.

Four numbers, all relative to the name's own history where possible:
  IV rank            current IV vs trailing 52w range
  put/call OI ratio  vs its own 60-day baseline
  25-delta skew      put IV minus call IV
  unusual OI buildup at strikes above spot
"""

from __future__ import annotations

from typing import Any

import numpy as np
import structlog

from src.config.factor_weights import CATALYST_POINTS

log = structlog.get_logger(__name__)


def iv_rank(current_iv: float | None, history: list[float] | None) -> float | None:
    """Where current IV sits in its trailing range, 0-100.

    Without a stored history this returns None rather than a fabricated number.
    The pipeline backfills `iv_mean` daily so the rank becomes meaningful after
    a few weeks of runs.
    """
    if current_iv is None or not history:
        return None
    lo, hi = min(history), max(history)
    if hi <= lo:
        return None
    return float(np.clip((current_iv - lo) / (hi - lo) * 100.0, 0, 100))


def score_options(
    snapshot: dict[str, Any],
    *,
    iv_history: list[float] | None = None,
    pc_baseline: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Score positioning. Returns (points, detail). Empty snapshot scores zero."""
    if not snapshot:
        return 0.0, {}

    points = 0.0
    notes: list[str] = []

    rank = iv_rank(snapshot.get("iv_mean"), iv_history)
    if rank is not None and rank < 40:
        # Cheap optionality ahead of a catalyst is a mild positive.
        points += CATALYST_POINTS["options_iv_rank_low"]
        notes.append(f"IV rank {rank:.0f}")

    skew = snapshot.get("skew_25d")
    if skew is not None and skew < 0:
        # Calls bid over puts: positioning is leaning long.
        points += CATALYST_POINTS["options_call_skew"]
        notes.append("call-skewed")

    pc = snapshot.get("put_call_oi_ratio")
    if pc is not None and pc_baseline and pc < pc_baseline * 0.8:
        points += CATALYST_POINTS["options_call_skew"] * 0.5
        notes.append("P/C OI below baseline")

    upside = snapshot.get("upside_oi_share")
    if upside is not None and upside > 0.55:
        points += CATALYST_POINTS["options_unusual_oi_upside"]
        notes.append(f"{upside:.0%} OI above spot")

    return points, {
        "iv_rank": rank,
        "iv_mean": snapshot.get("iv_mean"),
        "put_call_oi_ratio": pc,
        "skew_25d": skew,
        "upside_oi_share": upside,
        "notes": notes,
    }
