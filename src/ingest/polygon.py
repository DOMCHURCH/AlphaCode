"""Polygon: all OHLCV, the universe reference data, and options snapshots.

Grouped-daily returns every US ticker in ONE call. That is the backbone of the
whole funnel -- the wide end of the cascade costs one HTTP request.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

BASE_URL = "https://api.polygon.io"

VALID_EXCHANGES = {"XNYS", "XNAS", "ARCX"}


# Calls permitted on the FREE tier. Grouped-daily is a single whole-market call
# (the daily increment). Everything else -- options snapshots, the paginated
# reference, per-ticker aggs -- 403s on free and, worse, makes the shared rate
# limiter sleep up to its max_wait per call (the 12s-wait flood). So on free we
# refuse those calls IMMEDIATELY, naming the call site, instead of stalling.
_FREE_TIER_PERMITTED = frozenset({"grouped_daily"})


def _guard_tier(call: str) -> None:
    s = get_settings()
    if s.polygon_tier == "free" and call not in _FREE_TIER_PERMITTED:
        raise PermanentAPIError(
            f"polygon.{call} is not available on POLYGON_TIER=free -- refused "
            f"without a network call (it would 403 and stall the rate limiter "
            f"~12s). Set POLYGON_TIER=paid to enable it."
        )


def _client(concurrency: int | None = None) -> APIClient:
    key = get_settings().polygon_api_key
    if not key:
        raise PermanentAPIError("POLYGON_API_KEY is not set")
    return APIClient(
        "polygon",
        BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        concurrency=concurrency,
    )


async def fetch_grouped_daily(
    date: dt.date, *, adjusted: bool = True
) -> list[dict[str, Any]]:
    """Every US ticker's OHLCV for one session. One call.

    Returns rows shaped like {ticker, date, open, high, low, close, volume, vwap}.
    An empty list means the market was closed that day.
    """
    _guard_tier("grouped_daily")
    async with _client() as c:
        data = await c.get_json(
            f"/v2/aggs/grouped/locale/us/market/stocks/{date.isoformat()}",
            params={"adjusted": str(adjusted).lower()},
            cache_ttl=6 * 3600,
        )
    results = (data or {}).get("results") or []
    out = []
    for r in results:
        t = r.get("T")
        if not t:
            continue
        out.append(
            {
                "ticker": t,
                "date": date,
                "open": r.get("o"),
                "high": r.get("h"),
                "low": r.get("l"),
                "close": r.get("c"),
                "volume": r.get("v"),
                "vwap": r.get("vw"),
                "transactions": r.get("n"),
            }
        )
    log.info("polygon_grouped_daily", date=str(date), rows=len(out))
    return out


async def fetch_ticker_reference(
    *, market: str = "stocks", active: bool = True, limit: int = 1000
) -> list[dict[str, Any]]:
    """Paginated /v3/reference/tickers. Gives type (CS/ETF/...) and exchange."""
    _guard_tier("ticker_reference")
    out: list[dict[str, Any]] = []
    async with _client() as c:
        params: dict[str, Any] = {
            "market": market,
            "active": str(active).lower(),
            "limit": limit,
        }
        url = "/v3/reference/tickers"
        while True:
            data = await c.get_json(url, params=params, cache_ttl=12 * 3600)
            results = (data or {}).get("results") or []
            for r in results:
                out.append(
                    {
                        "ticker": r.get("ticker"),
                        "name": r.get("name"),
                        "security_type": r.get("type"),
                        "exchange": r.get("primary_exchange"),
                        "cik": r.get("cik"),
                        "active": r.get("active", True),
                        "currency": r.get("currency_name"),
                        "locale": r.get("locale"),
                    }
                )
            cursor = (data or {}).get("next_url")
            if not cursor or not results:
                break
            # next_url is absolute; strip the base so the client's base_url applies.
            url = cursor.replace(BASE_URL, "")
            params = {}
    log.info("polygon_ticker_reference", rows=len(out))
    return out


async def fetch_daily_bars(
    ticker: str, start: dt.date, end: dt.date, *, adjusted: bool = True
) -> list[dict[str, Any]]:
    """Backfill path for a single ticker. Not used in the daily hot loop."""
    _guard_tier("daily_bars")
    async with _client() as c:
        data = await c.get_json(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            params={"adjusted": str(adjusted).lower(), "limit": 50000, "sort": "asc"},
            cache_ttl=6 * 3600,
        )
    rows = []
    for r in (data or {}).get("results") or []:
        ts = r.get("t")
        if ts is None:
            continue
        d = dt.datetime.fromtimestamp(ts / 1000, tz=dt.UTC).date()
        rows.append(
            {
                "ticker": ticker,
                "date": d,
                "open": r.get("o"),
                "high": r.get("h"),
                "low": r.get("l"),
                "close": r.get("c"),
                "volume": r.get("v"),
                "vwap": r.get("vw"),
                "transactions": r.get("n"),
            }
        )
    return rows


async def fetch_options_snapshot(
    ticker: str, *, limit: int = 250, client: APIClient | None = None
) -> dict[str, Any]:
    """Options chain summary: IV, open interest, skew inputs.

    Returns a compact dict, never the raw chain. Degrades to empty on failure --
    options data is a nice-to-have, not a gate.
    """
    _guard_tier("options_snapshot")
    owns = client is None
    c = client or _client()
    try:
        if owns:
            await c.__aenter__()
        data = await c.get_json(
            f"/v3/snapshot/options/{ticker}",
            params={"limit": limit},
            cache_ttl=3600,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("options_snapshot_failed", ticker=ticker, error=str(exc))
        return {}
    finally:
        if owns:
            await c.__aexit__(None, None, None)

    results = (data or {}).get("results") or []
    return summarise_option_chain(results)


def summarise_option_chain(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a raw chain into the four numbers Stage 3 actually uses.

    - put/call open interest ratio
    - 25-delta skew (put IV minus call IV)
    - mean IV (feeds IV rank once we have a trailing series)
    - unusual OI buildup at strikes above spot
    """
    calls_oi = puts_oi = 0.0
    call_ivs: list[tuple[float, float]] = []  # (delta, iv)
    put_ivs: list[tuple[float, float]] = []
    ivs: list[float] = []
    upside_oi = 0.0
    spot = 0.0
    strike_oi: dict[float, float] = {}

    for r in results:
        det = r.get("details") or {}
        greeks = r.get("greeks") or {}
        day = r.get("day") or {}
        under = r.get("underlying_asset") or {}
        spot = spot or float(under.get("price") or 0.0)
        oi = float(r.get("open_interest") or 0.0)
        iv = r.get("implied_volatility")
        ctype = det.get("contract_type")
        strike = float(det.get("strike_price") or 0.0)
        delta = greeks.get("delta")

        if iv is not None:
            ivs.append(float(iv))
        if ctype == "call":
            calls_oi += oi
            if iv is not None and delta is not None:
                call_ivs.append((float(delta), float(iv)))
            if spot and strike > spot:
                upside_oi += oi
        elif ctype == "put":
            puts_oi += oi
            if iv is not None and delta is not None:
                put_ivs.append((float(delta), float(iv)))
        if strike:
            strike_oi[strike] = strike_oi.get(strike, 0.0) + oi
        _ = day  # volume available if we later want OI-vs-volume checks

    def _nearest_iv(pairs: list[tuple[float, float]], target: float) -> float | None:
        if not pairs:
            return None
        return min(pairs, key=lambda p: abs(abs(p[0]) - target))[1]

    put25 = _nearest_iv(put_ivs, 0.25)
    call25 = _nearest_iv(call_ivs, 0.25)
    total_oi = calls_oi + puts_oi

    return {
        "put_call_oi_ratio": (puts_oi / calls_oi) if calls_oi else None,
        "iv_mean": (sum(ivs) / len(ivs)) if ivs else None,
        "skew_25d": (put25 - call25) if (put25 is not None and call25 is not None) else None,
        "upside_oi_share": (upside_oi / total_oi) if total_oi else None,
        "total_oi": total_oi,
        "spot": spot or None,
        "n_contracts": len(results),
    }
