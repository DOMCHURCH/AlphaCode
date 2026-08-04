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

# Total-assets size bands for the failures. A shell company with $17k of assets
# failing by 70,000% is arithmetic on noise, not evidence about the parser --
# and lumping it in with a real large-cap failure hides the one that matters.
SIZE_BUCKETS = (
    ("under_1m", 0.0, 1e6),
    ("1m_to_10m", 1e6, 1e7),
    ("10m_to_100m", 1e7, 1e8),
    ("over_100m", 1e8, float("inf")),
)

MICROCAP_CEILING = 1e7  # $10M


def _bucket(drift_pct: float) -> str:
    for name, lo, hi in DRIFT_BUCKETS:
        if lo <= drift_pct < hi:
            return name
    return DRIFT_BUCKETS[-1][0]


def _size_bucket(assets: float) -> str:
    for name, lo, hi in SIZE_BUCKETS:
        if lo <= assets < hi:
            return name
    return SIZE_BUCKETS[-1][0]


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
        "liabilities_and_equity",
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
    # Same buckets under the reconstructed L + E, over the filers that report
    # both, so the stated total's value is measured rather than claimed.
    naive_buckets = {name: 0 for name, _lo, _hi in DRIFT_BUCKETS}
    basis_counts: dict[str, int] = defaultdict(int)
    size_buckets = {name: 0 for name, _lo, _hi in SIZE_BUCKETS}
    closed_by_stated = 0
    comparable = 0
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
        stated = m.get("liabilities_and_equity")

        # Prefer the filer's own stated total. Reconstructing L + E is only as
        # good as our choice of which equity tag to add; the stated figure has
        # none of that ambiguity because it is the total as filed.
        if stated is not None:
            rhs: float | None = stated
            basis = "liabilities_and_equity"
        elif liabilities is not None and equity is not None:
            rhs = liabilities + equity
            basis = (
                "total_equity_incl_nci" if equity_incl is not None else "total_equity"
            )
        else:
            rhs = None
            basis = "none"

        if not assets or assets <= 0 or rhs is None:
            not_checkable += 1
            by_sector[sector]["not_checkable"] += 1
        else:
            checkable += 1
            by_sector[sector]["checkable"] += 1
            basis_counts[basis] += 1
            if basis == "total_equity_incl_nci":
                used_nci_basis += 1
            drift_pct = abs(assets - rhs) / assets * 100.0
            bucket = _bucket(drift_pct)
            buckets[bucket] += 1
            by_sector[sector][bucket] += 1

            # What the stated total bought us: where a filer reports both, how
            # would the reconstructed L + E have scored? That difference is the
            # measured value of the mapping, not an assertion about it.
            if stated is not None and liabilities is not None and equity is not None:
                naive = liabilities + equity
                naive_pct = abs(assets - naive) / assets * 100.0
                naive_buckets[_bucket(naive_pct)] += 1
                if naive_pct > 1.0 >= drift_pct:
                    closed_by_stated += 1
                comparable += 1

            if drift_pct > 10.0:
                size_buckets[_size_bucket(assets)] += 1

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
                "size_band": _size_bucket(assets),
                "microcap": assets < MICROCAP_CEILING,
            }
            # Would the NCI close it? If so this is the known mapping gap, not
            # a mystery -- and saying so stops it being re-investigated.
            nci = m.get("minority_interest")
            if basis == "total_equity" and nci is not None and drift_pct > 1.0:  # noqa: SIM102
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
            "basis_counts": dict(basis_counts),
        },
        # The measured value of mapping LiabilitiesAndStockholdersEquity: over
        # filers reporting both, how the reconstructed L + E would have scored.
        "stated_total": {
            "used": basis_counts.get("liabilities_and_equity", 0),
            "comparable": comparable,
            "moved_into_1pct": closed_by_stated,
            "buckets_if_reconstructed": naive_buckets,
        },
        # Size profile of the >10% failures. If they are overwhelmingly tiny,
        # the bucket is arithmetic on noise rather than a parser problem.
        "over_10pct_by_size": {
            "buckets": size_buckets,
            "under_10m": size_buckets["under_1m"] + size_buckets["1m_to_10m"],
            "under_10m_pct": round(
                100.0
                * (size_buckets["under_1m"] + size_buckets["1m_to_10m"])
                / max(1, buckets["over_10pct"]),
                1,
            ),
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
        stated_total_used=basis_counts.get("liabilities_and_equity", 0),
        over_10pct_under_10m_pct=result["over_10pct_by_size"]["under_10m_pct"],
    )
    return result
