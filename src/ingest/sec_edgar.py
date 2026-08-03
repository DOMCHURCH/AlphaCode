"""SEC EDGAR: the company universe, SIC sectors, and filing metadata.

Free, no key, but two hard constraints:
  1. You MUST send a descriptive User-Agent with a contact email or you get a
     403. `SEC_USER_AGENT` env var, enforced at client construction.
  2. Max 10 req/sec. The shared token bucket is configured at 8/s.

The `filed` field on a submission is the date a number actually became public,
which is what makes point-in-time reads honest.

Fundamentals do NOT come from here -- they come from the bulk quarterly datasets
via `src/ingest/sec_datasets.py` and `src/ingest/xbrl.py`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov"
WWW_URL = "https://www.sec.gov"

# Filing forms, used to pick periodic reports out of a submissions payload.
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
    # 24h TTL: submissions (SIC + recent filings) are near-static intraday, and
    # without this the source-"sec" client has no CACHE_TTL entry so it never
    # cached -- every SIC fetch and every Stage-3 SEC call re-hit the network.
    return await client.get_json(
        f"/submissions/CIK{pad_cik(cik)}.json", cache_ttl=24 * 3600
    )


def parse_sic(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull (sic, sicDescription) from a submissions payload. Pure/testable."""
    if not isinstance(payload, dict):
        return None, None
    sic = payload.get("sic")
    desc = payload.get("sicDescription")
    sic = str(sic).strip() if sic not in (None, "") else None
    desc = str(desc).strip()[:160] if desc else None
    return sic, desc


async def fetch_sic(client: APIClient, cik: str | int) -> tuple[str | None, str | None]:
    """SIC code + description for one company. Cached by the client (near-static)."""
    try:
        payload = await fetch_submissions(client, cik)
    except Exception as exc:  # noqa: BLE001 - one bad CIK is survivable
        log.debug("sec_sic_failed", cik=str(cik), error=str(exc)[:200])
        return None, None
    return parse_sic(payload)


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


# NOTE: the XBRL tag->metric map and the per-CIK companyconcept crawl that used
# to live here are gone, along with the Form 4 parser the deleted catalyst stage
# needed. Fundamentals now come from the bulk quarterly datasets, and the ONLY
# tag->metric mapping in this codebase is `src/ingest/xbrl.CONCEPTS`. Two
# competing concept maps is how the extraction drifted wrong in the first place;
# there must not be a second one.
