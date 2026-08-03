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

# Reference figures, tolerance, and the verification logic all live in
# src/company/verify.py, shared with the GET /admin/verify endpoint so the two
# cannot drift apart.
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
    from src.backfill import wipe_fundamentals

    before = wipe_fundamentals()
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
    from src.company.verify import verify_companies

    result = verify_companies()
    print("\n" + "=" * 78)
    print("VERIFICATION — actual vs expected")
    print("=" * 78)

    for ticker, c in result["companies"].items():
        print(f"\n{ticker}")
        if not c["found"]:
            print(f"  NO DATA — {c.get('reason', '')}")
            continue
        print(f"  period {c['period_end']}  filed {c['filing_date']}")
        for metric, m in c["metrics"].items():
            if m["actual"] is None:
                print(f"  {metric:<14} MISSING           "
                      f"expected {fmt(m['expected'])}  ({m['basis']})")
                continue
            print(f"  {metric:<14} {fmt(m['actual']):>14}   "
                  f"expected {fmt(m['expected']):>14}   "
                  f"{'OK ' if m['passed'] else 'OFF'} {m['drift_pct']:5.1f}%   ({m['basis']})")
        if c["impossible"]:
            print(f"  IMPOSSIBLE: {c['impossible']}")
        ident = c["identity"]
        if ident["checkable"]:
            print(f"  A = L + E      {fmt(ident['assets'])} vs "
                  f"{fmt(ident['liabilities_plus_equity'])}   "
                  f"{'OK ' if ident['balanced'] else 'OFF'} {ident['drift_pct']:.2f}%")
        else:
            print(f"  A = L + E      not checkable ({ident['reason']})")
        for issue in c["data_quality_issues"]:
            print(f"  ISSUE: {issue}")

    print("\n" + "=" * 78)
    print(result["summary"])
    print("=" * 78)
    return result["passed"]


def coverage() -> None:
    from src.company.verify import concept_coverage

    cov = concept_coverage()
    total = cov["tickers_with_any_fundamentals"]
    print("\n" + "=" * 78)
    print(f"COVERAGE — {total:,} tickers have at least one fundamentals row")
    print("=" * 78)
    for concept, i in cov["by_concept"].items():
        print(f"  {concept:<26} {i['tickers_with_data']:>7,}  {i['coverage_pct']:5.1f}%")
    print(f"\n  renderable (assets AND equity): {cov['tickers_renderable']:,}")
    if cov["unmapped_metrics"]:
        print(f"  unmapped metrics in table: {', '.join(cov['unmapped_metrics'])}")


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
