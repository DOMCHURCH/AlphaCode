"""Whole-universe validation of the extraction.

The five-company check catches a parser reading the wrong fact. It cannot catch
whether the parse is right across the file: five names out of ~5,300 is an
anecdote. This pass runs the accounting identity over every ticker with enough
data to check, buckets the drift, names the worst offenders, and breaks the
result down by sector -- because a failure concentrated in banks or REITs is a
structural problem with those filers' tags, not noise.

It also looks across periods, which no per-period check can: a total that moves
more than 10x between consecutive quarters is a units or scale error, and every
individual quarter of it can look perfectly self-consistent.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Identity drift buckets, in percent. Ordered; the last is open-ended.
DRIFT_BUCKETS = (
    ("within_1pct", 0.0, 1.0),
    ("1_to_5pct", 1.0, 5.0),
    ("5_to_10pct", 5.0, 10.0),
    ("over_10pct", 10.0, float("inf")),
)

# A period-on-period move beyond this is a scale error, not growth.
SCALE_JUMP_FACTOR = 10.0

WORST_N = 50


def _bucket(drift_pct: float) -> str:
    for name, lo, hi in DRIFT_BUCKETS:
        if lo <= drift_pct < hi:
            return name
    return DRIFT_BUCKETS[-1][0]


def run_universe_check(as_of: dt.date | None = None) -> dict[str, Any]:
    """Run the identity and scale checks over every ticker in the table."""
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap

    as_of = as_of or dt.date.today()

    wanted = (
        "total_assets",
        "total_liabilities",
        "total_equity",
        "total_equity_incl_nci",
        "minority_interest",
    )

    with session_scope() as session:
        rows = session.execute(
            select(
                Fundamental.ticker,
                Fundamental.metric,
                Fundamental.value,
                Fundamental.period_end,
                Fundamental.filing_date,
            )
            .where(Fundamental.metric.in_(wanted))
            .where(Fundamental.period_end <= as_of)
        ).all()
        sectors = dict(
            session.execute(
                select(SectorMap.ticker, SectorMap.sector)
            ).all()
        )

    # ticker -> period_end -> metric -> value. Earliest filing wins within a
    # period, matching how the loader stores as-first-reported.
    by_ticker: dict[str, dict[dt.date, dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    seen_filing: dict[tuple[str, dt.date, str], dt.date] = {}
    for ticker, metric, value, period_end, filing_date in rows:
        if value is None:
            continue
        key = (ticker, period_end, metric)
        prev = seen_filing.get(key)
        if prev is not None and filing_date >= prev:
            continue
        seen_filing[key] = filing_date
        by_ticker[ticker][period_end][metric] = float(value)

    buckets = {name: 0 for name, _lo, _hi in DRIFT_BUCKETS}
    by_sector: dict[str, dict[str, int]] = defaultdict(
        lambda: {**{n: 0 for n, _l, _h in DRIFT_BUCKETS}, "checkable": 0,
                 "not_checkable": 0}
    )
    worst: list[dict[str, Any]] = []
    scale_jumps: list[dict[str, Any]] = []
    checkable = 0
    not_checkable = 0
    nci_explained = 0
    used_nci_basis = 0

    for ticker, periods in by_ticker.items():
        sector = sectors.get(ticker) or "unknown"

        # --- identity, on the most recent period we have ---
        latest = max(periods)
        m = periods[latest]
        assets = m.get("total_assets")
        liabilities = m.get("total_liabilities")
        equity_incl = m.get("total_equity_incl_nci")
        equity_parent = m.get("total_equity")
        equity = equity_incl if equity_incl is not None else equity_parent
        basis = "total_equity_incl_nci" if equity_incl is not None else "total_equity"

        if not assets or assets <= 0 or liabilities is None or equity is None:
            not_checkable += 1
            by_sector[sector]["not_checkable"] += 1
        else:
            checkable += 1
            by_sector[sector]["checkable"] += 1
            if basis == "total_equity_incl_nci":
                used_nci_basis += 1
            rhs = liabilities + equity
            drift_pct = abs(assets - rhs) / assets * 100.0
            bucket = _bucket(drift_pct)
            buckets[bucket] += 1
            by_sector[sector][bucket] += 1

            entry: dict[str, Any] = {
                "ticker": ticker,
                "sector": sector,
                "period_end": latest.isoformat(),
                "total_assets": assets,
                "total_liabilities": liabilities,
                "equity": equity,
                "equity_basis": basis,
                "liabilities_plus_equity": rhs,
                "drift_pct": round(drift_pct, 2),
            }
            # Would the NCI close it? If so this is the known mapping gap, not
            # a mystery -- and saying so stops it being re-investigated.
            nci = m.get("minority_interest")
            if basis == "total_equity" and nci is not None and drift_pct > 1.0:
                closed = abs(assets - (rhs + nci)) / assets * 100.0
                if closed <= 1.0:
                    entry["explained_by"] = "noncontrolling_interest"
                    entry["drift_pct_with_nci"] = round(closed, 2)
                    nci_explained += 1
            if drift_pct > 1.0:
                worst.append(entry)

        # --- scale jumps, across consecutive periods ---
        ordered = sorted(periods)
        for prev_p, cur_p in zip(ordered, ordered[1:]):
            a_prev = periods[prev_p].get("total_assets")
            a_cur = periods[cur_p].get("total_assets")
            if not a_prev or not a_cur or a_prev <= 0:
                continue
            ratio = a_cur / a_prev
            if ratio > SCALE_JUMP_FACTOR or ratio < 1.0 / SCALE_JUMP_FACTOR:
                scale_jumps.append({
                    "ticker": ticker,
                    "sector": sector,
                    "from_period": prev_p.isoformat(),
                    "to_period": cur_p.isoformat(),
                    "from_assets": a_prev,
                    "to_assets": a_cur,
                    "ratio": round(ratio, 2),
                })

    worst.sort(key=lambda e: -e["drift_pct"])
    scale_jumps.sort(key=lambda e: -abs(e["ratio"]))

    total_checked = checkable + not_checkable
    pass_rate = buckets["within_1pct"] / checkable if checkable else 0.0

    # Sector verdicts: a sector failing far worse than the universe is a
    # structural tag problem for that filer class, not scattered noise.
    sector_rows = []
    for sector, counts in by_sector.items():
        c = counts["checkable"]
        if not c:
            sector_rows.append({
                "sector": sector, "checkable": 0, "not_checkable": counts["not_checkable"],
                "pass_rate_pct": None, "verdict": "no checkable tickers",
            })
            continue
        rate = counts["within_1pct"] / c
        verdict = "ok"
        if rate < 0.5:
            verdict = "STRUCTURAL — most of this sector fails the identity"
        elif rate < max(0.8, pass_rate - 0.15):
            verdict = "worse than the universe — check this sector's equity tag"
        sector_rows.append({
            "sector": sector,
            "checkable": c,
            "not_checkable": counts["not_checkable"],
            "within_1pct": counts["within_1pct"],
            "1_to_5pct": counts["1_to_5pct"],
            "5_to_10pct": counts["5_to_10pct"],
            "over_10pct": counts["over_10pct"],
            "pass_rate_pct": round(rate * 100, 1),
            "verdict": verdict,
        })
    sector_rows.sort(key=lambda r: (r["pass_rate_pct"] is None, r["pass_rate_pct"] or 0))

    result = {
        "as_of": as_of.isoformat(),
        "tickers_in_table": len(by_ticker),
        "identity": {
            "checkable": checkable,
            "not_checkable": not_checkable,
            "total": total_checked,
            "buckets": buckets,
            "pass_rate_pct": round(pass_rate * 100, 1),
            "equity_basis_incl_nci": used_nci_basis,
            "explained_by_nci": nci_explained,
        },
        "worst": worst[:WORST_N],
        "worst_total": len(worst),
        "scale_jumps": scale_jumps[:WORST_N],
        "scale_jumps_total": len(scale_jumps),
        "by_sector": sector_rows,
    }
    log.info(
        "universe_check_done", tickers=len(by_ticker), checkable=checkable,
        pass_rate_pct=result["identity"]["pass_rate_pct"],
        over_10pct=buckets["over_10pct"], scale_jumps=len(scale_jumps),
        explained_by_nci=nci_explained,
    )
    return result
