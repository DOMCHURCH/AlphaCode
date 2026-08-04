"""View 3: where the money goes.

Revenue in at the top, out through cost of sales, operating costs and tax, with
what survives at the bottom. Most people have no idea a grocery chain keeps 2%
and a software company keeps 30%; one picture per company makes that obvious.

Only rendered where the components exist. A partial flow -- revenue and net
income with the middle missing -- would invite the reader to infer the gap, so
the view is skipped instead.

Duration facts, so figures are trailing twelve months: four quarters summed, or
a single annual filing. Mixing a quarterly figure into an annual comparison is
the error that makes a company look 4x more profitable than it is, so the
period basis is computed once and every line uses it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# The flow, in the order money actually leaves.
STAGES: tuple[tuple[str, str, str], ...] = (
    ("cogs", "Cost of sales", "What it costs to make or buy what it sells"),
    ("opex", "Operating costs", "Running the business: staff, marketing, admin"),
    ("income_tax", "Tax", "Tax on profit"),
    ("other_costs", "Everything else", "Interest and other items"),
)

# Below this share of revenue a stage is too thin to draw as its own band.
MIN_STAGE_PCT = 0.5

# If the unexplained remainder is bigger than this, the flow is not a flow --
# it is one giant "everything else" block with a couple of slivers beside it.
# A bank is the clearest case: it reports no cost of sales because it has none,
# so its costs are interest and provisions that these concepts do not carry.
# Drawing that anyway would invite the reader to infer a breakdown that is not
# there, which is exactly what the brief says to avoid.
MAX_REMAINDER_PCT = 40.0


@dataclass
class FlowStage:
    key: str
    label: str
    caption: str
    value: float
    pct: float
    derived: bool = False


@dataclass
class View3:
    ticker: str
    period_basis: str            # "ttm" | "annual"
    periods_used: int
    period_end: dt.date
    filing_date: dt.date
    revenue: float
    net_income: float
    stages: list[FlowStage] = field(default_factory=list)
    margin_pct: float = 0.0
    loss_making: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "period_basis": self.period_basis,
            "periods_used": self.periods_used,
            "period_end": self.period_end.isoformat(),
            "filing_date": self.filing_date.isoformat(),
            "revenue": self.revenue,
            "net_income": self.net_income,
            "margin_pct": round(self.margin_pct, 2),
            "loss_making": self.loss_making,
            "stages": [
                {
                    "key": s.key, "label": s.label, "caption": s.caption,
                    "value": s.value, "pct": round(s.pct, 3), "derived": s.derived,
                }
                for s in self.stages
            ],
            "notes": self.notes,
        }


def _trailing(
    rows: list[tuple[str, float, dt.date, str | None]],
) -> tuple[dict[str, float], str, int, dt.date] | None:
    """Sum duration facts to a trailing-twelve-month basis.

    An annual filing stands alone. Otherwise four consecutive quarters are
    summed. Fewer than four and there is no comparable year, so nothing is
    drawn -- a nine-month figure presented as a year is simply wrong.
    """
    by_period: dict[dt.date, dict[str, float]] = {}
    fiscal: dict[dt.date, str | None] = {}
    for metric, value, period_end, fp in rows:
        by_period.setdefault(period_end, {})[metric] = value
        fiscal[period_end] = fp

    if not by_period:
        return None
    periods = sorted(by_period, reverse=True)
    latest = periods[0]

    # An annual filing already covers twelve months.
    if (fiscal.get(latest) or "").upper() == "FY":
        return by_period[latest], "annual", 1, latest

    if len(periods) < 4:
        return None
    window = periods[:4]
    summed: dict[str, float] = {}
    for p in window:
        for metric, value in by_period[p].items():
            summed[metric] = summed.get(metric, 0.0) + value
    return summed, "ttm", 4, latest


def build_view3(ticker: str, as_of: dt.date | None = None) -> View3 | None:
    """Compose the revenue flow, or None when the parts are not there."""
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    as_of = as_of or dt.date.today()
    wanted = (
        "revenue", "cogs", "gross_profit", "operating_income",
        "income_tax", "net_income",
    )

    with session_scope() as session:
        rows = session.execute(
            select(
                Fundamental.metric, Fundamental.value, Fundamental.period_end,
                Fundamental.fiscal_period, Fundamental.filing_date,
            )
            .where(Fundamental.ticker == ticker.upper())
            .where(Fundamental.metric.in_(wanted))
            .where(Fundamental.period_end <= as_of)
            .order_by(Fundamental.period_end.desc())
        ).all()

    if not rows:
        return None

    filing_by_period = {r[2]: r[4] for r in rows}
    trailing = _trailing([(r[0], float(r[1]), r[2], r[3]) for r in rows if r[1] is not None])
    if trailing is None:
        return None
    m, basis, periods_used, period_end = trailing

    revenue = m.get("revenue")
    net_income = m.get("net_income")
    if not revenue or revenue <= 0 or net_income is None:
        # Without both ends there is no flow to draw.
        return None

    view = View3(
        ticker=ticker.upper(),
        period_basis=basis,
        periods_used=periods_used,
        period_end=period_end,
        filing_date=filing_by_period.get(period_end, period_end),
        revenue=revenue,
        net_income=net_income,
        margin_pct=net_income / revenue * 100.0,
        loss_making=net_income < 0,
    )

    pct = lambda v: v / revenue * 100.0  # noqa: E731

    cogs = m.get("cogs")
    gross = m.get("gross_profit")
    if cogs is None and gross is not None:
        # Stated gross profit fixes cost of sales exactly; that is arithmetic on
        # the filer's own two figures, not an estimate.
        cogs = revenue - gross
    if cogs is not None and cogs > 0:
        view.stages.append(FlowStage(
            "cogs", "Cost of sales",
            "What it costs to make or buy what it sells",
            cogs, pct(cogs), derived=m.get("cogs") is None,
        ))

    # Operating costs are the gap between gross profit and operating income.
    operating = m.get("operating_income")
    if gross is None and cogs is not None:
        gross = revenue - cogs
    if operating is not None and gross is not None:
        opex = gross - operating
        if opex > 0:
            view.stages.append(FlowStage(
                "opex", "Operating costs",
                "Running the business: staff, marketing, admin",
                opex, pct(opex), derived=True,
            ))

    tax = m.get("income_tax")
    if tax is not None and tax > 0:
        view.stages.append(FlowStage(
            "income_tax", "Tax", "Tax on profit", tax, pct(tax),
        ))

    # Whatever the named stages do not account for. Named as a remainder so it
    # is never read as a line the filer reported.
    accounted = sum(s.value for s in view.stages) + net_income
    rest = revenue - accounted
    if rest > 0 and pct(rest) >= MIN_STAGE_PCT:
        view.stages.append(FlowStage(
            "other_costs", "Everything else",
            "Interest and other items",
            rest, pct(rest), derived=True,
        ))
        if pct(rest) > MAX_REMAINDER_PCT:
            log.info(
                "view3_remainder_dominates", ticker=ticker,
                remainder_pct=round(pct(rest), 1),
            )
            return None
    elif rest < 0:
        view.notes.append(
            "The reported costs add up to more than revenue less profit, so the "
            "flow is shown without a remainder."
        )

    # A flow of revenue straight to profit with nothing between is not a flow.
    if not view.stages:
        log.info("view3_no_stages", ticker=ticker)
        return None

    # Nor is one where the only named stage is a sliver. Two stages minimum, or
    # the picture says nothing the headline number does not.
    named = [st for st in view.stages if st.key != "other_costs"]
    if not named:
        log.info("view3_no_named_stages", ticker=ticker)
        return None

    if basis == "ttm":
        view.notes.append(
            "Figures are the last four quarters added together."
        )
    return view


def describe_flow(view: View3) -> list[str]:
    """One or two sentences restating the picture. Description, not judgement."""
    out: list[str] = []
    keep = view.margin_pct
    if view.loss_making:
        out.append(
            f"{view.ticker} spent more than it took in: revenue of "
            f"{_money(view.revenue)} against a loss of {_money(abs(view.net_income))}."
        )
    else:
        out.append(
            f"Of every dollar {view.ticker} took in, {keep / 100:.2f} was left "
            f"as profit after all costs and tax — {_money(view.net_income)} on "
            f"{_money(view.revenue)} of revenue."
        )
    biggest = max(view.stages, key=lambda s: s.value, default=None)
    if biggest is not None:
        out.append(
            f"The largest single outflow is {biggest.label.lower()}, at "
            f"{biggest.pct:.0f}% of revenue."
        )
    return out


def _money(v: float) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f} trillion"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.1f} billion"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.0f} million"
    return f"{sign}${a:,.0f}"
