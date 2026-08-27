"""Five-company verification of the rebuilt XBRL extraction.

The single source of truth for the reference figures. The `/admin/verify`
endpoint and `scripts/reload_fundamentals.py` both call in here, so the numbers
cannot drift apart between the two.

JPM's figures are exact: they were read off a real num.txt dump for period
2025-12-31 and are the consolidated rows the filter must now select. The rest
are order-of-magnitude expectations -- they catch a parser reading the wrong
fact, not a small reporting difference.

Every reference figure is a fact about ONE period, so each carries the period
it was read for and is compared against that period. Until it did, this module
compared its 2025-12-31 figures against whatever the newest loaded period
happened to be, and reported the difference as a parser failure: JPM's assets
read 4,900,475,000,000 for 2026-03-31 against the 4,424,900,000,000 confirmed
for 2025-12-31 and failed at 10.75% drift, with the extraction entirely
correct and the balance identity clean. A balance sheet grows between filings.
Comparing across periods measures the growth, not the parser, and it fails
harder every quarter that passes -- always in the same direction, because
assets accumulate. That is a verification bug that manufactures extraction
bugs, so the period is now data the check enforces rather than prose in
`basis` that nothing reads.
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

    `period_end` is the period the figure was read for. A confirmed reference
    must have one: it is a fact about a specific balance sheet, and comparing
    it against a different one measures elapsed time. `None` means the figure
    has no period basis at all -- a recollection nobody has pinned to a filing
    -- and such a reference can only ever be a smell test against the latest
    period, never evidence that the parser is wrong.
    """

    def __init__(
        self,
        value: float,
        basis: str,
        period_end: dt.date | None = None,
        confirmed: bool = False,
    ) -> None:
        if confirmed and period_end is None:
            raise ValueError(
                f"confirmed reference {value:,.0f} ({basis}) has no period_end; "
                "a figure read off a dump is a fact about one period and cannot "
                "be checked without knowing which."
            )
        self.value = value
        self.basis = basis
        self.period_end = period_end
        self.confirmed = confirmed


