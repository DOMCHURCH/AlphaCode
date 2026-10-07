"""Replay the extractor's per-filing tag choice on SEC companyfacts and count how many
apparent restatements are really a switch between XBRL tags.

Read-only research script: it fetches data.sec.gov companyfacts for the largest filers,
applies the same alias-rank rule as `src.ingest.xbrl.extract_facts` (lowest-ranked tag
present in a filing wins), then compares each filing's value with the previous filing's
for the same (metric, period), exactly as `src.company.exceptions` does. A change is
"cross-tag" when the two filings supplied the value from different tags.

    python scripts/audit_restatement_tags.py [N_COMPANIES] [OUT.json]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date

from src.company.exceptions import TRACKED
from src.ingest.xbrl import CONCEPTS, INSTANT

UA = "BalanceProof research contact@balanceproof.dev"
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT", "20-F", "40-F"}


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def qtrs(start: str | None, end: str) -> int | None:
    if not start:
        return 0
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if 80 <= days <= 100:
        return 1
    if 350 <= days <= 380:
        return 4
    return None


def audit(cik: int) -> list[dict]:
    d = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    gaap = d.get("facts", {}).get("us-gaap", {})
    out = []
    for c in CONCEPTS:
        if c.metric not in TRACKED or len(c.tags) < 2:
            continue
        # (period_end, qtrs) -> accn -> (rank, tag, value, filed)
        per: dict = defaultdict(dict)
        for rank, tag in enumerate(c.tags):
            for f in gaap.get(tag, {}).get("units", {}).get(c.uom, []):
                if f.get("form") not in FORMS:
                    continue
                q = qtrs(f.get("start"), f["end"])
                if q is None or (c.kind == INSTANT) != (q == 0):
                    continue
                cur = per[(f["end"], q)].get(f["accn"])
                if cur is None or rank < cur[0]:
                    per[(f["end"], q)][f["accn"]] = (rank, tag, f["val"], f["filed"])
        for (end, _q), by_accn in per.items():
            seq = sorted(by_accn.values(), key=lambda t: t[3])
            for prev, cur in zip(seq, seq[1:], strict=False):
                if cur[2] == prev[2] or not prev[2]:
                    continue
                out.append({
                    "cik": cik, "company": d["entityName"], "metric": c.metric,
                    "period_end": end, "prev_tag": prev[1], "tag": cur[1],
                    "prev": prev[2], "now": cur[2], "prev_filed": prev[3], "filed": cur[3],
                    "pct": (cur[2] - prev[2]) / abs(prev[2]) * 100,
                    "cross_tag": prev[1] != cur[1],
                })
    return out


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    dest = sys.argv[2] if len(sys.argv) > 2 else "audit_restatement_tags.json"
    tickers = get("https://www.sec.gov/files/company_tickers.json")
    ciks = [v["cik_str"] for _, v in sorted(tickers.items(), key=lambda kv: int(kv[0]))][:n]
    rows: list[dict] = []
    for cik in ciks:
        try:
            rows += audit(cik)
        except Exception as exc:  # noqa: BLE001 - a missing filer must not stop the sweep
            print("skip", cik, exc, file=sys.stderr)
        time.sleep(0.2)
    json.dump(rows, open(dest, "w"))
    for metric in sorted({r["metric"] for r in rows}):
        m = [r for r in rows if r["metric"] == metric]
        big = [r for r in m if abs(r["pct"]) >= 1]
        cross = [r for r in big if r["cross_tag"]]
        print(f"{metric}: {len(m)} changed pairs, {len(big)} at >=1%, "
              f"{len(cross)} of those are cross-tag ({len(cross) / max(1, len(big)):.0%})")


if __name__ == "__main__":
    main()
