"""FRED: macro regime state.

Updated weekly/monthly, cached 24h. Drives sector tilts and total risk
appetite -- it never picks an individual name.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

BASE_URL = "https://api.stlouisfed.org/fred"

SERIES = {
    "T10Y2Y": "10y-2y treasury spread",
    "BAMLH0A0HYM2": "high yield OAS",
    "ICSA": "initial jobless claims",
    "UNRATE": "unemployment rate",
    "CPIAUCSL": "CPI",
    "DFF": "fed funds effective",
    "VIXCLS": "VIX",
    "NFCI": "Chicago Fed national financial conditions",
}


def make_client() -> APIClient:
    if not get_settings().fred_api_key:
        raise PermanentAPIError("FRED_API_KEY is not set")
    return APIClient("fred", BASE_URL, cache_ttl=24 * 3600)


async def fetch_series(
    client: APIClient, series_id: str, *, lookback_days: int = 730
) -> list[dict[str, Any]]:
    start = dt.date.today() - dt.timedelta(days=lookback_days)
    data = await client.get_json(
        "/series/observations",
        params={
            "series_id": series_id,
            "api_key": get_settings().fred_api_key,
            "file_type": "json",
            "observation_start": start.isoformat(),
        },
    )
    out = []
    for o in (data or {}).get("observations") or []:
        v = o.get("value")
        if v in (None, ".", ""):
            continue
        try:
            out.append(
                {"date": dt.date.fromisoformat(o["date"]), "value": float(v)}
            )
        except (KeyError, ValueError):
            continue
    return out


async def fetch_all_series(
    lookback_days: int = 730,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    async with make_client() as c:
        for sid in SERIES:
            try:
                out[sid] = await fetch_series(c, sid, lookback_days=lookback_days)
            except Exception as exc:  # noqa: BLE001 - one bad series is survivable
                log.warning("fred_series_failed", series=sid, error=str(exc))
                out[sid] = []
    return out
