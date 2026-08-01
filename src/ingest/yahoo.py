"""Yahoo (yfinance): fallback and reconciliation only.

Unofficial API. Never a hard dependency. Every call is wrapped, every failure
degrades to an empty result, and yfinance itself is imported lazily so the
package is optional at deploy time.

Job: corporate actions (splits, dividends) and short-interest reconciliation.
Nothing in the funnel gates on it.
"""

from __future__ import annotations

import asyncio
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


def _frame_to_rows(df: Any, tickers: list[str]) -> list[dict[str, Any]]:
    """Turn a yfinance download frame into DailyBar rows.

    yfinance returns a per-ticker column MultiIndex for multiple symbols and a
    flat frame for one. Pure (no network) so it is unit-testable with a synthetic
    frame. Rows missing a close are skipped -- Yahoo pads delisted names with NaN.
    """
    import math

    import pandas as pd  # local import: pandas is heavy, keep module import cheap

    if df is None or getattr(df, "empty", True):
        return []

    def _num(v: Any) -> float | None:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(f) else f

    multi = isinstance(df.columns, pd.MultiIndex)
    out: list[dict[str, Any]] = []
    syms = tickers if multi else (tickers[:1] or ["?"])
    for sym in syms:
        try:
            sub = df[sym] if multi else df
        except KeyError:
            continue
        for idx, row in sub.iterrows():
            d = idx.date() if hasattr(idx, "date") else idx
            close = _num(row.get("Close"))
            if close is None:
                continue
            out.append(
                {
                    "ticker": str(sym).upper(),
                    "date": d,
                    "open": _num(row.get("Open")),
                    "high": _num(row.get("High")),
                    "low": _num(row.get("Low")),
                    "close": close,
                    "volume": _num(row.get("Volume")),
                    "vwap": None,
                    "transactions": None,
                }
            )
    return out


def _download(yf: Any, tickers: list[str], start: dt.date, end: dt.date) -> Any:
    """Blocking yfinance batch download. Runs in a worker thread via to_thread."""
    return yf.download(
        tickers=tickers,
        start=start.isoformat(),
        end=(end + dt.timedelta(days=1)).isoformat(),  # yfinance end is exclusive
        interval="1d",
        auto_adjust=True,
        group_by="ticker",
        threads=True,
        progress=False,
        actions=False,
    )


async def fetch_daily_bars_batch(
    tickers: list[str],
    start: dt.date,
    end: dt.date,
    *,
    chunk: int = 200,
) -> list[dict[str, Any]]:
    """Free OHLCV history for many tickers via Yahoo, batched.

    The keyless backbone of free-data mode: yfinance fetches a chunk of symbols
    in one threaded call, so ~10k names cost ~50 calls, not 10k. Degrades to an
    empty list if yfinance is unavailable or a chunk fails -- one bad chunk never
    sinks the backfill.
    """
    yf = _yfinance()
    if yf is None:
        log.error("yahoo_batch_no_yfinance")
        return []
    clean = list(dict.fromkeys(t.upper() for t in tickers if t))
    out: list[dict[str, Any]] = []
    for i in range(0, len(clean), chunk):
        batch = clean[i : i + chunk]
        try:
            df = await asyncio.to_thread(_download, yf, batch, start, end)
            rows = _frame_to_rows(df, batch)
        except Exception as exc:  # noqa: BLE001 - one bad chunk is survivable
            log.warning("yahoo_batch_chunk_failed", n=len(batch), error=str(exc)[:200])
            rows = []
        out.extend(rows)
        log.info(
            "yahoo_batch_progress", done=min(i + chunk, len(clean)),
            total=len(clean), rows=len(out),
        )
    return out


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
