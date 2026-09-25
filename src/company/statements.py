"""Income statement and cash flow, with the checks that make them worth buying.

Read off `fundamentals`, like the balance sheet. Three facts about SEC data
shape everything here:

* A 10-K files the YEAR. Q4 on its own is almost never filed, so it is
  derived: the year minus the first nine months.
* A 10-Q files cash flow YEAR-TO-DATE only (6 months at Q2, 9 at Q3), which
  the extractor keeps as `<metric>_ytd`. The quarter is the difference.
* A derived figure is arithmetic on filed figures, never an estimate, and is
  named in `derived` so a reader can tell the two apart.

The checks are the product. Each is an identity the filer's own numbers must
satisfy; one that fails is reported with both sides and the gap, and one that
cannot be run says which figure is missing rather than passing silently:

* gross profit: revenue - cost of revenue = gross profit
* cash flow:    operating + investing + financing + FX effect = change in cash
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, select

from src.storage.db import session_scope

INCOME = (
    "revenue", "cogs", "gross_profit", "operating_income", "income_tax",
    "net_income", "eps_diluted",
)
CASH_FLOW = (
    "operating_cash_flow", "investing_cash_flow", "financing_cash_flow",
    "fx_effect_on_cash", "cash_change", "capex", "depreciation_amortization",
    "stock_compensation",
)
YTD_SUFFIX = "_ytd"
# Per-share and not additive across quarters: a Q4 EPS is not FY - 9M EPS.
NOT_ADDITIVE = frozenset({"eps_diluted"})
# Two period ends this close are one period (bulk data rounds to month end).
SAME_PERIOD_DAYS = 10
QUARTER_DAYS = 91.31
MAX_PERIODS = 48


def _tolerance(*values: float) -> float:
    """Filers round to thousands; half a percent of the largest term, or $1k."""
    return max(1_000.0, 0.005 * max(abs(v) for v in values))


def _kind(metric: str, fiscal_period: str | None) -> str:
    if metric.endswith(YTD_SUFFIX):
        return "YTD"
    return "FY" if fiscal_period == "FY" else "Q"


def _load(ticker: str, as_of: dt.date) -> dict[str, dict[str, dict[dt.date, tuple[float, dt.date]]]]:
    """{kind: {metric: {period_end: (value, filing_date)}}}, latest filing known by as_of."""
    from src.storage.models import Fundamental

    wanted = set(INCOME) | set(CASH_FLOW) | {m + YTD_SUFFIX for m in CASH_FLOW}
    out: dict[str, dict[str, dict[dt.date, tuple[float, dt.date]]]] = {
        "FY": {}, "Q": {}, "YTD": {},
    }
    with session_scope() as session:
        rows = session.execute(
            select(Fundamental.metric, Fundamental.value, Fundamental.period_end,
                   Fundamental.filing_date, Fundamental.fiscal_period)
            .where(and_(Fundamental.ticker == ticker,
                        Fundamental.metric.in_(wanted),
                        Fundamental.filing_date <= as_of))
            .order_by(Fundamental.filing_date.asc())
        ).all()
    for metric, value, period_end, filed, fp in rows:
        if value is None:
            continue
        kind = _kind(metric, fp)
        base = metric[: -len(YTD_SUFFIX)] if kind == "YTD" else metric
        # Ascending by filing date, so a later filing (a restatement)
        # overwrites the earlier one -- the latest figure known by as_of.
        out[kind].setdefault(base, {})[period_end] = (float(value), filed)
    return out


def _month_gap(a: int, b: int) -> int:
    d = abs(a - b) % 12
    return min(d, 12 - d)


def _fix_mislabelled_years(data: dict[str, dict[str, dict[dt.date, tuple[float, dt.date]]]]) -> None:
    """Move quarter figures stored as "FY" into the quarterly series, in place.

    Rows loaded before 2026-09-24 carried the FILING's fiscal period, so the
    quarterly-results note in a 10-K (three months, qtrs=1) was stored as
    "FY" -- Tesla's Q1-Q3 2024 net income sat beside its 2024 year. Two tests
    tell a year from a quarter without the filing:

    * the fiscal year end: a real year ends in the month most of the filer's
      years end in (give or take one, for 52/53-week years);
    * size: a year's revenue is about four quarters, so an "FY" revenue under
      twice the quarter before it is a quarter.
    """
    fy, q = data["FY"], data["Q"]
    months: dict[int, int] = {}
    for series in fy.values():
        for d in series:
            months[d.month] = months.get(d.month, 0) + 1
    if not months:
        return
    fye = max(months, key=lambda mo: months[mo])
    rev_q = q.get("revenue", {})
    moved: set[dt.date] = set()
    for series in fy.values():
        for d in series:
            if _month_gap(d.month, fye) > 1:
                moved.add(d)
    for d, (v, _filed) in fy.get("revenue", {}).items():
        prev = _near(rev_q, d - dt.timedelta(days=QUARTER_DAYS))
        if prev is not None and v > 0 and v < 2 * rev_q[prev][0]:
            moved.add(d)
    for metric, series in fy.items():
        for d in [d for d in series if d in moved]:
            v = series.pop(d)
            q.setdefault(metric, {}).setdefault(d, v)


def _cluster(dates: set[dt.date]) -> dict[dt.date, dt.date]:
    """Map each date to its period's representative (the latest of a cluster)."""
    rep: dict[dt.date, dt.date] = {}
    held: list[dt.date] = []
    for d in sorted(dates, reverse=True):
        if held and (held[-1] - d).days <= SAME_PERIOD_DAYS:
            rep[d] = held[-1]
            continue
        held.append(d)
        rep[d] = d
    return rep


