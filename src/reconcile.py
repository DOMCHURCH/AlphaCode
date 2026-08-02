"""Live reconciliation for the keyless Stooq bulk source.

Passing tests on a synthetic bundle proves the loader works, NOT that Stooq's
real data matches expectations. This runs on the deploy, against real loaded
bars, and prints the actual numbers -- never assumes:

  symbology  do Stooq tickers join against the SEC universe (and Polygon)?
  coverage   how many liquid names does Stooq actually carry?
  adjustment are prices split-adjusted? (unadjusted -> every momentum factor is
             wrong, so this is the one that can silently poison the funnel)
  recency    how stale is the file relative to as_of?

Run it after the first bulk backfill:  python -m src.reconcile
It reads stored bars + the SEC seed + Yahoo splits; if a POLYGON_API_KEY is set
it also diffs against Polygon's universe. Nothing is fabricated -- missing
inputs are reported as such.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
from typing import Any

import pandas as pd
import structlog

log = structlog.get_logger(__name__)


def detect_split_adjustment(
    close: pd.Series, splits: list[tuple[dt.date, float]], *, tol: float = 0.15
) -> str:
    """Pure heuristic: are `close` prices split-adjusted?

    For each known split (date, ratio>1 for a forward split), an UNADJUSTED
    series drops by ~1/ratio across the ex-date; an ADJUSTED series does not.
    Returns "adjusted" | "unadjusted" | "inconclusive". Testable offline.
    """
    close = close.dropna().sort_index()
    if close.empty:
        return "inconclusive"
    verdicts: list[str] = []
    for ex_date, ratio in splits:
        if not ratio or ratio <= 0 or abs(ratio - 1.0) < 1e-9:
            continue
        before = close[close.index < ex_date]
        after = close[close.index >= ex_date]
        if before.empty or after.empty:
            continue
        p_before = float(before.iloc[-1])
        p_after = float(after.iloc[0])
        if p_before <= 0:
            continue
        observed = p_after / p_before
        # Unadjusted: price falls by ~1/ratio at the split. Adjusted: ~unchanged.
        if abs(observed - 1.0 / ratio) <= tol * (1.0 / ratio):
            verdicts.append("unadjusted")
        elif abs(observed - 1.0) <= tol:
            verdicts.append("adjusted")
        else:
            verdicts.append("inconclusive")
    if not verdicts:
        return "inconclusive"
    if "unadjusted" in verdicts:  # one real unadjusted split is enough to worry
        return "unadjusted"
    if all(v == "adjusted" for v in verdicts):
        return "adjusted"
    return "inconclusive"


def _norm_polygon(ticker: str) -> str:
    """Polygon uses dots for class shares (BRK.B); Stooq/SEC use dashes."""
    return ticker.upper().replace(".", "-")


async def reconcile(sample: int = 25, as_of: dt.date | None = None) -> dict[str, Any]:
    """Assemble and print the reconciliation report from real loaded data."""
    from sqlalchemy import func, select

    from src.config.settings import get_settings
    from src.ingest import sec_edgar, yahoo
    from src.storage.db import session_scope
    from src.storage.models import DailyBar
    from src.storage.pit import get_bars

    s = get_settings()
    as_of = as_of or dt.date.today()
    report: dict[str, Any] = {}

    with session_scope() as session:
        stooq_tickers = set(
            session.execute(select(DailyBar.ticker).distinct()).scalars().all()
        )
        latest = session.execute(select(func.max(DailyBar.date))).scalar_one_or_none()

    report["recency"] = {
        "latest_bar_date": latest.isoformat() if latest else None,
        "as_of": as_of.isoformat(),
        "staleness_days": (as_of - latest).days if latest else None,
    }

    # Symbology + coverage vs the SEC universe seed.
    try:
        sec_rows = await sec_edgar.fetch_company_tickers()
        sec_tickers = {str(r["ticker"]).upper() for r in sec_rows}
    except Exception as exc:  # noqa: BLE001
        sec_tickers = set()
        report["sec_error"] = str(exc)[:200]
    joined = stooq_tickers & sec_tickers
    report["symbology_sec"] = {
        "stooq_tickers": len(stooq_tickers),
        "sec_tickers": len(sec_tickers),
        "joined": len(joined),
        "join_rate_vs_sec": round(len(joined) / len(sec_tickers), 3) if sec_tickers else None,
        "sample_stooq_only": sorted(stooq_tickers - sec_tickers)[:10],
    }

    # Coverage vs Polygon's universe, if a key is present (the diff you asked for).
    if s.polygon_api_key:
        try:
            from src.ingest import polygon

            grouped = await polygon.fetch_grouped_daily(as_of)
            poly = {_norm_polygon(str(b["ticker"])) for b in grouped}
            report["coverage_vs_polygon"] = {
                "polygon_tickers": len(poly),
                "in_both": len(poly & stooq_tickers),
                "polygon_not_in_stooq": len(poly - stooq_tickers),
                "coverage_rate": round(len(poly & stooq_tickers) / len(poly), 3) if poly else None,
                "sample_missing_from_stooq": sorted(poly - stooq_tickers)[:15],
            }
        except Exception as exc:  # noqa: BLE001
            report["coverage_vs_polygon"] = {"error": str(exc)[:200]}
    else:
        report["coverage_vs_polygon"] = "skipped (no POLYGON_API_KEY)"

    # Adjustment: sample names with a known split in the loaded window.
    adj: dict[str, str] = {}
    checked = 0
    with session_scope() as session:
        for tkr in sorted(joined):
            if checked >= sample:
                break
            actions = yahoo.fetch_corporate_actions(tkr, since=as_of - dt.timedelta(days=730))
            splits = [
                (a["date"], float(a["split_ratio"]))
                for a in actions
                if a.get("split_ratio") and float(a["split_ratio"]) not in (0.0, 1.0)
            ]
            if not splits:
                continue
            bars = get_bars(session, [tkr], as_of - dt.timedelta(days=730), as_of)
            if bars.empty:
                continue
            close = bars.set_index("date")["close"]
            adj[tkr] = detect_split_adjustment(close, splits)
            checked += 1
    report["adjustment"] = {
        "checked": checked,
        "verdicts": adj,
        "summary": {v: sum(1 for x in adj.values() if x == v)
                    for v in ("adjusted", "unadjusted", "inconclusive")},
        "note": (
            "UNADJUSTED prices make every momentum factor wrong. If any names read "
            "unadjusted, do not trust the funnel until the source is fixed."
        ),
    }

    log.info("stooq_reconciliation", **{k: v for k, v in report.items() if k != "adjustment"})
    return report


def _print(report: dict[str, Any]) -> None:
    import json

    print(json.dumps(report, indent=2, default=str))


def main() -> None:
    from src.logging_config import configure_logging

    parser = argparse.ArgumentParser(description="Reconcile the Stooq bulk source")
    parser.add_argument("--sample", type=int, default=25)
    args = parser.parse_args()
    configure_logging()
    _print(asyncio.run(reconcile(sample=args.sample)))


if __name__ == "__main__":
    main()