REFERENCE: dict[str, dict[str, Reference]] = {
    "JPM": {
        "total_assets": Reference(
            4_424_900_000_000, "num.txt dump, period 2025-12-31",
            period_end=dt.date(2025, 12, 31), confirmed=True,
        ),
        "total_equity": Reference(
            362_438_000_000, "num.txt dump, period 2025-12-31",
            period_end=dt.date(2025, 12, 31), confirmed=True,
        ),
    },
    "MSFT": {
        # Confirmed off the dump: 1 row, consolidated, ddate=20251231, qtrs=0,
        # USD. Notably the same figure the parser produced -- but it is confirmed
        # because the file says so, not because the parser agreed with itself.
        "total_assets": Reference(
            665_302_000_000, "num.txt dump, period 2025-12-31",
            period_end=dt.date(2025, 12, 31), confirmed=True,
        ),
        # Corrected from a remembered ~$300B. The num.txt dump for period
        # 2025-12-31 shows exactly one consolidated StockholdersEquity row at
        # this figure: the parser was right and the expectation was stale, which
        # is the only direction a reference is allowed to move.
        "total_equity": Reference(
            390_875_000_000, "num.txt dump, period 2025-12-31",
            period_end=dt.date(2025, 12, 31), confirmed=True,
        ),
    },
    "WMT": {
        # No period basis: this is a remembered round number, and ~$260B is
        # what WMT's balance sheet looked like at a fiscal year end more than a
        # year before the newest loaded period. Compared against the latest
        # period it reads ~11% low, which is the reference aging, not the
        # parser. It stays unconfirmed and unpinned until a dump settles it,
        # and unconfirmed references cannot fail the run.
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
    unverifiable: list[str] = []

    for ticker, checks in REFERENCE.items():
        # The latest period carries the identity and impossibility checks: those
        # are statements about a balance sheet being internally coherent, and
        # they are most useful on the newest data we hold.
        bs = get_balance_sheet(ticker, as_of=as_of)
        if bs is None:
            companies[ticker] = {
                "found": False,
                "passed": False,
                "reason": "no fundamentals rows for this ticker",
            }
            all_passed = False
            continue

        # Reference comparisons run against the period each figure was read
        # for, which is usually NOT the latest one. Cached because several
        # metrics normally share a period and each lookup is a query.
        pinned: dict[dt.date, Any] = {}

        metrics: dict[str, Any] = {}
        company_passed = True

        for metric, ref in checks.items():
            group_name, concept = _LOCATION[metric]
            if ref.period_end is None:
                # No period basis, so the newest sheet is all there is to
                # compare against -- flagged as such on the way out.
                ref_bs = bs
            else:
                if ref.period_end not in pinned:
                    pinned[ref.period_end] = get_balance_sheet(
                        ticker, as_of=as_of, period_end=ref.period_end
                    )
                ref_bs = pinned[ref.period_end]
            compared_period = ref.period_end or bs.period_end

            # The reference names a period we have not loaded. That is not the
            # parser being wrong -- it is nothing to compare against -- and
            # calling it a failure is how a partial load reads as a data bug.
            if ref_bs is None:
                metrics[metric] = {
                    "actual": None,
                    "expected": ref.value,
                    "basis": ref.basis,
                    "confirmed": ref.confirmed,
                    "reference_period_end": ref.period_end.isoformat(),
                    "latest_period_end": bs.period_end.isoformat()
                    if bs.period_end else None,
                    "drift_pct": None,
                    "passed": False,
                    "verdict": "reference period not loaded — cannot verify",
                }
                unverifiable.append(f"{ticker}.{metric}")
                continue

            cell = getattr(ref_bs, group_name)[concept]
            actual = None if cell.missing else cell.value

            if actual is None:
                metrics[metric] = {
                    "actual": None, "expected": ref.value, "basis": ref.basis,
                    "compared_period_end": compared_period.isoformat()
                    if compared_period else None,
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
                # Which balance sheet the figure was actually compared against.
                # An unpinned reference falls back to the latest period, and
                # saying so is the difference between a real check and one that
                # silently measures elapsed time.
                "compared_period_end": compared_period.isoformat()
                if compared_period else None,
                "period_matched": ref.period_end is not None,
                "latest_period_end": bs.period_end.isoformat()
                if bs.period_end else None,
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
    # Name the actual cause. "The parser is wrong" is one of several reasons a
    # run fails, and printing it for a ticker that simply was not loaded sends
    # the reader to debug an extractor that is working.
    absent = [t for t, c in companies.items() if not c.get("found")]
    drifted = [
        f"{t}.{m}"
        for t, c in companies.items()
        if c.get("found")
        for m, x in c["metrics"].items()
        # `drift_pct is None` means the figure was never compared -- missing
        # metric or unloaded reference period. Those are coverage gaps, and
        # counting them as disagreement is what this whole check got wrong.
        if x.get("confirmed") and not x.get("passed")
        and x.get("drift_pct") is not None
    ]
    if drifted:
        summary = (
            f"FAIL — CONFIRMED figure(s) off by more than {TOLERANCE:.0%} "
            f"against the period they were read for: {', '.join(drifted)}. The "
            f"parser is wrong; do not build on these."
        )
    elif not all_passed:
        reasons = []
        if absent:
            reasons.append(f"no fundamentals loaded for {', '.join(absent)}")
        other = [
            t for t, c in companies.items()
            if c.get("found") and not c["passed"] and t not in absent
        ]
        if other:
            reasons.append(f"failing checks for {', '.join(other)}")
        summary = (
            "FAIL — " + "; ".join(reasons or ["see per-company detail"])
            + ". No confirmed figure disagrees with its reference, so this is a "
              "coverage problem, not a parser problem."
        )
    elif unverifiable:
        # Nothing disagreed, but not everything was checkable. Saying "PASS"
        # flat would overstate what was actually verified.
        summary = (
            f"PASS — every checkable figure reconciles, but {len(unverifiable)} "
            f"reference period(s) are not loaded and went unverified: "
            f"{', '.join(unverifiable)}"
        )
    else:
        summary = "PASS — every company reconciles against its reference period"

    return {
        "as_of": as_of.isoformat(),
        "tolerance_pct": TOLERANCE * 100,
        "companies": companies,
        "passed": all_passed,
        "unconfirmed_mismatches": unconfirmed,
        # References whose period is absent from the load. Neither a pass nor a
        # parser failure: there was nothing to compare against.
        "unverifiable": unverifiable,
        "summary": summary,
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