def _normalise(data: dict[str, dict[str, dict[dt.date, tuple[float, dt.date]]]]) -> None:
    """Snap near-duplicate period ends onto one date, in place."""
    dates = {d for kind in data.values() for series in kind.values() for d in series}
    rep = _cluster(dates)
    for kind in data.values():
        for metric, series in kind.items():
            merged: dict[dt.date, tuple[float, dt.date]] = {}
            for d, (v, filed) in series.items():
                r = rep[d]
                if r not in merged or filed > merged[r][1]:
                    merged[r] = (v, filed)
            kind[metric] = merged


def _near(series: dict[dt.date, Any], target: dt.date) -> dt.date | None:
    for d in series:
        if abs((d - target).days) <= SAME_PERIOD_DAYS + 6:
            return d
    return None


def _quarter_index(d: dt.date, year_ends: list[dt.date]) -> int | None:
    """1-4 within its fiscal year, from the fiscal year ends the filer used."""
    for e in sorted(year_ends):
        if e >= d - dt.timedelta(days=SAME_PERIOD_DAYS):
            k = round((e - d).days / QUARTER_DAYS)
            if 0 <= k <= 3:
                return 4 - k
            break
    # No filed year end at or after d (the year in progress): count forward
    # from the last year end before it.
    before = [e for e in year_ends if e < d - dt.timedelta(days=SAME_PERIOD_DAYS)]
    if before:
        k = round((d - max(before)).days / QUARTER_DAYS)
        if 1 <= k <= 3:
            return k
    return None


def _checks(values: dict[str, float | None]) -> list[dict[str, Any]]:
    out = []

    def check(name: str, rule: str, left_terms: dict[str, float | None], right: str) -> None:
        missing = [k for k, v in left_terms.items() if v is None]
        if values.get(right) is None:
            missing.append(right)
        if missing:
            out.append({"check": name, "rule": rule, "status": "not_testable",
                        "missing": missing})
            return
        left = sum(v for v in left_terms.values())  # type: ignore[misc]
        r = values[right]
        gap = left - r  # type: ignore[operator]
        ok = abs(gap) <= _tolerance(left, r, *left_terms.values())  # type: ignore[arg-type]
        out.append({"check": name, "rule": rule,
                    "status": "passed" if ok else "failed",
                    "left": left, "right": r, "gap": gap})

    rev, cogs = values.get("revenue"), values.get("cogs")
    check("gross_profit", "revenue - cogs = gross_profit",
          {"revenue": rev, "-cogs": -cogs if cogs is not None else None}, "gross_profit")
    fx = values.get("fx_effect_on_cash")
    check("cash_flow", "operating + investing + financing + fx = cash_change",
          {"operating_cash_flow": values.get("operating_cash_flow"),
           "investing_cash_flow": values.get("investing_cash_flow"),
           "financing_cash_flow": values.get("financing_cash_flow"),
           # Most filers without foreign operations report no FX line at all;
           # that is zero, not a missing term.
           "fx_effect_on_cash": fx if fx is not None else 0.0},
          "cash_change")
    return out


def _period(values: dict[str, float | None], filed: dt.date | None,
            period_end: dt.date, fiscal_period: str, derived: list[str]) -> dict[str, Any]:
    return {
        "period_end": period_end.isoformat(),
        "fiscal_period": fiscal_period,
        "filing_date": filed.isoformat() if filed else None,
        "income_statement": {m: values.get(m) for m in INCOME},
        "cash_flow": {m: values.get(m) for m in CASH_FLOW},
        "derived": derived,
        "checks": _checks(values),
    }


