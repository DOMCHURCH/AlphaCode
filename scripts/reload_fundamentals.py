#!/usr/bin/env python3
"""Wipe the fundamentals table and reload it through the rebuilt extractor.

The existing rows came from the old parser, which took whichever num.txt row
came first and so stored segment and rollforward facts as company totals. They
cannot be repaired in place -- a wrong number is indistinguishable from a right
one once the tag and dimensions are discarded. So: delete, reload, verify.

    python3 scripts/reload_fundamentals.py --wipe --quarters 7
    python3 scripts/reload_fundamentals.py --verify-only

Needs DATABASE_URL and SEC_USER_AGENT in the environment, same as the service.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Running `python3 scripts/reload_fundamentals.py` puts scripts/ on sys.path, not
# the repo root, so `import src...` would fail. Fix that before importing src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Expected consolidated figures for the five verification companies.
#
# JPM's two numbers are exact: they were read off the real num.txt dump for
# period 2025-12-31, and are the consolidated rows the rebuilt filter must now
# select. The rest are order-of-magnitude expectations -- they catch a parser
# reading the wrong fact, not a small reporting difference.
#
# Note JPM total assets is $4.42T, not the ~$4.0T working estimate: the dump is
# ground truth here, so it is what we check against.
EXPECTED = {
    "JPM": {
        "total_assets": (4_424_900_000_000, "exact, from the num.txt dump"),
        "total_equity": (362_438_000_000, "exact, from the num.txt dump"),
    },
    "AAL": {
        # American Airlines genuinely runs a stockholders' deficit. The sign is
        # the point: negative here is correct, and must not be "fixed".
        "total_equity": (-3_900_000_000, "approx; negative is correct for AAL"),
    },
    "MSFT": {
        "total_assets": (560_000_000_000, "approx"),
        "total_equity": (300_000_000_000, "approx"),
    },
    "WMT": {"total_assets": (260_000_000_000, "approx")},
    "FCX": {"total_assets": (55_000_000_000, "approx; must be positive")},
}

TOLERANCE = 0.10


def fmt(v: float | None) -> str:
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e12:
        return f"${v / 1e12:,.3f}T"
    if a >= 1e9:
        return f"${v / 1e9:,.1f}B"
    if a >= 1e6:
        return f"${v / 1e6:,.1f}M"
    return f"${v:,.0f}"


def wipe() -> int:
    """Delete every fundamentals row. Returns how many were removed."""
    from sqlalchemy import delete, func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as session:
        before = session.execute(select(func.count()).select_from(Fundamental)).scalar_one()
        session.execute(delete(Fundamental))
    print(f"Deleted {before:,} fundamentals rows.")
    return before


async def reload(quarters: int) -> int:
    from src.backfill import backfill_fundamentals, get_extraction_reports

    print(f"Reloading {quarters} quarters through the rebuilt extractor ...")
    rows = await backfill_fundamentals(quarters=quarters)
    print(f"\nWrote {rows:,} rows.\n")

    reports = get_extraction_reports()
    if reports:
        print("=" * 78)
        print("EXTRACTION REPORT")
        print("=" * 78)
    for q in sorted(reports, reverse=True):
        r = reports[q]
        print(f"\n{q}:")
        print(f"  tag matches          {r['tag_matched']:>12,}")
        print(f"  dropped dimensional  {r['dropped_dimensional']:>12,}   <- the bug")
        print(f"  dropped wrong qtrs   {r['dropped_wrong_qtrs']:>12,}")
        print(f"    of which YTD cum.  {r.get('dropped_ytd_cumulative', 0):>12,}"
              "   (qtrs 2/3, cumulative not quarterly)")
        hist = r.get("duration_qtrs_seen") or {}
        if hist:
            shown = "  ".join(f"qtrs={k}: {v:,}" for k, v in sorted(hist.items()))
            print(f"  duration qtrs seen   {shown}")
            print("    ^ what filers actually report. We keep 1 and 4 only. If a "
                  "duration\n      metric appears ONLY at 2/3, we are losing it -- "
                  "check coverage below.")
        print(f"  dropped non-USD      {r['dropped_non_usd']:>12,}")
        print(f"  dropped alias dupes  {r['dropped_alias_duplicate']:>12,}")
        print(f"  kept                 {r['kept']:>12,}")
        print(f"  periods validated    {r['validated_periods']:>12,}")
        print(f"  periods rejected     {r['rejected_periods']:>12,}"
              f"   ({r['reject_rate'] * 100:.2f}%)")
        for rej in r.get("rejections", [])[:10]:
            print(f"    REJECT {rej['ticker']:<6} {rej['metric']:<24} "
                  f"{rej['rule']:<32} {fmt(rej.get('value'))}")
        for fl in r.get("flags", [])[:10]:
            print(f"    FLAG   {fl.get('ticker', ''):<6} {fl.get('rule')}")
    return rows


def verify() -> bool:
    """Check the five companies. Returns True only if every check passes."""
    from src.company.balancesheet import get_balance_sheet

    print("\n" + "=" * 78)
    print("VERIFICATION — actual vs expected")
    print("=" * 78)

    all_ok = True
    for ticker, checks in EXPECTED.items():
        print(f"\n{ticker}")
        bs = get_balance_sheet(ticker)
        if bs is None:
            print("  NO DATA — the reload wrote nothing for this ticker")
            all_ok = False
            continue

        print(f"  period {bs.period_end}  filed {bs.filing_date}")
        actual = {
            "total_assets": bs.assets["total_assets"],
            "total_equity": bs.equity["shareholders_equity"],
        }
        for metric, (expected, note) in checks.items():
            cell = actual[metric]
            got = None if cell.missing else cell.value
            if got is None:
                print(f"  {metric:<14} MISSING           expected {fmt(expected)}  ({note})")
                all_ok = False
                continue
            drift = abs(got - expected) / abs(expected)
            ok = drift <= TOLERANCE
            all_ok = all_ok and ok
            print(f"  {metric:<14} {fmt(got):>14}   expected {fmt(expected):>14}   "
                  f"{'OK' if ok else 'OFF'} {drift * 100:5.1f}%   ({note})")

        ta = None if bs.assets["total_assets"].missing else bs.assets["total_assets"].value
        if ta is not None and ta <= 0:
            print(f"  IMPOSSIBLE: total assets is {fmt(ta)}")
            all_ok = False

        tl = bs.liabilities["total_liabilities"]
        te = bs.equity["shareholders_equity"]
        if ta and not tl.missing and not te.missing:
            rhs = tl.value + te.value
            drift = abs(ta - rhs) / ta * 100
            print(f"  A = L + E      {fmt(ta)} vs {fmt(rhs)}   "
                  f"{'OK' if drift < 1 else 'OFF'} {drift:.2f}%")
        else:
            print("  A = L + E      not checkable (total_liabilities not reported)")

        for issue in bs.data_quality_issues:
            print(f"  ISSUE: {issue}")

    print("\n" + "=" * 78)
    print("PASS — every company reconciles" if all_ok else
          f"FAIL — at least one company is off by more than {TOLERANCE:.0%}. "
          "The parser is still wrong. Do not build on these numbers.")
    print("=" * 78)
    return all_ok


def coverage() -> None:
    from sqlalchemy import func, select

    from src.company.balancesheet import BALANCE_SHEET_CONCEPTS
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as session:
        total = session.execute(
            select(func.count(func.distinct(Fundamental.ticker)))
        ).scalar_one() or 0
        rows = session.execute(
            select(Fundamental.metric, func.count(func.distinct(Fundamental.ticker)))
            .group_by(Fundamental.metric)
        ).all()

    have = dict(rows)
    print("\n" + "=" * 78)
    print(f"COVERAGE — {total:,} tickers have at least one fundamentals row")
    print("=" * 78)
    for concept, metric in sorted(BALANCE_SHEET_CONCEPTS.items()):
        n = have.get(metric, 0)
        pct = 100.0 * n / total if total else 0.0
        print(f"  {concept:<26} {n:>7,}  {pct:5.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wipe", action="store_true",
                    help="delete every fundamentals row before reloading")
    ap.add_argument("--quarters", type=int, default=7, help="quarters to load (default 7)")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip the reload; just check the five companies")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        sys.exit("ERROR: DATABASE_URL is not set.")

    if not args.verify_only:
        if not os.environ.get("SEC_USER_AGENT", "").strip():
            sys.exit('ERROR: SEC_USER_AGENT is not set (e.g. "Your Name you@email.com").')
        if args.wipe:
            wipe()
        else:
            print("Not wiping (pass --wipe). Reloading on top of existing rows.")
        asyncio.run(reload(args.quarters))

    coverage()
    return 0 if verify() else 1


if __name__ == "__main__":
    raise SystemExit(main())
