"""Finnhub: estimate revisions, recommendation trends, surprises, insiders.

60 req/min on the free tier, so this is Stage 3 only -- after the universe is
under 400 names. Every call goes through the shared rate limiter.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

BASE_URL = "https://finnhub.io/api/v1"


def make_client(concurrency: int = 5) -> APIClient:
    key = get_settings().finnhub_api_key
    if not key:
        raise PermanentAPIError("FINNHUB_API_KEY is not set")
    return APIClient(
        "finnhub", BASE_URL, headers={"X-Finnhub-Token": key}, concurrency=concurrency
    )


async def fetch_recommendation_trend(
    client: APIClient, ticker: str
) -> dict[str, Any]:
    """Latest recommendation distribution plus the month-over-month delta.

    Delta is expressed as a change in the mean rating where
    strong buy = 5 ... strong sell = 1, so positive = analysts warming up.
    """
    data = await client.get_json("/stock/recommendation", params={"symbol": ticker})
    if not data:
        return {}
    rows = sorted(data, key=lambda r: r.get("period", ""), reverse=True)
    cur = rows[0]
    prev = rows[1] if len(rows) > 1 else None

    def _mean(r: dict[str, Any]) -> float | None:
        weights = {
            "strongBuy": 5,
            "buy": 4,
            "hold": 3,
            "sell": 2,
            "strongSell": 1,
        }
        num = sum(float(r.get(k) or 0) * w for k, w in weights.items())
        den = sum(float(r.get(k) or 0) for k in weights)
        return num / den if den else None

    cur_mean = _mean(cur)
    prev_mean = _mean(prev) if prev else None
    return {
        "reco_strong_buy": cur.get("strongBuy"),
        "reco_buy": cur.get("buy"),
        "reco_hold": cur.get("hold"),
        "reco_sell": cur.get("sell"),
        "reco_strong_sell": cur.get("strongSell"),
        "reco_mean": cur_mean,
        "reco_trend_delta": (
            cur_mean - prev_mean
            if cur_mean is not None and prev_mean is not None
            else None
        ),
    }


async def fetch_eps_estimates(client: APIClient, ticker: str) -> dict[str, Any]:
    data = await client.get_json(
        "/stock/eps-estimate", params={"symbol": ticker, "freq": "quarterly"}
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return {}
    nxt = sorted(rows, key=lambda r: r.get("period", ""))
    forward = next(
        (r for r in nxt if r.get("period", "") >= dt.date.today().isoformat()), nxt[-1]
    )
    return {
        "eps_consensus": forward.get("epsAvg"),
        "eps_analysts": forward.get("numberAnalysts"),
        "eps_high": forward.get("epsHigh"),
        "eps_low": forward.get("epsLow"),
        "eps_period": forward.get("period"),
    }


async def fetch_revenue_estimates(client: APIClient, ticker: str) -> dict[str, Any]:
    data = await client.get_json(
        "/stock/revenue-estimate", params={"symbol": ticker, "freq": "quarterly"}
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return {}
    nxt = sorted(rows, key=lambda r: r.get("period", ""))
    forward = next(
        (r for r in nxt if r.get("period", "") >= dt.date.today().isoformat()), nxt[-1]
    )
    return {
        "revenue_consensus": forward.get("revenueAvg"),
        "revenue_analysts": forward.get("numberAnalysts"),
    }


async def fetch_earnings_surprises(
    client: APIClient, ticker: str
) -> list[dict[str, Any]]:
    """Trailing actual-vs-consensus history. Feeds the SUE denominator."""
    data = await client.get_json("/stock/earnings", params={"symbol": ticker})
    rows = []
    for r in data or []:
        period = r.get("period")
        if not period:
            continue
        try:
            pe = dt.date.fromisoformat(str(period)[:10])
        except ValueError:
            continue
        rows.append(
            {
                "ticker": ticker,
                "period_end": pe,
                "actual_eps": r.get("actual"),
                "consensus_eps": r.get("estimate"),
                "surprise_pct": r.get("surprisePercent"),
            }
        )
    return rows


ROLE_KEYWORDS = {
    "ceo": ("chief executive", "ceo", "president and chief executive"),
    "cfo": ("chief financial", "cfo"),
    "director": ("director",),
    "officer": ("officer", "chief", "evp", "svp", "vice president"),
}


def classify_role(title: str | None) -> str:
    t = (title or "").lower()
    for role in ("ceo", "cfo"):
        if any(k in t for k in ROLE_KEYWORDS[role]):
            return role
    if any(k in t for k in ROLE_KEYWORDS["director"]):
        return "director"
    if any(k in t for k in ROLE_KEYWORDS["officer"]):
        return "officer"
    return "other"


async def fetch_insider_transactions(
    client: APIClient, ticker: str, since: dt.date
) -> list[dict[str, Any]]:
    data = await client.get_json(
        "/stock/insider-transactions",
        params={"symbol": ticker, "from": since.isoformat()},
    )
    rows = []
    for r in (data or {}).get("data") or []:
        d = r.get("transactionDate")
        if not d:
            continue
        try:
            tdate = dt.date.fromisoformat(str(d)[:10])
        except ValueError:
            continue
        shares = r.get("change")
        price = r.get("transactionPrice")
        rows.append(
            {
                "ticker": ticker,
                "person": r.get("name"),
                "role": classify_role(r.get("position") or r.get("name")),
                "transaction_code": r.get("transactionCode"),
                "shares": shares,
                "price": price,
                "value_usd": (
                    abs(float(shares)) * float(price)
                    if shares is not None and price
                    else None
                ),
                "transaction_date": tdate,
                "filed_date": None,
                "source": "finnhub",
            }
        )
    return rows
