"""Dump raw num.txt rows for one company, straight out of an SEC quarterly ZIP.

This is the tool that settled the JPM bug: it shows every column of every row
carrying a tag, reports which columns actually vary across those rows, and shows
what the consolidated-instant filter selects. Reading the file is how a disputed
number gets resolved -- never by adjusting the expectation to match the output.

Server-side twin of `scripts/dump_jpm_raw_facts.py`, so the same inspection is
available from a phone.

num.txt carries ~2M rows per quarter. It is streamed through `csv` and filtered
row by row rather than loaded into a DataFrame, because materialising the whole
member would cost gigabytes and can OOM a small container.
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

import structlog

from src.ingest.xbrl import DIMENSION_COLUMNS

log = structlog.get_logger(__name__)

# Unpadded CIKs for companies we routinely check. A convenience, not a
# whitelist -- any CIK can be passed explicitly.
KNOWN_CIKS = {
    "JPM": "19617",
    "AAL": "6201",
    "MSFT": "789019",
    "WMT": "104169",
    "FCX": "831259",
}


def _stream(zf: zipfile.ZipFile, name: str):
    """Yield (fieldnames, row) for one TSV member without materialising it."""
    if name not in zf.namelist():
        raise RuntimeError(f"SEC dataset ZIP missing {name}; members={zf.namelist()[:8]}")
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text, delimiter="\t")
        header = reader.fieldnames or []
        for row in reader:
            yield header, row


def _cell(row: dict[str, Any], col: str) -> str:
    return str(row.get(col) or "").strip()


def dump_company_facts(
    zbytes: bytes,
    ticker: str,
    tags: tuple[str, ...],
    ddate: str | None = None,
    cik: str | None = None,
    max_rows: int = 200,
) -> dict[str, Any]:
    """Every num.txt row for `ticker` and `tags` in one quarterly dataset."""
    ticker = ticker.upper()
    raw_cik = (cik or KNOWN_CIKS.get(ticker) or "").lstrip("0")
    if not raw_cik:
        return {
            "error": f"No CIK known for {ticker}. Pass cik= explicitly.",
            "known": sorted(KNOWN_CIKS),
        }

    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        submissions: list[dict[str, Any]] = []
        adshs: set[str] = set()
        sub_header: list[str] = []
        for header, row in _stream(zf, "sub.txt"):
            sub_header = header
            if _cell(row, "cik").lstrip("0") != raw_cik:
                continue
            adshs.add(row["adsh"])
            submissions.append({
                "adsh": row.get("adsh"), "form": row.get("form"),
                "period": row.get("period"), "filed": row.get("filed"),
                "fp": row.get("fp"),
            })

        if not adshs:
            return {
                "ticker": ticker, "cik": raw_cik,
                "error": (
                    f"No submissions for CIK {raw_cik} in this dataset. A "
                    "fiscal-year-end 10-K lands in the FOLLOWING quarter's file "
                    "(FY2025 -> filed ~Feb 2026 -> 2026q1). Try another quarter."
                ),
                "sub_columns": sub_header,
            }

        num_header: list[str] = []
        matched: dict[str, list[dict[str, Any]]] = {t: [] for t in tags}
        counts: dict[str, int] = dict.fromkeys(tags, 0)
        for header, row in _stream(zf, "num.txt"):
            num_header = header
            if row.get("adsh") not in adshs:
                continue
            tag = row.get("tag")
            if tag not in matched:
                continue
            if ddate and _cell(row, "ddate") != str(ddate):
                continue
            counts[tag] += 1
            if len(matched[tag]) < max_rows:
                matched[tag].append(dict(row))

    dim_cols = [c for c in DIMENSION_COLUMNS if c in num_header]

    by_tag: dict[str, Any] = {}
    for tag in tags:
        rows = matched[tag]
        if not rows:
            by_tag[tag] = {"row_count": 0, "rows": [], "note": "no rows for this tag"}
            continue

        dumped = []
        for r in rows:
            cell = {c: _cell(r, c) for c in num_header}
            cell["_consolidated"] = all(not _cell(r, c) for c in dim_cols)
            cell["_instant"] = _cell(r, "qtrs") == "0"
            dumped.append(cell)

        # Which columns actually differ across these rows -- the empirical answer
        # to "what separates the consolidated row from the rest".
        varying = {}
        for col in num_header:
            vals = sorted({_cell(r, col) for r in rows})
            if len(vals) > 1:
                varying[col] = vals[:12]

        survivors = [d for d in dumped if d["_consolidated"] and d["_instant"]]
        by_tag[tag] = {
            "row_count": counts[tag],
            "rows_shown": len(dumped),
            "rows": dumped,
            "varying_columns": varying,
            "dimension_columns_present": dim_cols,
            "consolidated_instant": [
                {"ddate": s.get("ddate"), "uom": s.get("uom"),
                 "qtrs": s.get("qtrs"), "value": s.get("value")}
                for s in survivors
            ],
            "filter_selects_exactly_one": len(survivors) == 1,
        }

    log.info("raw_facts_dumped", ticker=ticker, cik=raw_cik,
             tags=list(tags), counts=counts)
    return {
        "ticker": ticker,
        "cik": raw_cik,
        "ddate_filter": ddate,
        "num_columns": num_header,
        "submissions": submissions,
        "by_tag": by_tag,
    }
