"""FMP: bulk fundamentals, ratios, sector/industry mapping, earnings calendar.

Bulk endpoints only. We never loop per-ticker on FMP -- that is what blows up
both the bill and the wall clock.

Note on restatements: FMP serves *restated* figures. They are fine for the live
daily run (today's best estimate of the truth) but SEC as-reported is the source
of truth for backtests. Everything written here is tagged source="fmp",
restated=True so the PIT accessor can prefer SEC.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterable
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

BASE_URL = "https://financialmodelingprep.com"


def _client(concurrency: int | None = None) -> APIClient:
    key = get_settings().fmp_api_key
    if not key:
        raise PermanentAPIError("FMP_API_KEY is not set")
    return APIClient("fmp", BASE_URL, concurrency=concurrency, timeout=120.0)


def _auth(params: dict[str, Any] | None = None) -> dict[str, Any]:
    p = dict(params or {})
    p["apikey"] = get_settings().fmp_api_key
    return p


async def fetch_batch_eod(date: dt.date) -> list[dict[str, Any]]:
    """`/api/v4/batch-request-end-of-day-prices` -- whole-market EOD for one date.

    One call returns every symbol's OHLCV for `date` (CSV), so it is a
    whole-market bulk source for the wide-end backfill, alongside Stooq. It is a
    paid-tier FMP endpoint: a plan without it returns 402/403, which surfaces here
    as PermanentAPIError -- the backfill's capability probe catches that and falls
    through rather than looping a dead endpoint. Rows are shaped for the bar store.
    """
    async with _client() as c:
        raw = await c.get_json(
            "/api/v4/batch-request-end-of-day-prices",
            params=_auth({"date": date.isoformat()}),
            expect_json=False,  # this endpoint serves CSV
            cache_ttl=6 * 3600,
        )
    records = _coerce_bulk_payload(raw)
    out: list[dict[str, Any]] = []
    for r in records:
        sym = r.get("symbol") or r.get("ticker")
        if not sym:
            continue
        out.append(
            {
                "ticker": sym,
                "date": _parse_date(r.get("date")) or date,
                "open": _as_float(r.get("open")),
                "high": _as_float(r.get("high")),
                "low": _as_float(r.get("low")),
                "close": _as_float(r.get("close")),
                "volume": _as_float(r.get("volume")),
                # adjClose is present on this endpoint; keep close raw and record
                # adjusted separately is out of scope -- the bar store holds one
                # close, and Stooq is the primary keyless source anyway.
                "vwap": None,
            }
        )
    log.info("fmp_batch_eod", date=str(date), rows=len(out))
    return out


async def fetch_stock_splits(
    start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    """Every split across the market in [start, end] -- ONE call.

    `/api/v3/stock_split_calendar`. Returns {ticker, date, ratio} where ratio is
    new-shares-per-old (>1 forward, <1 reverse) -- the same convention Yahoo and
    the split-adjustment detector use. This is the bulk source that lets the
    reconciler SAMPLE NAMES THAT ACTUALLY SPLIT instead of scanning the whole
    universe hoping to land on one. A plan without it raises PermanentAPIError,
    which the caller catches to fall back.
    """
    async with _client() as c:
        data = await c.get_json(
            "/api/v3/stock_split_calendar",
            params=_auth({"from": start.isoformat(), "to": end.isoformat()}),
            cache_ttl=12 * 3600,
        )
    out: list[dict[str, Any]] = []
    for r in data or []:
        sym = r.get("symbol") or r.get("ticker")
        d = _parse_date(r.get("date"))
        num = _as_float(r.get("numerator"))
        den = _as_float(r.get("denominator"))
        if not sym or not d:
            continue
        ratio = None
        if num and den and den != 0:
            ratio = num / den  # e.g. 4/1 = 4 (forward), 1/10 = 0.1 (reverse)
        if ratio is None or ratio <= 0 or ratio == 1.0:
            continue
        out.append({"ticker": str(sym).upper(), "date": d, "ratio": ratio})
    log.info("fmp_stock_splits", start=str(start), end=str(end), rows=len(out))
    return out


async def fetch_screener(
    *,
    exchanges: Iterable[str] = ("NYSE", "NASDAQ", "AMEX"),
    limit: int = 20000,
) -> list[dict[str, Any]]:
    """One call. Market cap, sector, industry, exchange for the whole market."""
    async with _client() as c:
        data = await c.get_json(
            "/api/v3/stock-screener",
            params=_auth(
                {
                    "exchange": ",".join(exchanges),
                    "limit": limit,
                    "isActivelyTrading": "true",
                }
            ),
            cache_ttl=12 * 3600,
        )
    rows = []
    for r in data or []:
        sym = r.get("symbol")
        if not sym:
            continue
        rows.append(
            {
                "ticker": sym,
                "name": r.get("companyName"),
                "market_cap": r.get("marketCap"),
                "sector": r.get("sector"),
                "industry": r.get("industry"),
                "exchange": r.get("exchangeShortName"),
                "beta": r.get("beta"),
                "price": r.get("price"),
                "volume": r.get("volume"),
                "is_etf": bool(r.get("isEtf")),
                "is_fund": bool(r.get("isFund")),
                "country": r.get("country"),
            }
        )
    log.info("fmp_screener", rows=len(rows))
    return rows


# Metrics we extract from the bulk statement payloads. Keys are our canonical
# metric names; values are the candidate field names FMP may use.
_STATEMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "Revenues", "totalRevenue"),
    "cogs": ("costOfRevenue", "CostOfRevenue"),
    "gross_profit": ("grossProfit",),
    "operating_income": ("operatingIncome", "OperatingIncomeLoss"),
    "net_income": ("netIncome", "NetIncomeLoss"),
    "eps": ("eps", "epsdiluted", "EarningsPerShareDiluted"),
    "total_assets": ("totalAssets", "Assets"),
    "total_equity": ("totalStockholdersEquity", "StockholdersEquity"),
    "total_debt": ("totalDebt",),
    "cash": ("cashAndCashEquivalents", "cashAndShortTermInvestments"),
    "operating_cash_flow": ("operatingCashFlow", "netCashProvidedByOperatingActivities"),
    "capex": ("capitalExpenditure",),
    "free_cash_flow": ("freeCashFlow",),
    "ebitda": ("ebitda",),
    "ebit": ("operatingIncome", "ebit"),
    "shares_diluted": ("weightedAverageShsOutDil", "weightedAverageShsOut"),
    "current_assets": ("totalCurrentAssets",),
    "current_liabilities": ("totalCurrentLiabilities",),
    "long_term_debt": ("longTermDebt",),
    "interest_expense": ("interestExpense",),
    "income_tax": ("incomeTaxExpense",),
    "pretax_income": ("incomeBeforeTax",),
}


def _pick(record: dict[str, Any], names: tuple[str, ...]) -> float | None:
    for n in names:
        v = record.get(n)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if not value:
        return None
    text = str(value)[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def normalise_statements(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten FMP statement records into `fundamentals` rows.

    Every row carries period_end AND filing_date. If FMP omits fillingDate we
    drop the record rather than guessing -- a fundamental without a known
    publication date cannot be used point-in-time.
    """
    out: list[dict[str, Any]] = []
    dropped = 0
    for rec in records:
        ticker = rec.get("symbol") or rec.get("ticker")
        period_end = _parse_date(rec.get("date") or rec.get("period_end"))
        filing_date = _parse_date(
            rec.get("fillingDate") or rec.get("filingDate") or rec.get("acceptedDate")
        )
        if not ticker or not period_end or not filing_date:
            dropped += 1
            continue
        fiscal = rec.get("period")
        for metric, names in _STATEMENT_FIELDS.items():
            value = _pick(rec, names)
            if value is None:
                continue
            out.append(
                {
                    "ticker": ticker,
                    "metric": metric,
                    "value": value,
                    "period_end": period_end,
                    "fiscal_period": fiscal,
                    "filing_date": filing_date,
                    "source": "fmp",
                    "restated": True,
                }
            )
    if dropped:
        log.warning("fmp_statements_dropped_no_filing_date", count=dropped)
    return out


