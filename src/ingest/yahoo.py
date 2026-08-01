"""Yahoo (yfinance): fallback and reconciliation only.

Unofficial API. Never a hard dependency. Every call is wrapped, every failure
degrades to an empty result, and yfinance itself is imported lazily so the
package is optional at deploy time.

Job: corporate actions (splits, dividends) and short-interest reconciliation.
Nothing in the funnel gates on it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_yf: Any | None = None
_yf_checked = False


def _yfinance() -> Any | None:
    global _yf, _yf_checked
    if _yf_checked:
        return _yf
    _yf_checked = True
    try:
        import yfinance  # type: ignore

        _yf = yfinance
    except Exception as exc:  # noqa: BLE001 - optional dependency
        log.info("yfinance_unavailable", error=str(exc))
        _yf = None
    return _yf


def fetch_corporate_actions(ticker: str, since: dt.date | None = None) -> list[dict]:
    """Splits and dividends. Used to sanity-check Polygon's adjusted series."""
    yf = _yfinance()
    if yf is None:
        return []
    try:
        actions = yf.Ticker(ticker).actions
    except Exception as exc:  # noqa: BLE001
        log.debug("yahoo_actions_failed", ticker=ticker, error=str(exc))
        return []
    if actions is None or getattr(actions, "empty", True):
        return []
    out = []
    try:
        for idx, row in actions.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            if since and d < since:
                continue
            out.append(
                {
                    "ticker": ticker,
                    "date": d,
                    "dividend": float(row.get("Dividends", 0) or 0),
                    "split_ratio": float(row.get("Stock Splits", 0) or 0),
                }
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("yahoo_actions_parse_failed", ticker=ticker, error=str(exc))
        return []
    return out


def fetch_short_interest(ticker: str) -> dict[str, float | None]:
    """Short interest and days-to-cover for the crowding screen.

    Returns empty on any failure; the screen treats missing data as "unknown",
    which does not reject the name.
    """
    yf = _yfinance()
    if yf is None:
        return {}
    try:
        info = yf.Ticker(ticker).get_info()
    except Exception as exc:  # noqa: BLE001
        log.debug("yahoo_info_failed", ticker=ticker, error=str(exc))
        return {}
    if not isinstance(info, dict):
        return {}

    def _f(key: str) -> float | None:
        v = info.get(key)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    pct = _f("shortPercentOfFloat")
    return {
        "short_interest_pct": pct * 100 if pct is not None and pct < 1.5 else pct,
        "days_to_cover": _f("shortRatio"),
        "float_shares": _f("floatShares"),
    }
