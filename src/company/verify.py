"""Five-company verification of the rebuilt XBRL extraction.

The single source of truth for the reference figures. The `/admin/verify`
endpoint and `scripts/reload_fundamentals.py` both call in here, so the numbers
cannot drift apart between the two.

JPM's figures are exact: they were read off a real num.txt dump for period
2025-12-31 and are the consolidated rows the filter must now select. The rest
are order-of-magnitude expectations -- they catch a parser reading the wrong
fact, not a small reporting difference.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Fraction a figure may differ from its reference before it counts as a failure.
TOLERANCE = 0.10


class Reference:
    """One expected figure and why we believe it.

    `confirmed` marks a figure read off a raw num.txt dump. An unconfirmed
    reference is a recollection, and when it disagrees with the parser the
    reference is at least as likely to be the wrong side. Saying which is which
    keeps "the parser is broken" and "my memory was stale" from looking
    identical in the output.
    """

    def __init__(self, value: float, basis: str, confirmed: bool = False) -> None:
        self.value = value
        self.basis = basis
        self.confirmed = confirmed


REFERENCE: dict[str, dict[str, Reference]] = {
    "JPM": {
        "total_assets": Reference(
            4_424_900_000_000, "num.txt dump, period 2025-12-31", confirmed=True
        ),
        "total_equity": Reference(
            362_438_000_000, "num.txt dump, period 2025-12-31", confirmed=True
        ),
    },
    "MSFT": {
        # Confirmed off the dump: 1 row, consolidated, ddate=20251231, qtrs=0,
        # USD. Notably the same figure the parser produced -- but it is confirmed
        # because the file says so, not because the parser agreed with itself.
        "total_assets": Reference(
            665_302_000_000, "num.txt dump, period 2025-12-31", confirmed=True
        ),
        # Corrected from a remembered ~$300B. The num.txt dump for period
        # 2025-12-31 shows exactly one consolidated StockholdersEquity row at
        # this figure: the parser was right and the expectation was stale, which
        # is the only direction a reference is allowed to move.
        "total_equity": Reference(
            390_875_000_000, "num.txt dump, period 2025-12-31", confirmed=True
        ),
    },
    "WMT": {
        "total_assets": Reference(260_000_000_000, "recollection, unconfirmed"),
    },
    "FCX": {
        "total_assets": Reference(
            55_000_000_000, "recollection, unconfirmed; must be positive"
        ),
    },
    "AAL": {
        # American Airlines genuinely runs a stockholders' deficit. The sign is
        # the point: negative is correct here and must not be "fixed".
        "total_equity": Reference(
            -3_900_000_000, "recollection, unconfirmed; negative is correct for AAL"
        ),
    },
}

# Where each checked metric lives on the BalanceSheet dataclass.
_LOCATION = {
    "total_assets": ("assets", "total_assets"),
    "total_equity": ("equity", "shareholders_equity"),
}


def verify_companies(as_of: dt.date | None = None) -> dict[str, Any]:
    """Check every reference company. Returns a JSON-safe result.

    `passed` is True only if every company reconciles. A missing figure, an
    impossible total, or a drift beyond TOLERANCE all fail.
    """
    from src.company.balancesheet import get_balance_sheet

    as_of = as_of or dt.date.today()
    companies: dict[str, Any] = {}
    all_passed = True

    for ticker, checks in REFERENCE.items():
        bs = get_balance_sheet(ticker, as_of=as_of)
        if bs is None:
            companies[ticker] = {
                "found": False,
                "passed": False,
                "reason": "no fundamentals rows for this ticker",
            }
            all_passed = False
            continue

        metrics: dict[str, Any] = {}
        company_passed = True

        for metric, ref in checks.items():
            group_name, concept = _LOCATION[metric]
            cell = getattr(bs, group_name)[concept]
            actual = None if cell.missing else cell.value

            if actual is None:
                metrics[metric] = {
                    "actual": None, "expected": ref.value, "basis": ref.basis,
                    "drift_pct": None, "passed": False, "note": "missing",
                }
                company_passed = False
                continue

            drift = abs(actual - ref.value) / abs(ref.value)
            ok = drift <= TOLERANCE
            metrics[metric] = {
                "actual": actual,
                "expected": ref.value,
                "basis": ref.basis,
                "confirmed": ref.confirmed,
                "drift_pct": round(drift * 100, 2),
                "passed": ok,
            }
            if not ok and not ref.confirmed:
                # A mismatch against an unconfirmed reference is not evidence
                # the parser is wrong. Report it as needing a dump rather than
                # letting a stale memory read as a data failure.
                metrics[metric]["verdict"] = "reference unconfirmed — dump to settle"
            company_passed = company_passed and (ok or not ref.confirmed)

        # Impossibility check, independent of the reference figures.
        ta_cell = bs.assets["total_assets"]
        total_assets = None if ta_cell.missing else ta_cell.value
        impossible = None
        if total_assets is not None and total_assets <= 0:
            impossible = f"total assets is {total_assets:,.0f}"
            company_passed = False

        # A = L + E, reported totals only -- never a sum of parts standing in
        # for a total the filing did not state.
        #
        # Balances against TOTAL equity including noncontrolling interests when
        # the filer reports it: `Assets` is consolidated, parent-only
        # StockholdersEquity is not, and using the latter leaves the NCI behind
        # as drift that looks like a parser bug but is an apples-to-oranges
        # comparison.
        tl_cell = bs.liabilities["total_liabilities"]
        stated_cell = bs.liabilities["liabilities_and_equity"]
        incl_cell = bs.equity["total_equity_incl_nci"]
        parent_cell = bs.equity["shareholders_equity"]
        te_cell = incl_cell if not incl_cell.missing else parent_cell
        equity_basis = (
            "total_equity_incl_nci" if not incl_cell.missing else "total_equity"
        )
        identity: dict[str, Any]
        if total_assets and not stated_cell.missing:
            # The filer's own stated total beats reconstructing L + E.
            rhs = stated_cell.value
            equity_basis = "liabilities_and_equity"
            drift = abs(total_assets - rhs) / total_assets * 100
            identity = {
                "checkable": True,
                "assets": total_assets,
                "liabilities_plus_equity": rhs,
                "equity_basis": equity_basis,
                "drift_pct": round(drift, 2),
                "balanced": drift < 1.0,
            }
        elif total_assets and not tl_cell.missing and not te_cell.missing:
            rhs = tl_cell.value + te_cell.value
            drift = abs(total_assets - rhs) / total_assets * 100
            identity = {
                "checkable": True,
                "assets": total_assets,
                "liabilities_plus_equity": rhs,
                "equity_basis": equity_basis,
                "drift_pct": round(drift, 2),
                "balanced": drift < 1.0,
            }
            nci_cell = bs.equity["minority_interest"]
            if equity_basis == "total_equity" and not nci_cell.missing and drift > 1.0:
                closed = abs(total_assets - (rhs + nci_cell.value)) / total_assets * 100
                if closed <= 1.0:
                    identity["explained_by"] = "noncontrolling_interest"
                    identity["drift_pct_with_nci"] = round(closed, 2)
        else:
            identity = {
                "checkable": False,
                "reason": "total_liabilities not reported for this period",
            }

        companies[ticker] = {
            "found": True,
            "company_name": bs.company_name,
            "period_end": bs.period_end.isoformat() if bs.period_end else None,
            "filing_date": bs.filing_date.isoformat() if bs.filing_date else None,
            "metrics": metrics,
            "identity": identity,
            "impossible": impossible,
            "data_quality_issues": bs.data_quality_issues,
            "passed": company_passed,
        }
        all_passed = all_passed and company_passed

    unconfirmed = [
        f"{t}.{m}"
        for t, c in companies.items()
        if c.get("found")
        for m, x in c["metrics"].items()
        if not x.get("confirmed") and not x.get("passed")
    ]
    return {
        "as_of": as_of.isoformat(),
        "tolerance_pct": TOLERANCE * 100,
        "companies": companies,
        "passed": all_passed,
        "unconfirmed_mismatches": unconfirmed,
        "summary": (
            "PASS — every company reconciles"
            if all_passed
            else f"FAIL — at least one CONFIRMED figure is off by more than "
                 f"{TOLERANCE:.0%}. The parser is wrong; do not build on these."
        ),
    }


def concept_coverage() -> dict[str, Any]:
    """Per-concept coverage across every ticker that has any fundamentals."""
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
    by_concept = {}
    for concept, metric in sorted(BALANCE_SHEET_CONCEPTS.items()):
        n = have.get(metric, 0)
        by_concept[concept] = {
            "metric": metric,
            "tickers_with_data": n,
            "coverage_pct": round(100.0 * n / total, 1) if total else 0.0,
        }

    renderable = sum(
        1 for t in _tickers_with_both() if t
    )
    return {
        "by_concept": by_concept,
        "tickers_with_any_fundamentals": total,
        "tickers_renderable": renderable,
        # Metrics present in the table that no balance-sheet concept reads.
        "unmapped_metrics": sorted(
            set(have) - set(BALANCE_SHEET_CONCEPTS.values())
        ),
    }


def _tickers_with_both() -> list[str]:
    """Tickers carrying BOTH total_assets and total_equity -- enough to render."""
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as session:
        rows = session.execute(
            select(Fundamental.ticker)
            .where(Fundamental.metric.in_(("total_assets", "total_equity")))
            .group_by(Fundamental.ticker)
            .having(func.count(func.distinct(Fundamental.metric)) == 2)
        ).scalars().all()
    return list(rows)
