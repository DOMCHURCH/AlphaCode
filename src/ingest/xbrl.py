"""XBRL fact extraction from SEC Financial Statement Data Sets.

Replaces the extraction that produced confidently wrong numbers: JPM total
assets read $641,190,000,000 when the consolidated figure is $4,424,900,000,000,
and JPM equity read -$1,426,000,000 when it is $362,438,000,000.

Neither was a missing concept. num.txt carries the same tag many times per
period at different dimensional levels, and the old parser took whichever row
came first. JPM's Assets appears 23 times; row 1 is `segments=Geographical=EMEA`,
so EMEA's assets became the company total. StockholdersEquity appears 14 times;
row 1 is `segments=EquityComponents=AccumulatedGainLossNetCashFlowHedgeParent`,
one line of the equity rollforward, so a hedge-accounting component became
shareholders' equity.

The old parser could not have filtered these even in principle: it read only
[adsh, tag, ddate, qtrs, uom, value] out of num.txt, so `coreg` and `segments`
-- the columns that carry the dimensional breakdown -- were never in memory.

Two rules, both enforced here rather than cleaned up downstream:

RULE ONE -- select the right fact.
  * Consolidated only: `coreg` empty AND `segments` empty. Any value in either
    means the row describes a segment, geography, equity component, or a
    coregistrant legal entity, not the company.
  * Balance-sheet items are instants: qtrs == 0. A nonzero qtrs on Assets is a
    change over a period, not a balance.
  * Income and cash-flow items are durations: qtrs in {1, 4}.

RULE TWO -- validate before storing. See `validate_facts`. A row that fails is
logged with ticker, tag, value and the rule that caught it, and is not written.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog

log = structlog.get_logger(__name__)

INSTANT = "instant"
DURATION = "duration"

# num.txt columns carrying dimensional breakdown. A consolidated fact has every
# one of these empty. Datasets before ~2021 omit `segments` entirely, so the test
# is "empty or absent", never "column must exist".
DIMENSION_COLUMNS = ("coreg", "segments")

# Duration facts we accept: one quarter (qtrs=1) or one year (qtrs=4).
#
# qtrs=2 and qtrs=3 are cumulative year-to-date windows. A 10-Q for Q3 typically
# reports BOTH the three-month figure (qtrs=1) and the nine-month cumulative one
# (qtrs=3); reading the latter as a quarterly figure would triple that quarter's
# revenue. So they are dropped -- but they are counted separately from other
# wrong-qtrs drops, and every duration fact's qtrs value is histogrammed, so a
# real load reports what filers actually do rather than what the spec says.
DURATION_QTRS = frozenset({"1", "4"})
YTD_CUMULATIVE_QTRS = frozenset({"2", "3"})


@dataclass(frozen=True)
class Concept:
    """One stored metric and the XBRL tags that can supply it.

    `tags` is in deterministic preference order -- the modern tag first. A filing
    reporting two aliases yields two rows with the same natural key, which aborts
    a Postgres upsert batch, so the alias collapse happens here where the tag is
    still known.
    """

    metric: str
    tags: tuple[str, ...]
    kind: str
    # Unit of measure this concept is reported in. Almost everything is a plain
    # USD amount, but per-share figures are USD/shares -- and a blanket
    # USD-only filter drops those silently, which looks exactly like "no filer
    # reports EPS".
    uom: str = "USD"


CONCEPTS: tuple[Concept, ...] = (
    # --- Balance sheet: instants (qtrs == 0) ---
    Concept("total_assets", ("Assets",), INSTANT),
    Concept("current_assets", ("AssetsCurrent",), INSTANT),
    Concept("total_liabilities", ("Liabilities",), INSTANT),
    Concept("current_liabilities", ("LiabilitiesCurrent",), INSTANT),
    # The filer's OWN stated right-hand side of the balance sheet. Thousands
    # report this directly, and where they do it beats reconstructing L + E
    # from separate tags: it is the total as filed, so it cannot disagree with
    # itself over which equity figure to include or what belongs in liabilities.
    Concept("liabilities_and_equity", ("LiabilitiesAndStockholdersEquity",), INSTANT),
    # Parent-only shareholders' equity -- what "shareholders' equity" normally
    # means, and what a reader expects to see.
    Concept("total_equity", ("StockholdersEquity",), INSTANT),
    # TOTAL equity, including the portion attributable to noncontrolling
    # interests. This is the figure the accounting identity actually balances
    # against: `Assets` is consolidated and includes the assets of partly-owned
    # subsidiaries, while `StockholdersEquity` excludes the outside investors'
    # share of them. For any filer with NCI, A - L - StockholdersEquity leaves
    # exactly the NCI behind, which reads as identity drift and is not.
    # Stored alongside rather than instead of the parent figure: they answer
    # different questions and neither substitutes for the other.
    Concept(
        "total_equity_incl_nci",
        ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
        INSTANT,
    ),
    Concept(
        "minority_interest",
        ("MinorityInterest", "StockholdersEquityAttributableToNoncontrollingInterest"),
        INSTANT,
    ),
    Concept("cash", ("CashAndCashEquivalentsAtCarryingValue",), INSTANT),
    Concept("receivables", ("AccountsReceivableNetCurrent",), INSTANT),
    Concept("inventory", ("InventoryNet",), INSTANT),
    Concept("property_plant_equipment", ("PropertyPlantAndEquipmentNet",), INSTANT),
    Concept("goodwill", ("Goodwill",), INSTANT),
    Concept(
        "intangibles",
        ("IntangibleAssetsNetExcludingGoodwill", "IntangibleAssetsNet"),
        INSTANT,
    ),
    Concept("long_term_debt", ("LongTermDebtNoncurrent", "LongTermDebt"), INSTANT),
    Concept("accounts_payable", ("AccountsPayableCurrent",), INSTANT),
    # --- What banks actually file ---
    # A bank's balance sheet has almost nothing in common with a manufacturer's.
    # It files no InventoryNet and no AccountsPayableCurrent, so against the
    # general component set it renders as one undifferentiated block -- which is
    # true but useless. These are the line items that make a bank legible.
    Concept("loans", ("LoansAndLeasesReceivableNetReportedAmount",
                      "NotesReceivableNet"), INSTANT),
    Concept("trading_securities", ("TradingSecurities",), INSTANT),
    Concept(
        "investment_securities",
        ("AvailableForSaleSecuritiesDebtSecurities",
         "HeldToMaturitySecurities",
         "MarketableSecurities"),
        INSTANT,
    ),
    Concept(
        "interbank_deposits", ("InterestBearingDepositsInBanks",), INSTANT
    ),
    Concept("deposits", ("Deposits",), INSTANT),
    Concept(
        "short_term_borrowings",
        ("SecuritiesSoldUnderAgreementsToRepurchase", "ShortTermBorrowings"),
        INSTANT,
    ),
    # --- Income and cash flow: durations (qtrs in {1, 4}) ---
    Concept(
        "revenue",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ),
        DURATION,
    ),
    Concept("net_income", ("NetIncomeLoss",), DURATION),
    Concept("operating_income", ("OperatingIncomeLoss",), DURATION),
    Concept(
        "operating_cash_flow",
        ("NetCashProvidedByUsedInOperatingActivities",),
        DURATION,
    ),
    Concept("capex", ("PaymentsToAcquirePropertyPlantAndEquipment",), DURATION),
    Concept("cogs", ("CostOfRevenue", "CostOfGoodsAndServicesSold"), DURATION),
    Concept("gross_profit", ("GrossProfit",), DURATION),
    Concept("income_tax", ("IncomeTaxExpenseBenefit",), DURATION),
    Concept("stock_compensation", ("ShareBasedCompensation",), DURATION),
    Concept(
        "depreciation_amortization",
        ("DepreciationDepletionAndAmortization",),
        DURATION,
    ),
    # Per-share, so USD/shares rather than USD.
    Concept("eps_diluted", ("EarningsPerShareDiluted",), DURATION, uom="USD/shares"),
    # --- More balance-sheet instants ---
    Concept(
        "retained_earnings", ("RetainedEarningsAccumulatedDeficit",), INSTANT
    ),
    Concept(
        "operating_lease_rou_asset", ("OperatingLeaseRightOfUseAsset",), INSTANT
    ),
)

_TAG_TO_CONCEPT: dict[str, Concept] = {
    tag: c for c in CONCEPTS for tag in c.tags
}
# Rank within a concept's alias tuple; lower wins.
_TAG_RANK: dict[str, int] = {
    tag: i for c in CONCEPTS for i, tag in enumerate(c.tags)
}

# Components that are part of total assets. Each must be <= total assets.
_ASSET_COMPONENTS = (
    "cash",
    "receivables",
    "inventory",
    "property_plant_equipment",
    "goodwill",
    "intangibles",
    "current_assets",
)

# How far assets may drift from liabilities + equity before we flag it.
BALANCE_TOLERANCE = 0.01

# A value this many times its own prior period is a units/scale error, not growth.
SCALE_JUMP_FACTOR = 100.0

# If validation (not structural filtering) rejects more than this share of
# company-periods, the parser is wrong and the load must fail rather than write
# partial data.
MAX_VALIDATION_REJECT_RATE = 0.20

# ...but a rate needs a denominator to mean anything. Below this many periods a
# single genuinely-bad filer would read as a catastrophic failure rate and abort
# a load that is fine. A real quarter carries thousands of company-periods, so
# this only ever relaxes the guard for small or targeted loads.
MIN_PERIODS_FOR_RATE_CHECK = 50


@dataclass
class ExtractionReport:
    """What the filter did, so a bad load is visible instead of silent."""

    rows_scanned: int = 0
    tag_matched: int = 0
    dropped_dimensional: int = 0
    dropped_wrong_qtrs: int = 0
    # Subset of dropped_wrong_qtrs: consolidated duration facts at qtrs 2 or 3,
    # i.e. cumulative year-to-date. Broken out because dropping these is a
    # judgement call, and the number makes it auditable on a real load.
    dropped_ytd_cumulative: int = 0
    # qtrs value -> count, over consolidated duration facts only. This is the
    # empirical answer to "what do filers actually report", read off the file.
    duration_qtrs_seen: dict[str, int] = field(default_factory=dict)
    dropped_non_usd: int = 0
    dropped_unparseable: int = 0
    dropped_no_ticker: int = 0
    dropped_alias_duplicate: int = 0
    kept: int = 0
    # Validation, counted separately: structural drops are expected and huge,
    # validation rejections should be rare.
    validated_periods: int = 0
    rejected_periods: int = 0
    rejections: list[dict[str, Any]] = field(default_factory=list)
    flags: list[dict[str, Any]] = field(default_factory=list)
    # Consolidated tags present in num.txt that NO concept maps. A low coverage
    # number is otherwise ambiguous -- "few companies report this" and "we are
    # reading the wrong tag for it" look identical. This census distinguishes
    # them: if a synonym of a thin concept shows up here in volume, the map has
    # a gap; if nothing relevant appears, the thinness is genuine.
    unmapped_tags: dict[str, int] = field(default_factory=dict)

    def top_unmapped(self, n: int = 40) -> list[dict[str, Any]]:
        return [
            {"tag": t, "count": c}
            for t, c in sorted(
                self.unmapped_tags.items(), key=lambda kv: -kv[1]
            )[:n]
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_scanned": self.rows_scanned,
            "tag_matched": self.tag_matched,
            "dropped_dimensional": self.dropped_dimensional,
            "dropped_wrong_qtrs": self.dropped_wrong_qtrs,
            "dropped_ytd_cumulative": self.dropped_ytd_cumulative,
            "duration_qtrs_seen": dict(sorted(self.duration_qtrs_seen.items())),
            "dropped_non_usd": self.dropped_non_usd,
            "dropped_unparseable": self.dropped_unparseable,
            "dropped_no_ticker": self.dropped_no_ticker,
            "dropped_alias_duplicate": self.dropped_alias_duplicate,
            "kept": self.kept,
            "validated_periods": self.validated_periods,
            "rejected_periods": self.rejected_periods,
            "reject_rate": round(
                self.rejected_periods / max(1, self.validated_periods), 4
            ),
            "rejections": self.rejections[:200],
            "flags": self.flags[:200],
            "top_unmapped_tags": self.top_unmapped(),
        }


class ExtractionError(RuntimeError):
    """The parser is wrong -- fail the load rather than write partial data."""


def coerce_float(value: Any) -> float | None:
    """Every value leaving the parser is a real, finite float or None.

    pandas NaN is a float and has bitten this codebase four times: it passes an
    `isinstance(x, float)` check, survives arithmetic as NaN, and lands in the DB
    as a null-that-isn't. Reject it here, once, rather than downstream forever.
    """
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def coerce_date(value: Any) -> dt.date | None:
    """SEC dataset dates are YYYYMMDD ints/strings."""
    s = str(value).strip()[:8]
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _cell(row: Any, col: str) -> str:
    """One num.txt cell as a stripped string, treating NaN/None as empty."""
    val = getattr(row, col, None)
    if val is None:
        return ""
    if isinstance(val, float) and math.isnan(val):
        return ""
    return str(val).strip()


def is_consolidated(row: Any) -> bool:
    """Consolidated == every dimensional column empty or absent.

    This is the rule the old parser lacked entirely. JPM's Assets rows differ
    only in `segments`; taking row 1 took `Geographical=EMEA`.
    """
    return all(not _cell(row, col) for col in DIMENSION_COLUMNS)


def _qtrs_ok(qtrs: str, kind: str) -> bool:
    if kind == INSTANT:
        return qtrs == "0"
    return qtrs in DURATION_QTRS


def _census_unmapped(unmapped: pd.DataFrame, report: ExtractionReport) -> None:
    """Count consolidated tags no concept reads, so thin coverage is diagnosable.

    Without this, a concept sitting at 31% coverage is ambiguous: either most
    filers genuinely do not report it, or they report it under a tag we do not
    map. Those need opposite fixes, and guessing between them is how a mapping
    gap gets rationalised as "that's just the data".
    """
    if unmapped.empty:
        return
    consolidated = unmapped
    for col in DIMENSION_COLUMNS:
        if col in consolidated.columns:
            vals = consolidated[col].fillna("").astype(str).str.strip()
            consolidated = consolidated[vals == ""]
    if consolidated.empty:
        return
    counts = consolidated["tag"].value_counts()
    for tag, n in counts.items():
        report.unmapped_tags[str(tag)] = report.unmapped_tags.get(str(tag), 0) + int(n)


def extract_facts(
    sub: pd.DataFrame,
    num: pd.DataFrame,
    cik_to_ticker: Mapping[str, str],
) -> tuple[list[dict[str, Any]], ExtractionReport]:
    """Map (num x sub) into consolidated `fundamentals` rows.

    period_end = num.ddate, filing_date = sub.filed -- the honest PIT pair. Every
    filing for a period is emitted; the PIT accessor picks the latest visible one.
    """
    report = ExtractionReport()
    if num.empty or sub.empty:
        return [], report

    report.rows_scanned = len(num)

    missing = [c for c in ("adsh", "tag", "ddate", "qtrs", "uom", "value") if c not in num.columns]
    if missing:
        raise ExtractionError(f"num.txt missing required columns {missing}")
    # `segments` is absent in pre-2021 datasets. `coreg` has always been present.
    # Warn loudly if BOTH are gone -- that means we cannot filter dimensionally
    # and every number we produce would be suspect.
    present_dims = [c for c in DIMENSION_COLUMNS if c in num.columns]
    if not present_dims:
        raise ExtractionError(
            "num.txt has neither `coreg` nor `segments`; dimensional facts cannot "
            "be distinguished from consolidated ones and the load would repeat the "
            "original bug."
        )

    mapped = num["tag"].isin(_TAG_TO_CONCEPT)

    # Census the consolidated tags we do NOT map, before discarding them. Only
    # consolidated instants are counted: dimensional rows would swamp the tally
    # with segment breakdowns of tags we already read.
    _census_unmapped(num[~mapped], report)

    facts = num[mapped]
    report.tag_matched = len(facts)
    if facts.empty:
        return [], report

    sub_meta = sub.set_index("adsh")[["cik", "filed", "fp"]]

    # (ticker, metric, period_end, filing_date) -> (alias_rank, row)
    best: dict[tuple[Any, ...], tuple[int, dict[str, Any]]] = {}

    for r in facts.itertuples(index=False):
        concept = _TAG_TO_CONCEPT[r.tag]

        if not is_consolidated(r):
            report.dropped_dimensional += 1
            continue

        qtrs = _cell(r, "qtrs")
        # Histogram every consolidated duration fact BEFORE filtering, so the
        # load reports what filers actually use rather than only what survived.
        if concept.kind == DURATION:
            report.duration_qtrs_seen[qtrs] = report.duration_qtrs_seen.get(qtrs, 0) + 1
        if not _qtrs_ok(qtrs, concept.kind):
            report.dropped_wrong_qtrs += 1
            if concept.kind == DURATION and qtrs in YTD_CUMULATIVE_QTRS:
                report.dropped_ytd_cumulative += 1
            continue

        # A fact in an unexpected unit is not comparable and must not be stored
        # as if it were. Checked per concept: per-share figures are USD/shares,
        # and a blanket USD-only test would drop every EPS fact silently.
        if _cell(r, "uom").upper() != concept.uom.upper():
            report.dropped_non_usd += 1
            continue

        value = coerce_float(getattr(r, "value", None))
        period_end = coerce_date(getattr(r, "ddate", None))
        if value is None or period_end is None:
            report.dropped_unparseable += 1
            continue

        adsh = getattr(r, "adsh", None)
        if adsh not in sub_meta.index:
            report.dropped_no_ticker += 1
            continue
        meta = sub_meta.loc[adsh]
        raw_cik = str(meta["cik"])
        ticker = cik_to_ticker.get(raw_cik.lstrip("0") or "0") or cik_to_ticker.get(raw_cik)
        if not ticker:
            report.dropped_no_ticker += 1
            continue

        filing_date = coerce_date(meta["filed"])
        if filing_date is None:
            report.dropped_unparseable += 1
            continue

        fiscal_period = str(meta["fp"] or "").strip()[:8] or None

        key = (ticker, concept.metric, period_end, filing_date)
        rank = _TAG_RANK.get(r.tag, 99)
        prev = best.get(key)
        if prev is not None:
            report.dropped_alias_duplicate += 1
            if rank >= prev[0]:
                continue
        best[key] = (
            rank,
            {
                "ticker": ticker,
                "metric": concept.metric,
                "value": value,
                "period_end": period_end,
                "fiscal_period": fiscal_period,
                "filing_date": filing_date,
                "source": "sec",
                "restated": False,
            },
        )

    rows = [row for _rank, row in best.values()]
    report.kept = len(rows)
    return rows, report


def _reject(
    report: ExtractionReport, row: dict[str, Any], rule: str, detail: str = ""
) -> None:
    rec = {
        "ticker": row.get("ticker"),
        "metric": row.get("metric"),
        "value": row.get("value"),
        "period_end": str(row.get("period_end")),
        "rule": rule,
    }
    if detail:
        rec["detail"] = detail
    report.rejections.append(rec)
    log.warning("xbrl_row_rejected", **rec)


def validate_facts(
    rows: Sequence[dict[str, Any]], report: ExtractionReport
) -> list[dict[str, Any]]:
    """Drop rows that cannot be true. Never repair, never impute.

    Grouped by (ticker, period_end, filing_date) because the identity checks are
    statements about one balance sheet as filed, not about a metric in isolation.
    """
    by_period: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
    for row in rows:
        key = (row["ticker"], row["period_end"], row["filing_date"])
        by_period.setdefault(key, {})[row["metric"]] = row

    kept: list[dict[str, Any]] = []
    for (ticker, period_end, _filed), metrics in by_period.items():
        report.validated_periods += 1
        assets = metrics.get("total_assets")
        drop_metrics: set[str] = set()

        # Rule: total assets must be positive. Negative or zero is impossible and
        # signals the wrong fact was selected -- the whole period is untrustworthy.
        if assets is not None:
            av = assets["value"]
            if av <= 0:
                _reject(report, assets, "total_assets_not_positive")
                report.rejected_periods += 1
                continue

            # Rule: no component of assets may exceed total assets.
            for comp in _ASSET_COMPONENTS:
                row = metrics.get(comp)
                if row is not None and row["value"] > av:
                    _reject(
                        report,
                        row,
                        "component_exceeds_total_assets",
                        f"total_assets={av:,.0f}",
                    )
                    drop_metrics.add(comp)

            # Rule: assets ~= liabilities + equity. Flag, do not drop -- the
            # identity can legitimately miss by a hair on rounding, and when it
            # misses badly we want the numbers visible while we work out why.
            #
            # Balance against TOTAL equity (including noncontrolling interests)
            # when the filer reports it. `Assets` is consolidated; parent-only
            # StockholdersEquity is not, so using it leaves the NCI as phantom
            # drift on every company that has any.
            # Prefer the filer's own stated total where they report it: a
            # reconstruction of L + E can only ever be as good as our choice of
            # which equity tag to add, and this has none of that ambiguity.
            stated = metrics.get("liabilities_and_equity")
            liabilities = metrics.get("total_liabilities")
            equity = metrics.get("total_equity_incl_nci") or metrics.get("total_equity")
            equity_basis = (
                "total_equity_incl_nci"
                if metrics.get("total_equity_incl_nci") is not None
                else "total_equity"
            )
            if stated is not None:
                rhs = stated["value"]
                equity_basis = "liabilities_and_equity"
                drift = abs(av - rhs) / av
                if drift > BALANCE_TOLERANCE:
                    flag = {
                        "ticker": ticker,
                        "period_end": str(period_end),
                        "rule": "balance_identity_drift",
                        "assets": av,
                        "liabilities_plus_equity": rhs,
                        "equity_basis": equity_basis,
                        "drift_pct": round(drift * 100, 2),
                    }
                    report.flags.append(flag)
                    log.warning("xbrl_balance_identity_drift", **flag)
            elif liabilities is not None and equity is not None:
                rhs = liabilities["value"] + equity["value"]
                drift = abs(av - rhs) / av
                if drift > BALANCE_TOLERANCE:
                    flag = {
                        "ticker": ticker,
                        "period_end": str(period_end),
                        "rule": "balance_identity_drift",
                        "assets": av,
                        "liabilities_plus_equity": rhs,
                        "equity_basis": equity_basis,
                        "drift_pct": round(drift * 100, 2),
                    }
                    # An NCI-sized gap on the parent-only figure is the known
                    # cause, so name it rather than leaving it as mystery drift.
                    nci = metrics.get("minority_interest")
                    if equity_basis == "total_equity" and nci is not None:
                        closed = abs(av - (rhs + nci["value"])) / av
                        if closed <= BALANCE_TOLERANCE:
                            flag["explained_by"] = "noncontrolling_interest"
                            flag["drift_pct_with_nci"] = round(closed * 100, 2)
                    report.flags.append(flag)
                    log.warning("xbrl_balance_identity_drift", **flag)

        for metric, row in metrics.items():
            if metric not in drop_metrics:
                kept.append(row)

    _flag_scale_jumps(kept, report)

    rate = report.rejected_periods / max(1, report.validated_periods)
    if (
        report.validated_periods >= MIN_PERIODS_FOR_RATE_CHECK
        and rate > MAX_VALIDATION_REJECT_RATE
    ):
        raise ExtractionError(
            f"validation rejected {report.rejected_periods}/{report.validated_periods} "
            f"company-periods ({rate:.1%} > {MAX_VALIDATION_REJECT_RATE:.0%}). The "
            f"parser is wrong; refusing to write partial data."
        )
    return kept


def _flag_scale_jumps(rows: Sequence[dict[str, Any]], report: ExtractionReport) -> None:
    """A value 100x its own prior period is a units or scale problem, not growth.

    Flagged rather than dropped: a genuine 100x is astronomically unlikely but a
    real filing could carry one, and silently deleting it would be its own bug.
    """
    series: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        series.setdefault((row["ticker"], row["metric"]), []).append(row)

    for (ticker, metric), items in series.items():
        items.sort(key=lambda r: r["period_end"])
        for prev, cur in zip(items, items[1:]):
            pv, cv = abs(prev["value"]), abs(cur["value"])
            if pv > 0 and cv > pv * SCALE_JUMP_FACTOR:
                flag = {
                    "ticker": ticker,
                    "metric": metric,
                    "rule": "scale_jump",
                    "period_end": str(cur["period_end"]),
                    "prior_value": prev["value"],
                    "value": cur["value"],
                    "ratio": round(cv / pv, 1),
                }
                report.flags.append(flag)
                log.warning("xbrl_scale_jump", **flag)


def extract_and_validate(
    sub: pd.DataFrame,
    num: pd.DataFrame,
    cik_to_ticker: Mapping[str, str],
) -> tuple[list[dict[str, Any]], ExtractionReport]:
    """The whole path: filter to consolidated facts, then validate before storing."""
    rows, report = extract_facts(sub, num, cik_to_ticker)
    if not rows:
        return [], report
    kept = validate_facts(rows, report)
    log.info("xbrl_extraction_done", **{
        k: v for k, v in report.as_dict().items() if k not in ("rejections", "flags")
    })
    return kept, report
