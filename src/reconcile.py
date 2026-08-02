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


def detect_split_adjustment_detailed(
    close: pd.Series, splits: list[tuple[dt.date, float]], *, tol: float = 0.15
) -> tuple[str, list[dict[str, Any]]]:
    """Are `close` prices split-adjusted? Returns (verdict, per-split detail).

    Convention: `ratio` is new-shares-per-old -- >1 forward, <1 reverse. Across a
    split's ex-date an UNADJUSTED series moves by ~1/ratio (a 2:1 forward halves;
    a 1:10 reverse jumps ~10x); an ADJUSTED series is continuous (~1.0). The
    detail carries the actual numbers and the reasoning so a caller (or a human)
    can judge a borderline case -- reverse splits especially, whose large jumps
    are a known false-positive shape.
    """
    close = close.dropna().sort_index()
    detail: list[dict[str, Any]] = []
    if close.empty:
        return "inconclusive", detail

    for ex_date, ratio in splits:
        if not ratio or ratio <= 0 or abs(ratio - 1.0) < 1e-9:
            continue
        kind = "forward" if ratio > 1 else "reverse"
        exp_unadj = 1.0 / ratio
        d: dict[str, Any] = {
            "ex_date": ex_date.isoformat() if hasattr(ex_date, "isoformat") else str(ex_date),
            "ratio": round(ratio, 4), "kind": kind,
            "expected_if_unadjusted": round(exp_unadj, 4),
            "expected_if_adjusted": 1.0,
        }
        before = close[close.index < ex_date]
        after = close[close.index >= ex_date]
        if before.empty or after.empty:
            d.update(verdict="no_data",
                     reasoning="ex-date outside the loaded price window (no bars on one side)")
            detail.append(d)
            continue
        p_before = float(before.iloc[-1])
        p_after = float(after.iloc[0])
        if p_before <= 0:
            d.update(verdict="no_data", reasoning="non-positive price before the ex-date")
            detail.append(d)
            continue
        observed = p_after / p_before
        d.update(price_before=round(p_before, 4), price_after=round(p_after, 4),
                 observed_ratio=round(observed, 4))
        if abs(observed - exp_unadj) <= tol * exp_unadj:
            d["verdict"] = "unadjusted"
            d["reasoning"] = (
                f"{kind} split (ratio {ratio:.3g}): an unadjusted series moves "
                f"~{exp_unadj:.3g}x at the ex-date; observed {p_before:.4g}→{p_after:.4g} "
                f"= {observed:.3g}x → UNADJUSTED"
            )
        elif abs(observed - 1.0) <= tol:
            d["verdict"] = "adjusted"
            d["reasoning"] = (
                f"{kind} split (ratio {ratio:.3g}): observed {p_before:.4g}→{p_after:.4g} "
                f"= {observed:.3g}x ≈ 1.0 → ADJUSTED (history already scaled)"
            )
        else:
            d["verdict"] = "inconclusive"
            d["reasoning"] = (
                f"{kind} split (ratio {ratio:.3g}): observed {observed:.3g}x matches "
                f"neither {exp_unadj:.3g}x (unadjusted) nor 1.0x (adjusted) — a "
                f"confounding move near the ex-date"
            )
        detail.append(d)

    verdicts = [d["verdict"] for d in detail if d["verdict"] in ("adjusted", "unadjusted", "inconclusive")]
    if not verdicts:
        return "inconclusive", detail
    if "unadjusted" in verdicts:  # one real unadjusted split is enough to worry
        return "unadjusted", detail
    if all(v == "adjusted" for v in verdicts):
        return "adjusted", detail
    return "inconclusive", detail


def detect_split_adjustment(
    close: pd.Series, splits: list[tuple[dt.date, float]], *, tol: float = 0.15
) -> str:
    """Verdict only: "adjusted" | "unadjusted" | "inconclusive". Testable offline."""
    return detect_split_adjustment_detailed(close, splits, tol=tol)[0]


def _norm_polygon(ticker: str) -> str:
    """Polygon uses dots for class shares (BRK.B); Stooq/SEC use dashes."""
    return ticker.upper().replace(".", "-")


async def reconcile(sample: int = 25, as_of: dt.date | None = None) -> dict[str, Any]:
    """Assemble and print the reconciliation report from real loaded data."""
    from sqlalchemy import func, select

    from src.config.settings import get_settings
    from src.ingest import sec_edgar
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
        except Exception as exc:  # noqa: BLE001 - body carries the 403 reason
            report["coverage_vs_polygon"] = {"error": str(exc)[:300]}
    else:
        report["coverage_vs_polygon"] = "skipped (no POLYGON_API_KEY)"

    # Adjustment: test names that ACTUALLY split in the window (see helper).
    report["adjustment"] = await _adjustment_report(
        joined, as_of, sample, get_bars, s
    )

    log.info("stooq_reconciliation", **{k: v for k, v in report.items() if k != "adjustment"})
    return report


