"""FRED macro regime overlay. Computed once per day, applies to all names.

The macro layer adjusts sector weights and total risk appetite. It does not
pick individual stocks.

Regime classification from five series:
  T10Y2Y        yield curve -- inverted means late cycle
  BAMLH0A0HYM2  HY credit spread -- widening is the best single stress signal
  ICSA          initial claims -- rising means labour cracking
  NFCI          Chicago Fed financial conditions -- positive means tight
  VIXCLS        vol level
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from src.config.factor_weights import REGIME_FINAL_COUNT, REGIME_SECTOR_TILTS

log = structlog.get_logger(__name__)

REGIMES = ("RISK_ON", "LATE_CYCLE", "RISK_OFF", "RECOVERY")


@dataclass
class MacroState:
    regime: str
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    levels: dict[str, float] = field(default_factory=dict)
    series: dict[str, list] = field(default_factory=dict)

    def sector_tilt(self, sector: str | None) -> float:
        return REGIME_SECTOR_TILTS.get(self.regime, {}).get(sector or "", 1.0)

    def final_count(self, default: int = 10) -> int:
        return REGIME_FINAL_COUNT.get(self.regime, default)

    def regime_fit(self, sector: str | None) -> float:
        """0-1 fit of a sector to the current regime, for the LLM packet."""
        tilt = self.sector_tilt(sector)
        return float(np.clip((tilt - 0.8) / 0.4, 0.0, 1.0))


def _latest(series: list[dict[str, Any]]) -> float | None:
    for obs in reversed(series or []):
        v = obs.get("value")
        if v is not None:
            return float(v)
    return None


def _change(series: list[dict[str, Any]], lookback: int) -> float | None:
    vals = [o["value"] for o in (series or []) if o.get("value") is not None]
    if len(vals) <= lookback:
        return None
    return float(vals[-1] - vals[-1 - lookback])


def _percentile(series: list[dict[str, Any]], value: float | None) -> float | None:
    vals = [o["value"] for o in (series or []) if o.get("value") is not None]
    if value is None or len(vals) < 20:
        return None
    return float(sum(1 for v in vals if v <= value) / len(vals) * 100.0)


def classify_regime(series: dict[str, list[dict[str, Any]]]) -> MacroState:
    """Map the FRED series into one of four regimes.

    Score runs roughly -3 (maximum stress) to +3 (maximum risk appetite).
    Each component contributes both a level and a direction, because a wide
    spread that is narrowing is a recovery, not a crisis.
    """
    curve = _latest(series.get("T10Y2Y", []))
    hy = _latest(series.get("BAMLH0A0HYM2", []))
    hy_chg = _change(series.get("BAMLH0A0HYM2", []), 20)
    claims = _latest(series.get("ICSA", []))
    claims_chg = _change(series.get("ICSA", []), 4)
    nfci = _latest(series.get("NFCI", []))
    vix = _latest(series.get("VIXCLS", []))
    hy_pct = _percentile(series.get("BAMLH0A0HYM2", []), hy)

    signals: dict[str, float] = {}
    score = 0.0

    # Credit spreads: the best single stress signal.
    if hy is not None:
        s = 1.0 if hy < 3.5 else (0.0 if hy < 5.0 else -1.5)
        signals["credit_level"] = s
        score += s
    if hy_chg is not None:
        s = -1.0 if hy_chg > 0.5 else (0.5 if hy_chg < -0.3 else 0.0)
        signals["credit_direction"] = s
        score += s

    # Yield curve: inverted = late cycle, steepening from inversion = recovery.
    if curve is not None:
        s = -0.5 if curve < 0 else (0.5 if curve > 1.0 else 0.0)
        signals["curve"] = s
        score += s

    # Labour: rising claims mean the labour market is cracking.
    if claims_chg is not None and claims:
        rel = claims_chg / claims
        s = -1.0 if rel > 0.05 else (0.5 if rel < -0.03 else 0.0)
        signals["claims"] = s
        score += s

    # Financial conditions: NFCI > 0 is tighter than average.
    if nfci is not None:
        s = -1.0 if nfci > 0.2 else (0.5 if nfci < -0.3 else 0.0)
        signals["nfci"] = s
        score += s

    # Volatility.
    if vix is not None:
        s = 0.5 if vix < 16 else (0.0 if vix < 25 else -1.0)
        signals["vix"] = s
        score += s

    regime = _score_to_regime(score, curve, hy_chg)

    log.info("macro_regime", regime=regime, score=round(score, 2), **{
        k: round(v, 2) for k, v in signals.items()
    })
    return MacroState(
        regime=regime,
        score=score,
        signals=signals,
        levels={
            "T10Y2Y": curve,
            "BAMLH0A0HYM2": hy,
            "BAMLH0A0HYM2_pctile": hy_pct,
            "ICSA": claims,
            "NFCI": nfci,
            "VIXCLS": vix,
            "UNRATE": _latest(series.get("UNRATE", [])),
            "CPIAUCSL": _latest(series.get("CPIAUCSL", [])),
            "DFF": _latest(series.get("DFF", [])),
        },
        series=series,
    )


def _score_to_regime(
    score: float, curve: float | None, hy_chg: float | None
) -> str:
    if score <= -2.0:
        return "RISK_OFF"
    if score >= 1.5:
        return "RISK_ON"
    # The middle band splits on direction: spreads tightening out of stress is a
    # recovery; an inverted curve with flat spreads is late cycle.
    if hy_chg is not None and hy_chg < -0.2 and score > -1.0:
        return "RECOVERY"
    if curve is not None and curve < 0.25:
        return "LATE_CYCLE"
    return "RISK_ON" if score > 0 else "LATE_CYCLE"


async def load_macro_state(session=None, as_of: dt.date | None = None) -> MacroState:
    """Fetch FRED, classify, and persist. Degrades to a neutral state."""
    from src.ingest import fred
    from src.storage import repository

    as_of = as_of or dt.date.today()
    try:
        series = await fred.fetch_all_series()
    except Exception as exc:  # noqa: BLE001 - macro is an overlay, not a gate
        log.warning("fred_unavailable_using_neutral_regime", error=str(exc))
        return MacroState(regime="LATE_CYCLE", score=0.0)

    state = classify_regime(series)
    if session is not None:
        repository.save_macro(
            session,
            {
                "as_of_date": as_of,
                "regime": state.regime,
                "regime_score": state.score,
                "series": {"levels": state.levels, "signals": state.signals},
            },
        )
    return state


def summarise_for_report(state: MacroState) -> dict[str, Any]:
    """Compact form for the report header and the macro dashboard chart."""
    return {
        "regime": state.regime,
        "score": round(state.score, 2),
        "levels": {
            k: (round(v, 3) if isinstance(v, (int, float)) else v)
            for k, v in state.levels.items()
            if v is not None
        },
        "signals": {k: round(v, 2) for k, v in state.signals.items()},
        "tilts": REGIME_SECTOR_TILTS.get(state.regime, {}),
        "final_count": state.final_count(),
    }


def median_or_none(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None and np.isfinite(v)]
    return statistics.median(vals) if vals else None
