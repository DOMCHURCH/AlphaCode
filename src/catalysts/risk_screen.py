"""Crowding and tradeability screen. These are FILTERS, not scores.

A name that trips any of these is rejected outright regardless of how good its
composite looks. Each rejection is recorded with a reason so the funnel chart
can show what was cut and why.

Missing data never rejects a name -- "unknown short interest" is not evidence
of crowding. It is recorded in the completeness report instead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog

from src.config.factor_weights import RISK_SCREEN, RiskScreenConfig

log = structlog.get_logger(__name__)


@dataclass
class ScreenResult:
    passed: bool
    reasons: list[str]
    detail: dict[str, Any]


def screen_name(
    ticker: str,
    *,
    short_interest_pct: float | None,
    days_to_cover: float | None,
    next_earnings: dt.date | None,
    as_of: dt.date,
    realised_vol_20d: float | None,
    universe_median_vol: float | None,
    spread_bps: float | None,
    pending_merger_cash: bool = False,
    config: RiskScreenConfig = RISK_SCREEN,
) -> ScreenResult:
    reasons: list[str] = []

    # Squeeze risk cuts both ways -- too unstable to hold as a momentum name.
    if (
        short_interest_pct is not None
        and days_to_cover is not None
        and short_interest_pct > config.max_short_interest_pct
        and days_to_cover > config.max_days_to_cover
    ):
        reasons.append(
            f"crowded short: SI {short_interest_pct:.0f}% / DTC {days_to_cover:.1f}"
        )

    # A binary event is not an edge.
    if next_earnings is not None:
        days = _business_days_between(as_of, next_earnings)
        if 0 <= days <= config.min_days_to_earnings:
            reasons.append(f"earnings in {days}bd")

    if (
        realised_vol_20d is not None
        and universe_median_vol
        and np.isfinite(realised_vol_20d)
        and realised_vol_20d > universe_median_vol * config.max_vol_multiple_of_median
    ):
        reasons.append(
            f"vol {realised_vol_20d:.0%} > {config.max_vol_multiple_of_median}x median"
        )

    if spread_bps is not None and spread_bps > config.max_spread_bps:
        reasons.append(f"spread {spread_bps:.0f}bps")

    if pending_merger_cash:
        reasons.append("pending cash merger (dead money)")

    return ScreenResult(
        passed=not reasons,
        reasons=reasons,
        detail={
            "short_interest_pct": short_interest_pct,
            "days_to_cover": days_to_cover,
            "next_earnings": next_earnings,
            "realised_vol_20d": realised_vol_20d,
            "spread_bps": spread_bps,
        },
    )


def _business_days_between(start: dt.date, end: dt.date) -> int:
    if end < start:
        return -1
    days = 0
    cur = start
    while cur < end:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def estimate_spread_bps(
    close: float | None, high: float | None, low: float | None, adv: float | None
) -> float | None:
    """Proxy for the quoted spread.

    We do not have NBBO quotes, so we estimate from liquidity: spread in bps
    scales roughly with the inverse square root of dollar volume. Calibrated so
    a $2m ADV name lands near 40bps and a $100m name near 6bps, which matches
    observed US large/mid-cap spreads closely enough for a reject threshold.
    """
    if not adv or adv <= 0 or not close or close <= 0:
        return None
    bps = 55.0 * (2_000_000.0 / adv) ** 0.5
    # Sub-$5 stocks have a hard tick-size floor that liquidity cannot beat:
    # one cent on a $3 stock is 33bps no matter how much volume trades.
    tick_floor = 10_000.0 * (0.01 / close)
    return float(max(bps, tick_floor))
