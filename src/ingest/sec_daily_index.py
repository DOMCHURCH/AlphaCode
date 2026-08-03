"""SEC daily index: all company filings for a given day.

The daily index is the source of truth for event-driven filtering. It is lighter-
weight than querying per-company submissions and gives us filings as they are
published, not as the historical submissions API presents them (which sorts by
filing_date, not accession order).

Access patterns (one per date):
  https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{n}/company.{YYYYMMDD}.idx
  https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{n}/form.{YYYYMMDD}.idx
  https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{n}/master.{YYYYMMDD}.idx

The .idx files are pipe-separated with a header row, then one row per filing.
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


async def fetch_daily_index(client: APIClient, date: dt.date) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch all filings for one trading day from the SEC daily index.

    Tries multiple file formats (company.idx, form.idx, master.idx) in order.
    Returns rows from the daily index file, which is pipe-separated with fields:
    CIK, Company Name, Form Type, Date Filed, Accession Number, and others.

    Returns:
        Tuple of (rows, diagnostics) where diagnostics contains the URL and HTTP
        status for debugging.
    """
    year = date.year
    quarter = (date.month - 1) // 3 + 1
    date_str = date.strftime("%Y%m%d")

    # Try multiple file variants
    file_variants = [
        f"/Archives/edgar/daily-index/{year}/QTR{quarter}/company.{date_str}.idx",
        f"/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{date_str}.idx",
        f"/Archives/edgar/daily-index/{year}/QTR{quarter}/master.{date_str}.idx",
    ]

    diag = {
        "date": str(date),
        "urls_tried": file_variants,
        "raw_bytes": 0,
        "total_lines": 0,
        "status_code": None,
        "error": None,
    }

    for path in file_variants:
        try:
            text = await client.get_json(path, expect_json=False)
            diag["raw_bytes"] = len(text.encode()) if text else 0
            diag["status_code"] = 200
        except Exception as exc:
            diag["error"] = str(exc)[:200]
            log.debug(
                "daily_index_fetch_failed",
                date=str(date),
                path=path,
                error=str(exc)[:200],
            )
            continue

        if not text or len(text.strip().split("\n")) < 2:
            log.debug("daily_index_empty", date=str(date), path=path)
            diag["error"] = "Empty or too short response"
            continue

        # Parse pipe-separated file: skip header, extract columns
        lines = text.strip().split("\n")
        diag["total_lines"] = len(lines)
        if len(lines) < 2:
            continue

        # Expected header: CIK|Company Name|Form Type|Date Filed|Accession Number|...
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
                "accession": parts[4].strip() if len(parts) > 4 else None,
            })

        log.info("daily_index_loaded", date=str(date), rows=len(rows), path=path)
        diag["successful_path"] = path
        return rows, diag

    # All variants failed
    log.warning("daily_index_all_formats_failed", date=str(date))
    return [], diag


async def extract_daily_index_filings(
    client: APIClient,
    date: dt.date,
    ticker_to_cik: dict[str, str],
    tracked_forms: set[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract filings for tickers in our universe from the daily index.

    Maps CIK to ticker, filters to tracked forms (8-K, 10-Q, etc), and
    returns enriched filing events ready for Stage 1 ranking.

    Returns:
        Tuple of (filings, diagnostics) where diagnostics includes parse results.
    """
    raw_filings, diag = await fetch_daily_index(client, date)
    cik_to_ticker = {v.lower(): k for k, v in ticker_to_cik.items()}

    diag["total_filings_in_index"] = len(raw_filings)
    diag["filings_after_form_filter"] = 0
    diag["filings_in_universe"] = 0

    filings = []
    for row in raw_filings:
        cik = row["cik"].strip().lstrip("0") or None
        if not cik:
            continue

        form = (row["form_type"] or "").strip()
        if form not in tracked_forms:
            continue

        diag["filings_after_form_filter"] += 1

        if cik not in cik_to_ticker:
            continue

        ticker = cik_to_ticker[cik]
        try:
            filed = dt.date.fromisoformat(row["date_filed"][:10])
        except (ValueError, TypeError):
            continue

        diag["filings_in_universe"] += 1
        filings.append({
            "ticker": ticker,
            "cik": cik,
            "form": form,
            "filing_date": filed,
            "company_name": row["company_name"],
            "accession": row["accession"],
        })

    return filings, diag
