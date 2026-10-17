"""Find month-end rows that may be rounded, by comparing to EDGAR's true reportDates.

SEC's bulk Financial Statement Data Sets round every period end to the NEAREST
month end. The XBRL frames sometimes carry the real date, and those are handled
elsewhere. For OLDER filings only the rounded month-end row exists. This script
identifies "REDATE candidates": month-end period_ends that may have been rounded,
so a human can review them.

A candidate is a (ticker, filing_date) pair where:
  1. The ticker has at least one NON-month-end period_end in its total_assets rows
     (it is a 52/53-week-style filer)
  2. That filing has a total_assets row whose period_end is a month end
  3. That month end has NO same-filing non-month-end twin 1..15 days away
     (i.e. is not already known to be rounded via period_aliases)

For each candidate, the script looks up the true reportDate from EDGAR's
submissions API and compares it to the month-end period_end in the database.
The result is a CSV suitable for manual review.

NOTE: This script reads the database and the SEC, and writes a CSV file.
It has NO database write path of any kind and NO apply mode.
All lookup is read-only (SELECT only on Postgres).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeAlias
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Adjust path to import from src/
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from src.company.periods import WINDOW_DAYS, is_month_end, period_aliases
from src.config.settings import get_settings
from src.storage.models import Fundamental
from src.storage.script_db import make_engine, read_url, scrub

CandidateTuple: TypeAlias = tuple[str, dt.date, dt.date]  # ticker, filing_date, period_end


def find_candidates(engine: Engine) -> list[CandidateTuple]:
    """Derive candidates from the database.

    Returns (ticker, filing_date, period_end) tuples where period_end is a month
    end that has no non-month-end twin 1..15 days away IN THE SAME FILING, for a
    ticker that reports at least one non-month-end period somewhere (a 52/53-week
    style filer). Calendar-aligned filers are never candidates. The twin test is
    per filing on purpose: a date that another filing happens to have a twin for
    is still unproven in THIS filing, so it is listed (and EDGAR settles it).
    """
    insp = inspect(engine)
    if "fundamentals" not in insp.get_table_names():
        return []

    SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with SessionFactory() as session:
        rows = session.execute(
            select(Fundamental.ticker, Fundamental.period_end, Fundamental.filing_date)
            .where(Fundamental.metric == "total_assets")
            .distinct()
        ).all()
        session.rollback()

    by_ticker: dict[str, dict[dt.date, set[dt.date]]] = {}
    for ticker, period_end, filing_date in rows:
        by_ticker.setdefault(ticker, {}).setdefault(filing_date, set()).add(period_end)

    candidates: list[CandidateTuple] = []
    for ticker in sorted(by_ticker):
        filings = by_ticker[ticker]
        if all(is_month_end(pe) for pes in filings.values() for pe in pes):
            continue  # calendar-aligned filer
        for filing_date in sorted(filings):
            pes = filings[filing_date]
            twinned = period_aliases([(pe, filing_date) for pe in pes])
            for period_end in sorted(pes):
                if is_month_end(period_end) and period_end not in twinned:
                    candidates.append((ticker, filing_date, period_end))
    return candidates


def lookup_candidates(
    candidates: list[CandidateTuple],
    fetch: Callable[[str], Any],
    sleep: Callable[[float], None] = time.sleep,
    on_ticker_done: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Look up EDGAR reportDates for each candidate.

    Args:
        candidates: (ticker, filing_date, period_end) tuples
        fetch: callable(url) -> parsed JSON
        sleep: callable(seconds) for rate limiting
        on_ticker_done: called with each ticker's rows as soon as they are known

    Returns:
        List of dicts with keys: ticker, cik, filing_date, db_period_end,
        edgar_report_date, form, accession, diff_days, status, note
    """
    # Map ticker -> CIK
    ticker_to_cik = _build_ticker_cik_map(fetch, sleep)

    results: list[dict[str, Any]] = []
    submissions_cache: dict[str, Any] = {}  # cik -> parsed submissions JSON

    # Group by ticker to minimize lookups
    by_ticker = {}
    for ticker, filing_date, period_end in candidates:
        by_ticker.setdefault(ticker, []).append((filing_date, period_end))

    tickers_list = sorted(by_ticker.keys())
    flushed = 0

    def flush() -> None:
        nonlocal flushed
        if on_ticker_done is not None and len(results) > flushed:
            on_ticker_done(results[flushed:])
        flushed = len(results)

    for i, ticker in enumerate(tickers_list):
        flush()
        if (i + 1) % 25 == 0:
            print(f"Processed {i + 1}/{len(tickers_list)} tickers", file=sys.stderr)

        cik = ticker_to_cik.get(ticker)
        if not cik:
            for filing_date, period_end in by_ticker[ticker]:
                results.append({
                    "ticker": ticker,
                    "cik": None,
                    "filing_date": filing_date.isoformat(),
                    "db_period_end": period_end.isoformat(),
                    "edgar_report_date": None,
                    "form": None,
                    "accession": None,
                    "diff_days": None,
                    "status": "no_cik",
                    "note": "",
                })
            continue

        # Fetch submissions for this CIK
        if cik not in submissions_cache:
            sleep(0.2)  # Rate limit: max 5 req/s
            submissions = _fetch_submissions(cik, fetch)
            submissions_cache[cik] = submissions

        submissions = submissions_cache[cik]
        if submissions is None:
            for filing_date, period_end in by_ticker[ticker]:
                results.append({
                    "ticker": ticker,
                    "cik": cik,
                    "filing_date": filing_date.isoformat(),
                    "db_period_end": period_end.isoformat(),
                    "edgar_report_date": None,
                    "form": None,
                    "accession": None,
                    "diff_days": None,
                    "status": "error",
                    "note": "Failed to fetch submissions",
                })
            continue

        # Process candidates for this ticker
        for filing_date, period_end in by_ticker[ticker]:
            row = _match_filing(
                ticker, cik, filing_date, period_end, submissions, fetch, sleep
            )
            results.append(row)

    flush()
    return results