async def _adjustment_report(
    joined: set[str], as_of: dt.date, sample: int, get_bars, s
) -> dict[str, Any]:
    """Split-adjustment rate, measured on names that actually split.

    Random sampling comes back mostly "inconclusive" because most names have no
    split in the loaded window, so the detector can't say anything -- that dilutes
    the rate into meaninglessness. So we SAMPLE THE SPLITS: pull the market's
    split calendar from FMP (one bulk call), intersect with loaded tickers, and
    test up to `sample` of them. A small RANDOM control group is measured beside
    it so the contrast is visible. The split-targeted numbers are the ones whose
    denominator means something.
    """
    import random

    from src.ingest import fmp, yahoo
    from src.storage.db import session_scope

    window_start = as_of - dt.timedelta(days=365)
    empty_summary = {"adjusted": 0, "unadjusted": 0, "inconclusive": 0, "no_data": 0}
    out: dict[str, Any] = {
        "method": "split-targeted",
        "window_days": (as_of - window_start).days,
        "splits_source": None,
        "tested_with_known_splits": 0,
        "summary": dict(empty_summary),
        "verdicts": {},
        "details": {},
        "unadjusted_names": [],
        "control": {"tested": 0, "summary": dict(empty_summary), "verdicts": {}},
    }

    # 1) Discover names that split in the window. FMP is the bulk source; fall
    #    back to a bounded Yahoo scan if the FMP plan lacks the calendar.
    targeted: dict[str, list[tuple[dt.date, float]]] = {}
    if s.fmp_api_key:
        try:
            for r in await fmp.fetch_stock_splits(window_start, as_of):
                if r["ticker"] in joined:
                    targeted.setdefault(r["ticker"], []).append((r["date"], r["ratio"]))
            out["splits_source"] = "fmp"
        except Exception as exc:  # noqa: BLE001 - fall back, and say we did
            out["splits_source"] = "fmp_unavailable"
            out["splits_error"] = str(exc)[:200]

    if not targeted:
        out["splits_source"] = out["splits_source"] or "yahoo_scan"
        scanned = 0
        for t in sorted(joined):
            if len(targeted) >= sample or scanned >= sample * 40:
                break
            scanned += 1
            sp = _yahoo_splits(yahoo, t, window_start)
            if sp:
                targeted[t] = sp

    # 2) Run the detector on up to `sample` split names.
    chosen = sorted(targeted)[:sample]
    with session_scope() as session:
        for t in chosen:
            verdict, detail = _verdict_for(session, get_bars, as_of, t, targeted[t])
            out["verdicts"][t] = verdict
            if detail:
                out["details"][t] = detail
            out["summary"][verdict] = out["summary"].get(verdict, 0) + 1
        out["tested_with_known_splits"] = len(chosen)
        out["unadjusted_names"] = [t for t, v in out["verdicts"].items() if v == "unadjusted"]

        # 3) Random control group, for contrast (expected: mostly inconclusive).
        pool = sorted(joined - set(chosen))
        k = min(8, len(pool))
        if k:
            for t in random.Random(as_of.toordinal()).sample(pool, k):
                sp = _yahoo_splits(yahoo, t, window_start)
                verdict, _ = _verdict_for(session, get_bars, as_of, t, sp)
                out["control"]["verdicts"][t] = verdict
                out["control"]["summary"][verdict] = out["control"]["summary"].get(verdict, 0) + 1
            out["control"]["tested"] = k

    out["note"] = (
        f"{out['tested_with_known_splits']} names with a known split in the last "
        f"{out['window_days']}d tested (split-targeted). Random control "
        f"({out['control']['tested']}) is expected to be mostly inconclusive — random "
        f"names rarely split in-window — so read the split-targeted counts, not the "
        f"control, as the real rate. UNADJUSTED prices make every momentum factor wrong."
    )
    return out


def _yahoo_splits(yahoo, ticker: str, since: dt.date) -> list[tuple[dt.date, float]]:
    acts = yahoo.fetch_corporate_actions(ticker, since=since)
    return [
        (a["date"], float(a["split_ratio"]))
        for a in acts
        if a.get("split_ratio") and float(a["split_ratio"]) not in (0.0, 1.0)
    ]


def _verdict_for(session, get_bars, as_of, ticker, splits):
    """Run the detailed detector for one ticker against its loaded bars."""
    if not splits:
        return "inconclusive", []
    bars = get_bars(session, [ticker], as_of - dt.timedelta(days=500), as_of)
    if bars.empty:
        return "no_data", []
    close = bars.set_index("date")["close"]
    return detect_split_adjustment_detailed(close, splits)


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
