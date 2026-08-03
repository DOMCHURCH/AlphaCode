"""SEC daily index: all company filings for a given day.

The daily index is the source of truth for event-driven filtering. It is lighter-
weight than querying per-company submissions and gives us filings as they are
published, not as the historical submissions API presents them (which sorts by
filing_date, not accession order).

Access pattern:
  https://www.sec.gov/Archives/edgar/daily-index/{year}/Q{quarter}/company.0.txt

The .txt is tab-separated with a header row, then one row per filing.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pandas as pd
import structlog

from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError

log = structlog.get_logger(__name__)

WWW_URL = "https://www.sec.gov"


def make_client(concurrency: int = 2) -> APIClient:
    """Daily index is lighter than submissions, but still rate-limited."""
    ua = get_settings().sec_user_agent
    if not ua or "@" not in ua:
        raise PermanentAPIError(
            "SEC_USER_AGENT must be set to 'Name your@email.com' or SEC returns 403"
        )
    return APIClient(
        "sec_daily",
        WWW_URL,
        headers={
            "User-Agent": ua,
            "Accept-Encoding": "gzip, deflate",
            "Host": "www.sec.gov",
        },
        concurrency=concurrency,
        cache_ttl=24 * 3600,
    )


async def fetch_daily_index(client: APIClient, date: dt.date) -> list[dict[str, Any]]:
    """Fetch all filings for one trading day from the SEC daily index.

    Returns rows from the daily index file, which is tab-separated with fields:
    CIK, Company Name, Form Type, Date Filed, Filename, and others.

    The SEC publishes one file per day; it covers all filings for that day.
    """
    year = date.year
    quarter = (date.month - 1) // 3 + 1
    path = f"/Archives/edgar/daily-index/{year}/Q{quarter}/company.0.txt"

    try:
        text = await client.get_json(path, expect_json=False)
    except Exception as exc:
        log.warning(
            "daily_index_fetch_failed",
            date=str(date),
            year=year,
            quarter=quarter,
            error=str(exc)[:200],
        )
        return []

    if not text or len(text.strip().split("\n")) < 2:
        log.debug("daily_index_empty", date=str(date), path=path)
        return []

    # Parse tab-separated file: skip header, extract columns
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return []

    # Expected header: CIK|Company Name|Form Type|Date Filed|Filename
    header = lines[0].split("|")
    rows = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < 5:
            continue
        rows.append({
            "cik": parts[0].strip(),
            "company_name": parts[1].strip(),
            "form_type": parts[2].strip(),
            "date_filed": parts[3].strip(),
            "filename": parts[4].strip() if len(parts) > 4 else None,
        })

    log.info("daily_index_loaded", date=str(date), rows=len(rows))
    return rows


async def extract_daily_index_filings(
    client: APIClient,
    date: dt.date,
    ticker_to_cik: dict[str, str],
    tracked_forms: set[str],
) -> list[dict[str, Any]]:
    """Extract filings for tickers in our universe from the daily index.

    Maps CIK to ticker, filters to tracked forms (8-K, 10-Q, etc), and
    returns enriched filing events ready for Stage 1 ranking.
    """
    raw_filings = await fetch_daily_index(client, date)
    cik_to_ticker = {v.lower(): k for k, v in ticker_to_cik.items()}

    filings = []
    for row in raw_filings:
        cik = row["cik"].strip().lstrip("0") or None
        if not cik or cik not in cik_to_ticker:
            continue

        form = (row["form_type"] or "").strip()
        if form not in tracked_forms:
            continue

        ticker = cik_to_ticker[cik]
        try:
            filed = dt.date.fromisoformat(row["date_filed"][:10])
        except (ValueError, TypeError):
            continue

        filings.append({
            "ticker": ticker,
            "cik": cik,
            "form": form,
            "filing_date": filed,
            "company_name": row["company_name"],
            "accession": row["filename"][:32] if row["filename"] else None,
        })

    return filings