def _build_ticker_cik_map(
    fetch: Callable[[str], Any],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Build ticker -> CIK mapping, fetching company_tickers.json once."""
    try:
        sleep(0.2)
        data = fetch("https://www.sec.gov/files/company_tickers.json")
        by_ticker = {}
        for row in data.values():
            ticker = str(row.get("ticker", "")).upper()
            cik = row.get("cik_str")
            if ticker and cik is not None:
                by_ticker[ticker] = str(cik).zfill(10)
        return by_ticker
    except Exception as exc:
        print(f"Warning: Failed to fetch company_tickers.json: {exc}", file=sys.stderr)
        return {}


def _fetch_submissions(cik: str, fetch: Callable[[str], Any]) -> dict[str, Any] | None:
    """Fetch submissions JSON for a CIK (10-digit zero-padded).

    Returns the parsed JSON (containing 'filings' with 'recent' and optionally
    'files' arrays), or None on error.
    """
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        return fetch(url)
    except Exception:
        return None


def _match_filing(
    ticker: str,
    cik: str,
    filing_date: dt.date,
    period_end: dt.date,
    submissions: dict[str, Any],
    fetch: Callable[[str], Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Match a candidate filing_date to EDGAR submissions.

    Returns a dict with all columns for the CSV.
    """
    filings = submissions.get("filings", {})
    recent = filings.get("recent", {})

    # Build list of filing entries
    filing_entries = []

    # Add entries from 'recent'
    if recent:
        accession_nums = recent.get("accessionNumber", [])
        filing_dates = recent.get("filingDate", [])
        report_dates = recent.get("reportDate", [])
        forms = recent.get("form", [])
        primary_docs = recent.get("primaryDocument", [])

        for i in range(len(accession_nums)):
            filing_entries.append({
                "accessionNumber": accession_nums[i] if i < len(accession_nums) else None,
                "filingDate": filing_dates[i] if i < len(filing_dates) else None,
                "reportDate": report_dates[i] if i < len(report_dates) else None,
                "form": forms[i] if i < len(forms) else None,
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else None,
            })

    # Check if we need to fetch older filings
    oldest_recent = min(
        (dt.datetime.strptime(e["filingDate"], "%Y-%m-%d").date()
         for e in filing_entries if e["filingDate"]),
        default=None
    )

    if oldest_recent and filing_date < oldest_recent and fetch:
        files = filings.get("files", [])
        for file_info in files:
            file_name = file_info.get("name")
            if file_name:
                try:
                    url = f"https://data.sec.gov/submissions/{file_name}"
                    if sleep:
                        sleep(0.2)
                    file_data = fetch(url)

                    if file_data:
                        accession_nums = file_data.get("accessionNumber", [])
                        filing_dates = file_data.get("filingDate", [])
                        report_dates = file_data.get("reportDate", [])
                        forms = file_data.get("form", [])
                        primary_docs = file_data.get("primaryDocument", [])

                        for i in range(len(accession_nums)):
                            filing_entries.append({
                                "accessionNumber": accession_nums[i] if i < len(accession_nums) else None,
                                "filingDate": filing_dates[i] if i < len(filing_dates) else None,
                                "reportDate": report_dates[i] if i < len(report_dates) else None,
                                "form": forms[i] if i < len(forms) else None,
                                "primaryDocument": primary_docs[i] if i < len(primary_docs) else None,
                            })
                except Exception:
                    pass

    # Find matching entries
    matching = []
    for entry in filing_entries:
        if entry["filingDate"]:
            try:
                fdate = dt.datetime.strptime(entry["filingDate"], "%Y-%m-%d").date()
                if fdate == filing_date:
                    form = entry.get("form", "")
                    if form and (form.startswith("10-Q") or form.startswith("10-K")):
                        matching.append(entry)
            except (ValueError, TypeError):
                pass

    if not matching:
        return {
            "ticker": ticker,
            "cik": cik,
            "filing_date": filing_date.isoformat(),
            "db_period_end": period_end.isoformat(),
            "edgar_report_date": None,
            "form": None,
            "accession": None,
            "diff_days": None,
            "status": "no_match",
            "note": "",
        }

    # Extract report dates from matching entries
    report_dates = []
    for entry in matching:
        if entry["reportDate"]:
            try:
                rdate = dt.datetime.strptime(entry["reportDate"], "%Y-%m-%d").date()
                report_dates.append(rdate)
            except (ValueError, TypeError):
                pass

    report_dates = sorted(set(report_dates))

    if len(report_dates) > 1:
        return {
            "ticker": ticker,
            "cik": cik,
            "filing_date": filing_date.isoformat(),
            "db_period_end": period_end.isoformat(),
            "edgar_report_date": report_dates[0].isoformat(),
            "form": matching[0].get("form"),
            "accession": matching[0].get("accessionNumber"),
            "diff_days": None,
            "status": "multiple",
            "note": ", ".join(d.isoformat() for d in report_dates),
        }

    if not report_dates:
        return {
            "ticker": ticker,
            "cik": cik,
            "filing_date": filing_date.isoformat(),
            "db_period_end": period_end.isoformat(),
            "edgar_report_date": None,
            "form": matching[0].get("form") if matching else None,
            "accession": matching[0].get("accessionNumber") if matching else None,
            "diff_days": None,
            "status": "no_match",
            "note": "No reportDate found",
        }

    edgar_report_date = report_dates[0]
    diff = (period_end - edgar_report_date).days

    if edgar_report_date == period_end:
        status = "same"
    elif not is_month_end(edgar_report_date) and 1 <= abs(diff) <= WINDOW_DAYS:
        status = "rounded"
    elif abs(diff) > WINDOW_DAYS:
        status = "far"
    else:
        status = "same"  # Both are month ends or other unexpected case

    return {
        "ticker": ticker,
        "cik": cik,
        "filing_date": filing_date.isoformat(),
        "db_period_end": period_end.isoformat(),
        "edgar_report_date": edgar_report_date.isoformat(),
        "form": matching[0].get("form"),
        "accession": matching[0].get("accessionNumber"),
        "diff_days": diff,
        "status": status,
        "note": (
            "EDGAR's reportDate is the filing's own (current) period; this date is another period the "
            "filing quotes (a comparative), so it cannot be checked here" if status == "far" else ""
        ),
    }


def write_csv(
    results: list[dict[str, Any]],
    output_path: Path,
    resume: bool = False,
) -> None:
    """Write results to CSV, with optional resume mode."""
    # Sort results by ticker, filing_date, period_end
    results.sort(key=lambda r: (r["ticker"], r["filing_date"], r["db_period_end"]))

    mode = "a" if resume else "w"
    with open(output_path, mode, newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "ticker", "cik", "filing_date", "db_period_end",
                "edgar_report_date", "form", "accession", "diff_days",
                "status", "note"
            ],
        )
        if not resume or output_path.stat().st_size == 0:
            writer.writeheader()
        writer.writerows(results)