async def fetch_bulk_statements(
    year: int, period: str = "quarter"
) -> list[dict[str, Any]]:
    """`/api/v4/financial-statement-full-as-reported-bulk`. One call per year.

    Returns normalised `fundamentals` rows ready for the repository.
    """
    async with _client() as c:
        raw = await c.get_json(
            "/api/v4/financial-statement-full-as-reported-bulk",
            params=_auth({"year": year, "period": period}),
            expect_json=False,
            cache_ttl=24 * 3600,
        )
    records = _coerce_bulk_payload(raw)
    rows = normalise_statements(records)
    log.info("fmp_bulk_statements", year=year, period=period, rows=len(rows))
    return rows


def _coerce_bulk_payload(raw: Any) -> list[dict[str, Any]]:
    """The bulk endpoints return CSV; the per-ticker ones return JSON."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, str):
        text = raw.lstrip()
        if text.startswith("[") or text.startswith("{"):
            import json

            try:
                data = json.loads(text)
            except ValueError:
                return []
            return data if isinstance(data, list) else [data]
        return list(csv.DictReader(io.StringIO(raw)))
    return []


async def fetch_ratios_bulk(year: int, period: str = "quarter") -> list[dict[str, Any]]:
    """Pre-computed ratios. Cheaper than deriving everything ourselves."""
    async with _client() as c:
        raw = await c.get_json(
            "/api/v4/ratios-bulk",
            params=_auth({"year": year, "period": period}),
            expect_json=False,
            cache_ttl=24 * 3600,
        )
    return _coerce_bulk_payload(raw)


async def fetch_earnings_calendar(
    start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    """Both past reports (with actual vs estimate) and scheduled future dates."""
    async with _client() as c:
        data = await c.get_json(
            "/api/v3/earning_calendar",
            params=_auth({"from": start.isoformat(), "to": end.isoformat()}),
            cache_ttl=6 * 3600,
        )
    today = dt.date.today()
    rows = []
    for r in data or []:
        sym = r.get("symbol")
        d = _parse_date(r.get("date"))
        if not sym or not d:
            continue
        actual = r.get("eps")
        est = r.get("epsEstimated")
        surprise = None
        if actual is not None and est not in (None, 0):
            try:
                surprise = (float(actual) - float(est)) / abs(float(est))
            except (TypeError, ValueError, ZeroDivisionError):
                surprise = None
        rows.append(
            {
                "ticker": sym,
                "report_date": d,
                "period_end": _parse_date(r.get("fiscalDateEnding")),
                "actual_eps": _as_float(actual),
                "consensus_eps": _as_float(est),
                "surprise_pct": surprise,
                "is_future": d > today or actual is None,
            }
        )
    log.info("fmp_earnings_calendar", rows=len(rows))
    return rows


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def fetch_shares_float(symbols: Iterable[str] | None = None) -> dict[str, dict]:
    """Bulk float + short interest inputs for the crowding screen."""
    async with _client() as c:
        data = await c.get_json(
            "/api/v4/shares_float/all",
            params=_auth(),
            expect_json=False,
            cache_ttl=24 * 3600,
        )
    records = _coerce_bulk_payload(data)
    wanted = set(symbols) if symbols else None
    out: dict[str, dict] = {}
    for r in records:
        sym = r.get("symbol")
        if not sym or (wanted and sym not in wanted):
            continue
        out[sym] = {
            "float_shares": _as_float(r.get("floatShares")),
            "outstanding_shares": _as_float(r.get("outstandingShares")),
            "free_float_pct": _as_float(r.get("freeFloat")),
        }
    return out