def _annual(data: dict[str, Any]) -> list[dict[str, Any]]:
    fy = data["FY"]
    ends = sorted({d for s in fy.values() for d in s}, reverse=True)
    out = []
    for e in ends:
        values = {m: fy[m][e][0] if e in fy.get(m, {}) else None for m in INCOME + CASH_FLOW}
        filed = max((fy[m][e][1] for m in fy if e in fy[m]), default=None)
        out.append(_period(values, filed, e, "FY", []))
    return out


def _quarterly(data: dict[str, Any]) -> list[dict[str, Any]]:
    fy, q, ytd = data["FY"], data["Q"], data["YTD"]
    year_ends = sorted({d for s in fy.values() for d in s})
    ends = sorted({d for kind in (fy, q, ytd) for s in kind.values() for d in s}, reverse=True)
    index = {d: _quarter_index(d, year_ends) for d in ends}

    def cumulative(m: str, d: dt.date) -> float | None:
        """Year-to-date through the quarter ending d, from filed figures."""
        k = index.get(d)
        if k is None:
            return None
        if k == 4:
            v = fy.get(m, {}).get(d)
            return v[0] if v else None
        if k == 1:
            v = q.get(m, {}).get(d)
            return v[0] if v else None
        v = ytd.get(m, {}).get(d)
        if v:
            return v[0]
        # Income statements file every quarter, so YTD is the sum of them.
        total = 0.0
        cur = d
        for step in range(k):
            got = q.get(m, {}).get(cur)
            if got is None:
                return None
            total += got[0]
            if step == k - 1:
                break
            prev = _near(index, cur - dt.timedelta(days=QUARTER_DAYS))
            if prev is None:
                return None
            cur = prev
        return total

    out = []
    for d in ends:
        k = index.get(d)
        if k is None:
            continue
        values: dict[str, float | None] = {}
        derived: list[str] = []
        filed_dates = []
        for m in INCOME + CASH_FLOW:
            got = q.get(m, {}).get(d)
            if got is not None:
                values[m] = got[0]
                filed_dates.append(got[1])
                continue
            if m in NOT_ADDITIVE:
                values[m] = None
                continue
            now = cumulative(m, d)
            prev_end = _near(index, d - dt.timedelta(days=QUARTER_DAYS)) if k > 1 else None
            before = cumulative(m, prev_end) if prev_end else (0.0 if k == 1 else None)
            if now is None or before is None:
                values[m] = None
                continue
            values[m] = now - before
            derived.append(m)
            src = (fy if k == 4 else ytd).get(m, {}).get(d)
            if src:
                filed_dates.append(src[1])
        if all(v is None for v in values.values()):
            continue
        out.append(_period(values, max(filed_dates, default=None), d, f"Q{k}", derived))
    return out


def statements(ticker: str, period: str = "annual", years: int | None = None,
               as_of: dt.date | None = None) -> dict[str, Any] | None:
    """Income statement and cash flow per period, newest first, with checks.

    `period` is "annual" or "quarterly". `years` counts back from `as_of`
    (today when omitted); `as_of` reads only filings made by then.
    """
    from src.company.lookup import canonical_ticker

    symbol = canonical_ticker(ticker).upper()
    anchor = as_of or dt.date.today()
    data = _load(symbol, anchor)
    if not any(data[k] for k in data):
        return None
    _normalise(data)
    _fix_mislabelled_years(data)
    rows = _annual(data) if period == "annual" else _quarterly(data)
    if not rows:
        return None
    oldest = min((r["period_end"] for r in rows), default=None)
    if years:
        since = (anchor - dt.timedelta(days=int(years * 365.25))).isoformat()
        rows = [r for r in rows if r["period_end"] >= since]
    rows = rows[:MAX_PERIODS]
    tested = [c for r in rows for c in r["checks"] if c["status"] != "not_testable"]
    return {
        "ticker": symbol,
        "period": period,
        "as_of": as_of.isoformat() if as_of else None,
        "years_requested": years,
        "available_from": oldest,
        "periods": rows,
        "checks_summary": {
            "tested": len(tested),
            "passed": sum(1 for c in tested if c["status"] == "passed"),
            "failed": sum(1 for c in tested if c["status"] == "failed"),
        },
        "source": "SEC Financial Statement Data Sets (as reported); "
                  "derived figures are arithmetic on filed ones and listed in `derived`.",
    }
