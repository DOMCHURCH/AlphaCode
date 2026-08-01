"""SEC EDGAR: ground-truth filings.

Free, no key, but two hard constraints:
  1. You MUST send a descriptive User-Agent with a contact email or you get a
     403. `SEC_USER_AGENT` env var, enforced at client construction.
  2. Max 10 req/sec. The shared token bucket is configured at 8/s.

This is also the source of truth for point-in-time fundamentals: the `filed`
field on a submission is the date a number actually became public, and the
XBRL companyconcept endpoint returns as-reported (not restated) figures.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov"
WWW_URL = "https://www.sec.gov"

# Forms we care about, and how Stage 3 reads them.
POSITIVE_FORMS = {"SC 13D", "SC 13D/A"}
DILUTION_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "424B5", "424B3", "424B4"}
EVENT_FORMS = {"8-K", "8-K/A"}
PERIODIC_FORMS = {"10-Q", "10-K", "10-K/A", "10-Q/A"}
INSIDER_FORMS = {"4", "4/A"}
INSTITUTIONAL_FORMS = {"13F-HR", "13F-HR/A"}

TRACKED_FORMS = (
    POSITIVE_FORMS
    | DILUTION_FORMS
    | EVENT_FORMS
    | PERIODIC_FORMS
    | INSIDER_FORMS
    | INSTITUTIONAL_FORMS
)

# 8-K items with a directional read.
ITEM_MATERIAL_AGREEMENT = "1.01"
ITEM_RESULTS = "2.02"
ITEM_OFFICER_CHANGE = "5.02"


def make_client(concurrency: int = 8) -> APIClient:
    ua = get_settings().sec_user_agent
    if not ua or "@" not in ua:
        raise PermanentAPIError(
            "SEC_USER_AGENT must be set to 'Name your@email.com' or SEC returns 403"
        )
    return APIClient(
        "sec",
        SUBMISSIONS_URL,
        headers={
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        },
        concurrency=concurrency,
        cache_ttl=12 * 3600,
    )


def pad_cik(cik: str | int) -> str:
    return str(cik).lstrip("0").zfill(10)


def parse_company_tickers(payload: Any) -> list[dict[str, Any]]:
    """SEC's company_tickers.json -> universe rows.

    The file maps an index to {cik_str, ticker, title} for every operating
    company that files with the SEC -- i.e. common stocks, exactly the universe
    the funnel wants (no ETFs/funds). Pure, so it is unit-testable offline.
    """
    if isinstance(payload, dict):
        records = list(payload.values())
    elif isinstance(payload, list):
        records = payload
    else:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in records:
        if not isinstance(r, dict):
            continue
        ticker = str(r.get("ticker") or "").upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(
            {
                "ticker": ticker,
                "name": r.get("title"),
                "cik": str(r.get("cik_str")) if r.get("cik_str") is not None else None,
                "security_type": "CS",  # SEC filers are operating companies
                "exchange": None,
                "active": True,
            }
        )
    return out


async def fetch_company_tickers() -> list[dict[str, Any]]:
    """Every SEC-listed company's ticker + CIK + name. Free, no key.

    The free-data universe seed. Also hands each name its CIK, which the
    point-in-time fundamentals backfill needs anyway.
    """
    ua = get_settings().sec_user_agent
    if not ua or "@" not in ua:
        raise PermanentAPIError(
            "SEC_USER_AGENT must be set to 'Name your@email.com' or SEC returns 403"
        )
    client = APIClient(
        "sec",
        WWW_URL,
        headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate",
                 "Host": "www.sec.gov"},
        cache_ttl=12 * 3600,
    )
    async with client:
        data = await client.get_json("/files/company_tickers.json")
    rows = parse_company_tickers(data)
    log.info("sec_company_tickers", rows=len(rows))
    return rows


async def fetch_submissions(client: APIClient, cik: str | int) -> dict[str, Any]:
    return await client.get_json(f"/submissions/CIK{pad_cik(cik)}.json")


def _parse_recent(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """EDGAR returns recent filings as parallel arrays, not a list of objects."""
    recent = ((payload or {}).get("filings") or {}).get("recent") or {}
    keys = ["form", "filingDate", "accessionNumber", "primaryDocument", "items",
            "reportDate", "primaryDocDescription"]
    cols = {k: recent.get(k) or [] for k in keys}
    n = len(cols["form"])
    rows = []
    for i in range(n):
        rows.append({k: (cols[k][i] if i < len(cols[k]) else None) for k in keys})
    return rows


def extract_filing_events(
    payload: dict[str, Any], ticker: str, since: dt.date, until: dt.date
) -> list[dict[str, Any]]:
    """Map an EDGAR submissions payload to `filing_events` rows."""
    cik = str((payload or {}).get("cik") or "")
    out = []
    for r in _parse_recent(payload):
        form = (r.get("form") or "").strip()
        if form not in TRACKED_FORMS:
            continue
        try:
            fdate = dt.date.fromisoformat(str(r.get("filingDate"))[:10])
        except (TypeError, ValueError):
            continue
        if not (since <= fdate <= until):
            continue
        accession = (r.get("accessionNumber") or "").replace("-", "")
        if not accession:
            continue
        out.append(
            {
                "ticker": ticker,
                "cik": cik,
                "form": form,
                "items": r.get("items"),
                "filing_date": fdate,
                "accession": accession[:32],
                "primary_doc": r.get("primaryDocument"),
                "detail": {"report_date": r.get("reportDate")},
            }
        )
    return out


def latest_periodic_filing_dates(payload: dict[str, Any]) -> dict[dt.date, dt.date]:
    """Map period_end -> filing_date from 10-Q/10-K submissions.

    This is how we attach a real publication date to fundamentals that arrive
    from a vendor without one.
    """
    out: dict[dt.date, dt.date] = {}
    for r in _parse_recent(payload):
        if (r.get("form") or "").strip() not in PERIODIC_FORMS:
            continue
        try:
            filed = dt.date.fromisoformat(str(r.get("filingDate"))[:10])
            period = dt.date.fromisoformat(str(r.get("reportDate"))[:10])
        except (TypeError, ValueError):
            continue
        # Earliest filing wins: a later 10-K/A restates, it does not re-publish.
        if period not in out or filed < out[period]:
            out[period] = filed
    return out


# ---------------------------------------------------------------------------
# XBRL as-reported facts -- the PIT source of truth
# ---------------------------------------------------------------------------
XBRL_CONCEPTS: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "cogs": ("CostOfGoodsAndServicesSold", "CostOfRevenue"),
    "gross_profit": ("GrossProfit",),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "total_assets": ("Assets",),
    "total_equity": ("StockholdersEquity",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "long_term_debt": ("LongTermDebtNoncurrent", "LongTermDebt"),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
    "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    "eps": ("EarningsPerShareDiluted",),
    "income_tax": ("IncomeTaxExpenseBenefit",),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ),
}


async def fetch_company_concept(
    client: APIClient, cik: str | int, concept: str, taxonomy: str = "us-gaap"
) -> dict[str, Any]:
    try:
        return await client.get_json(
            f"/api/xbrl/companyconcept/CIK{pad_cik(cik)}/{taxonomy}/{concept}.json"
        )
    except Exception as exc:  # noqa: BLE001 - a missing concept is normal
        log.debug("xbrl_concept_missing", cik=str(cik), concept=concept, error=str(exc))
        return {}


def parse_company_concept(
    payload: dict[str, Any], ticker: str, metric: str
) -> list[dict[str, Any]]:
    """Turn a companyconcept payload into PIT `fundamentals` rows.

    Every unit entry carries `end` (period_end) and `filed` (filing_date). We
    keep both, which is exactly what makes the backtest honest. `frame` presence
    distinguishes original from amended; we keep all versions and let the PIT
    accessor pick the latest one visible at query time.
    """
    units = (payload or {}).get("units") or {}
    rows: list[dict[str, Any]] = []
    for unit_entries in units.values():
        for e in unit_entries:
            try:
                period_end = dt.date.fromisoformat(str(e.get("end"))[:10])
                filed = dt.date.fromisoformat(str(e.get("filed"))[:10])
            except (TypeError, ValueError):
                continue
            val = e.get("val")
            if val is None:
                continue
            rows.append(
                {
                    "ticker": ticker,
                    "metric": metric,
                    "value": float(val),
                    "period_end": period_end,
                    "fiscal_period": e.get("fp"),
                    "filing_date": filed,
                    "source": "sec",
                    "restated": bool(e.get("form", "").endswith("/A")),
                }
            )
    return rows


async def fetch_pit_fundamentals(
    client: APIClient, ticker: str, cik: str | int, metrics: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """As-reported fundamentals with true filing dates, for one company."""
    wanted = list(metrics) if metrics else list(XBRL_CONCEPTS)
    rows: list[dict[str, Any]] = []
    for metric in wanted:
        for concept in XBRL_CONCEPTS.get(metric, ()):
            payload = await fetch_company_concept(client, cik, concept)
            parsed = parse_company_concept(payload, ticker, metric)
            if parsed:
                rows.extend(parsed)
                break  # first concept that resolves wins
    return rows


# ---------------------------------------------------------------------------
# Form 4 parsing
# ---------------------------------------------------------------------------
_TAG = re.compile(r"<([A-Za-z0-9_]+)>([^<]*)</\1>")


def parse_form4_xml(xml: str, ticker: str) -> list[dict[str, Any]]:
    """Minimal Form 4 extractor.

    Deliberately regex-based rather than a full XML parse: EDGAR ships a mix of
    well-formed and legacy documents, and we only need five fields. Anything we
    cannot parse is skipped, never guessed.
    """
    if not xml:
        return []
    person = _first(_TAG.findall(xml), "rptOwnerName")
    is_director = _first(_TAG.findall(xml), "isDirector") in {"1", "true"}
    is_officer = _first(_TAG.findall(xml), "isOfficer") in {"1", "true"}
    title = _first(_TAG.findall(xml), "officerTitle") or ""
    role = "other"
    if is_officer:
        low = title.lower()
        if "chief executive" in low or "ceo" in low:
            role = "ceo"
        elif "chief financial" in low or "cfo" in low:
            role = "cfo"
        else:
            role = "officer"
    elif is_director:
        role = "director"

    out: list[dict[str, Any]] = []
    for block in re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S
    ):
        pairs = _TAG.findall(block)
        code = _first(pairs, "transactionCode")
        date_s = _first(pairs, "transactionDate")
        shares = _num(_first(pairs, "transactionShares"))
        price = _num(_first(pairs, "transactionPricePerShare"))
        if not code or not date_s:
            continue
        try:
            tdate = dt.date.fromisoformat(date_s[:10])
        except ValueError:
            continue
        out.append(
            {
                "ticker": ticker,
                "person": person,
                "role": role,
                "transaction_code": code,
                "shares": shares,
                "price": price,
                "value_usd": (shares * price) if shares and price else None,
                "transaction_date": tdate,
                "source": "sec",
            }
        )
    return out


def _first(pairs: list[tuple[str, str]], tag: str) -> str | None:
    for k, v in pairs:
        if k == tag and v.strip():
            return v.strip()
    return None


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None