def summarise(results: list[dict[str, Any]]) -> None:
    """Print summary of status counts and year breakdown."""
    status_counts: dict[str, int] = {}
    year_counts: dict[int, int] = {}

    for row in results:
        status = row.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

        try:
            year = dt.date.fromisoformat(row["filing_date"]).year
            year_counts[year] = year_counts.get(year, 0) + 1
        except (ValueError, KeyError):
            pass

    print("Status breakdown:")
    for status in sorted(status_counts.keys()):
        print(f"  {status}: {status_counts[status]}")

    print("\nBy filing year:")
    for year in sorted(year_counts.keys()):
        print(f"  {year}: {year_counts[year]}")

    print(f"\nTotal: {len(results)} rows")


def read_done(path: Path) -> set[tuple[str, str, str]]:
    """(ticker, filing_date, db_period_end) rows already in an existing CSV (for --resume)."""
    if not path.exists() or path.stat().st_size == 0:
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["ticker"], r["filing_date"], r["db_period_end"]) for r in csv.DictReader(f)}


FIELDS = ["ticker", "cik", "filing_date", "db_period_end", "edgar_report_date",
          "form", "accession", "diff_days", "status", "note"]


def append_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
        f.flush()


def main(argv: list[str] | None = None, *, fetch: Callable[[str], Any] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> int:
    """Read-only: candidates from the database, reportDates from EDGAR, results to a CSV."""
    parser = argparse.ArgumentParser(description="Look up EDGAR reportDates for month-end candidate rows (read-only)")
    parser.add_argument("--url-file", type=Path, help="File containing one line with the database URL")
    parser.add_argument("--out", type=Path, default=Path("candidates.csv"), help="Output CSV (default: candidates.csv)")
    parser.add_argument("--tickers", help="Comma-separated ticker list to restrict to (e.g. AAPL,NVDA)")
    parser.add_argument("--limit-tickers", type=int, help="Process at most this many tickers")
    parser.add_argument("--count-only", action="store_true", help="Print candidate counts; no network calls")
    parser.add_argument("--resume", action="store_true", help="Append to --out, skipping rows already in it")
    args = parser.parse_args(argv)

    url = None
    try:
        url = read_url(args.url_file, ("PERIOD_LOOKUP_DATABASE_URL",))
        if args.out.exists() and not args.resume and not args.count_only:
            print(f"Error: {args.out} exists; use --resume to append", file=sys.stderr)
            return 1
        engine = make_engine(url, readonly=True)
        try:
            candidates = find_candidates(engine)
        finally:
            engine.dispose()

        if args.tickers:
            allowed = {t.strip().upper() for t in args.tickers.split(",") if t.strip()}
            candidates = [c for c in candidates if c[0] in allowed]
        if args.limit_tickers:
            keep = set(sorted({c[0] for c in candidates})[: args.limit_tickers])
            candidates = [c for c in candidates if c[0] in keep]

        if args.count_only:
            by_year: dict[int, int] = {}
            for _t, filing_date, _pe in candidates:
                by_year[filing_date.year] = by_year.get(filing_date.year, 0) + 1
            print(f"Candidates: {len(candidates)} rows, {len({(c[0], c[1]) for c in candidates})} filings, "
                  f"{len({c[0] for c in candidates})} tickers")
            print("By filing year:")
            for year in sorted(by_year):
                print(f"  {year}: {by_year[year]}")
            return 0

        done = read_done(args.out) if args.resume else set()
        todo = [c for c in candidates if (c[0], c[1].isoformat(), c[2].isoformat()) not in done]
        print(f"{len(candidates)} candidates, {len(done)} already done, {len(todo)} to look up", file=sys.stderr)
        results = lookup_candidates(todo, fetch or _fetch_json_default, sleep,
                                    on_ticker_done=lambda rows: append_rows(args.out, rows))
        summarise(results)
        print(f"Results written to {args.out}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- scrubbed, then reported
        print(f"Error: {type(exc).__name__}: {scrub(exc, url)}", file=sys.stderr)
        return 1


def _fetch_json_default(url: str) -> Any:
    """Default JSON fetch implementation with retries and rate limiting."""
    ua = get_settings().sec_user_agent
    if not ua or "@" not in ua:
        raise ValueError(
            "SEC_USER_AGENT must be set to 'Name your@email.com' or SEC returns 403"
        )

    for attempt in range(3):
        try:
            req = Request(url, headers={
                "User-Agent": ua,
                "Accept-Encoding": "gzip, deflate"
            })
            with urlopen(req, timeout=30) as response:
                raw = response.read()
                if response.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                if attempt < 2:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait_time = float(retry_after) if retry_after else 2 ** (attempt + 1)
                    except ValueError:
                        wait_time = 2 ** (attempt + 1)
                    time.sleep(wait_time)
                    continue
            raise
        except Exception:
            raise

    raise RuntimeError("Failed to fetch after retries")


if __name__ == "__main__":
    raise SystemExit(main())
