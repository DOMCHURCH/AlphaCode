#!/usr/bin/env python3
"""Dump raw num.txt rows from an SEC Financial Statement Data Set.

Diagnostic for the XBRL extraction rebuild. The current parser reads whatever
row it finds first for a tag, which is how a $4T bank reports $641B in assets
and a miner reports negative total assets -- those are dimensional (segment /
geography / legal-entity) rows and duration rows being read as consolidated
balances.

This script answers the question the parser never asked: in the real file, what
distinguishes the consolidated instant fact from every other row carrying the
same tag? It prints every column of every matching row, then reports which
columns vary, then shows what a consolidated-instant filter would actually
select.

Deliberately standalone: stdlib only (no pandas, no httpx), no database, no
imports from src/. Runs on a fresh clone with nothing installed but Python.

Usage:
    export SEC_USER_AGENT="Your Name your@email.com"
    python3 scripts/dump_jpm_raw_facts.py                      # JPM, 2026q1
    python3 scripts/dump_jpm_raw_facts.py --ticker JPM,FCX
    python3 scripts/dump_jpm_raw_facts.py --year 2026 --quarter 1 --ddate 20251231

SEC requires a descriptive User-Agent with a contact email; without one every
request is 403'd.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

DATASET_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip"

# CIKs for the five verification companies, unpadded.
KNOWN_CIKS = {
    "JPM": "19617",     # JPMorgan Chase
    "AAL": "6201",      # American Airlines Group
    "MSFT": "789019",   # Microsoft
    "WMT": "104169",    # Walmart
    "FCX": "831259",    # Freeport-McMoRan
}

# The two tags that expose the bug most clearly. Both are balance-sheet
# instants, so every legitimate consolidated row must carry qtrs == 0.
DEFAULT_TAGS = ("Assets", "StockholdersEquity")

# num.txt columns that carry dimensional breakdown. A consolidated fact has
# every one of these empty. Older datasets omit `segments` entirely, so the
# check is "empty or absent", never "column must exist".
DIMENSION_COLUMNS = ("coreg", "segments")

CACHE_DIR = Path(".sec-cache")


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        sys.exit(
            "ERROR: SEC_USER_AGENT must be set to a descriptive string with a\n"
            "contact email, or SEC returns 403 on every request.\n\n"
            '    export SEC_USER_AGENT="Your Name your@email.com"\n'
        )
    return ua


def download(year: int, quarter: int) -> Path:
    """Fetch one quarter's ZIP, caching it so re-runs are free."""
    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / f"{year}q{quarter}.zip"
    if dest.exists() and dest.stat().st_size > 1024:
        print(f"Using cached {dest} ({dest.stat().st_size:,} bytes)")
        return dest

    url = DATASET_URL.format(year=year, q=quarter)
    print(f"Downloading {url} ...")
    req = urllib.request.Request(
        url, headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
            while chunk := resp.read(1 << 20):
                fh.write(chunk)
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        sys.exit(
            f"ERROR: HTTP {exc.code} for {url}\n"
            f"       {exc.reason}\n"
            "       A 403 usually means SEC_USER_AGENT is missing or malformed.\n"
            "       A 404 usually means that quarter is not published yet."
        )
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        sys.exit(f"ERROR: could not reach sec.gov: {exc.reason}")

    size = dest.stat().st_size
    if size < 1024 or not zipfile.is_zipfile(dest):
        dest.unlink(missing_ok=True)
        sys.exit(
            f"ERROR: {url} did not return a valid ZIP ({size:,} bytes) -- "
            "moved URL, throttle, or an HTML error page."
        )
    print(f"Downloaded {size:,} bytes -> {dest}")
    return dest


def read_tsv(zf: zipfile.ZipFile, name: str):
    """Stream one TSV member as dicts. num.txt is large; never load it whole."""
    if name not in zf.namelist():
        sys.exit(f"ERROR: {name} missing from ZIP; members={zf.namelist()[:8]}")
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
        reader = csv.DictReader(text, delimiter="\t")
        header = reader.fieldnames or []
        for row in reader:
            yield header, row


def find_submissions(zf: zipfile.ZipFile, ciks: set[str]) -> dict[str, dict]:
    """adsh -> submission metadata, for the CIKs we care about."""
    subs: dict[str, dict] = {}
    for _header, row in read_tsv(zf, "sub.txt"):
        cik = (row.get("cik") or "").lstrip("0")
        if cik in ciks:
            subs[row["adsh"]] = row
    return subs


def collect_facts(
    zf: zipfile.ZipFile, adshs: set[str], tags: tuple[str, ...], ddate: str | None
) -> tuple[list[str], list[dict]]:
    """All num.txt rows for the target submissions and tags."""
    header: list[str] = []
    facts: list[dict] = []
    for hdr, row in read_tsv(zf, "num.txt"):
        if not header:
            header = list(hdr)
        if row.get("adsh") not in adshs:
            continue
        if row.get("tag") not in tags:
            continue
        if ddate and (row.get("ddate") or "") != ddate:
            continue
        facts.append(row)
    return header, facts


def is_consolidated(row: dict) -> bool:
    """Consolidated == every dimensional column empty or absent."""
    return all(not (row.get(col) or "").strip() for col in DIMENSION_COLUMNS)


def fmt_value(raw: str) -> str:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return raw or "(empty)"
    if abs(val) >= 1e9:
        return f"{val:,.0f}  (${val / 1e9:,.1f}B)"
    return f"{val:,.0f}"


def dump(header: list[str], facts: list[dict], tag: str, label: str) -> None:
    rows = [f for f in facts if f.get("tag") == tag]
    print()
    print("=" * 100)
    print(f"{label} -- tag: {tag}  ({len(rows)} rows)")
    print("=" * 100)

    if not rows:
        print("  (no rows -- this tag is absent for this filing/period)")
        return

    for i, row in enumerate(rows, 1):
        kind = "CONSOLIDATED" if is_consolidated(row) else "DIMENSIONAL"
        qtrs = (row.get("qtrs") or "").strip()
        fact_type = "instant" if qtrs == "0" else f"duration ({qtrs} qtr)"
        print(f"\n  Row {i}  [{kind}, {fact_type}]")
        for col in header:
            val = (row.get(col) or "").strip()
            if not val:
                continue
            shown = fmt_value(val) if col == "value" else val
            marker = "  <-- DIMENSION" if col in DIMENSION_COLUMNS else ""
            print(f"    {col:<12} = {shown}{marker}")

    # Which columns actually vary? That is the answer to "what distinguishes
    # consolidated from dimensional in the real file".
    varying = {}
    for col in header:
        seen = {(r.get(col) or "").strip() for r in rows}
        if len(seen) > 1:
            varying[col] = sorted(seen)

    print(f"\n  --- columns that vary across these {len(rows)} rows ---")
    if not varying:
        print("  (none -- every row is identical on all columns)")
    else:
        for col, values in varying.items():
            flag = "  <-- DIMENSION" if col in DIMENSION_COLUMNS else ""
            print(f"    {col} ({len(values)} distinct){flag}")
            for v in values[:8]:
                print(f"      - {v or '(empty)'}")
            if len(values) > 8:
                print(f"      ... and {len(values) - 8} more")

    # What the rule-one filter would select. If exactly one row survives and its
    # value is the real consolidated figure, the rule is right.
    survivors = [r for r in rows if is_consolidated(r) and (r.get("qtrs") or "").strip() == "0"]
    print("\n  --- what a consolidated-instant filter selects ---")
    print(f"    {len(survivors)} of {len(rows)} rows survive "
          "(all dimension columns empty AND qtrs == 0)")
    for r in survivors:
        print(f"      ddate={r.get('ddate')}  uom={r.get('uom')}  "
              f"value={fmt_value(r.get('value', ''))}")
    if len(survivors) != 1:
        print("    NOTE: a correct filter should leave exactly ONE row per "
              "(tag, ddate, uom).")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Dump raw num.txt rows from an SEC Financial Statement Data Set."
    )
    ap.add_argument("--year", type=int, default=2026, help="dataset year (default 2026)")
    ap.add_argument("--quarter", type=int, default=1, help="dataset quarter 1-4 (default 1)")
    ap.add_argument(
        "--ticker",
        default="JPM",
        help=f"comma-separated, from {sorted(KNOWN_CIKS)} (default JPM)",
    )
    ap.add_argument("--cik", default="", help="comma-separated raw CIKs, overrides --ticker")
    ap.add_argument(
        "--tags",
        default=",".join(DEFAULT_TAGS),
        help=f"comma-separated XBRL tags (default {','.join(DEFAULT_TAGS)})",
    )
    ap.add_argument(
        "--ddate", default="", help="restrict to one period end, YYYYMMDD (e.g. 20251231)"
    )
    args = ap.parse_args()

    if args.cik:
        ciks = {c.strip().lstrip("0") for c in args.cik.split(",") if c.strip()}
        cik_to_ticker = {c: c for c in ciks}
    else:
        tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]
        unknown = [t for t in tickers if t not in KNOWN_CIKS]
        if unknown:
            sys.exit(f"ERROR: unknown ticker(s) {unknown}; known: {sorted(KNOWN_CIKS)}")
        cik_to_ticker = {KNOWN_CIKS[t]: t for t in tickers}
        ciks = set(cik_to_ticker)

    tags = tuple(t.strip() for t in args.tags.split(",") if t.strip())

    zip_path = download(args.year, args.quarter)

    with zipfile.ZipFile(zip_path) as zf:
        print(f"\nZIP members: {zf.namelist()}")

        print(f"\nScanning sub.txt for CIK(s) {sorted(ciks)} ...")
        subs = find_submissions(zf, ciks)
        if not subs:
            print(
                f"\nNo submissions for those CIKs in {args.year}q{args.quarter}.\n"
                "A fiscal-year-end 10-K lands in the FOLLOWING quarter's dataset:\n"
                "  FY2025 (period 20251231) -> filed ~Feb 2026 -> 2026q1\n"
                "Try a different --year/--quarter."
            )
            return 1

        print(f"Found {len(subs)} submission(s):")
        for adsh, s in sorted(subs.items(), key=lambda kv: kv[1].get("filed", "")):
            who = cik_to_ticker.get((s.get("cik") or "").lstrip("0"), "?")
            print(
                f"  {who:<5} {s.get('form','?'):<7} period={s.get('period','?')} "
                f"filed={s.get('filed','?')} fp={s.get('fp','?')} adsh={adsh}"
            )

        print(f"\nScanning num.txt for tags {list(tags)}"
              + (f" at ddate={args.ddate}" if args.ddate else "")
              + " ... (this reads a large file, ~30s)")
        header, facts = collect_facts(zf, set(subs), tags, args.ddate or None)

    print(f"\nnum.txt columns: {header}")
    print(f"Matching rows: {len(facts)}")
    if not facts:
        print(
            "\nNo matching rows. If you passed --ddate, the period end may be "
            "rounded to a different month end, or absent from this quarter's file."
        )
        return 1

    for adsh, s in sorted(subs.items(), key=lambda kv: kv[1].get("filed", "")):
        who = cik_to_ticker.get((s.get("cik") or "").lstrip("0"), "?")
        sub_facts = [f for f in facts if f.get("adsh") == adsh]
        if not sub_facts:
            continue
        label = f"{who} {s.get('form','?')} period={s.get('period','?')} filed={s.get('filed','?')}"
        for tag in tags:
            dump(header, sub_facts, tag, label)

    print("\n" + "=" * 100)
    print("Paste the output above. The `columns that vary` and `what a "
          "consolidated-instant\nfilter selects` sections are the ones that "
          "determine the new parser's filter.")
    print("=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
