"""View 1: the balance sheet drawn to scale.

Two columns of the same height, because assets equal liabilities plus equity.
The proportions are the information -- a bank, an airline and a software company
have completely different shapes, and you see it before you read anything.

Composition rules, all of which exist to keep the drawing honest:

  * A missing component is ABSENT and named, never imputed and never silently
    folded into "other".
  * "Other" is always total minus the components we actually have, and it says
    so. Where components are missing it also says how many, because otherwise
    "other" would quietly absorb them and look like a real line item.
  * Where only the totals exist, the simplified three-block version renders.
    That covers ~89% of filers and is a real answer, not a degraded one.
  * Negative equity is drawn below the baseline rather than hidden. A company
    that owes more than it owns should look like one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Asset components, top to bottom, most liquid first. The order is the story:
# cash at the top, the things hardest to turn back into cash at the bottom.
ASSET_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("cash", "Cash"),
    ("receivables", "Receivables"),
    ("inventory", "Inventory"),
    ("property_plant_equipment", "Property & equipment"),
    ("goodwill", "Goodwill"),
    ("intangibles", "Intangibles"),
)

# Claim components, most senior first: trade creditors, then lenders, then the
# owners' residual last -- which is the order they get paid in.
LIABILITY_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("accounts_payable", "Accounts payable"),
    ("long_term_debt", "Long-term debt"),
)

# Blocks smaller than this share of the total get no inline label; the legend
# carries them. Chosen so a label never overflows its own block.
INLINE_LABEL_MIN_PCT = 7.0


@dataclass
class Block:
    """One drawn band."""

    key: str
    label: str
    value: float
    pct: float           # of total assets, so both columns share one scale
    kind: str            # asset | liability | equity
    tone: int = 0        # 0..5, position within its colour family
    is_remainder: bool = False
    note: str | None = None

    @property
    def inline_label(self) -> bool:
        return self.pct >= INLINE_LABEL_MIN_PCT


@dataclass
class View1:
    ticker: str
    company_name: str | None
    sector: str | None
    period_end: dt.date
    filing_date: dt.date
    mode: str                       # detailed | totals_only
    assets: list[Block] = field(default_factory=list)
    claims: list[Block] = field(default_factory=list)
    total_assets: float = 0.0
    total_liabilities: float | None = None
    total_equity: float | None = None
    missing_components: list[str] = field(default_factory=list)
    negative_equity: bool = False
    # Height of the claims column relative to assets. Exceeds 100 only when
    # equity is negative, which is exactly when it should.
    claims_span_pct: float = 100.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def blocks(bs: list[Block]) -> list[dict[str, Any]]:
            return [
                {
                    "key": b.key, "label": b.label, "value": b.value,
                    "pct": round(b.pct, 3), "kind": b.kind, "tone": b.tone,
                    "is_remainder": b.is_remainder, "note": b.note,
                    "inline_label": b.inline_label,
                }
                for b in bs
            ]

        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "sector": self.sector,
            "period_end": self.period_end.isoformat(),
            "filing_date": self.filing_date.isoformat(),
            "mode": self.mode,
            "assets": blocks(self.assets),
            "claims": blocks(self.claims),
            "total_assets": self.total_assets,
            "total_liabilities": self.total_liabilities,
            "total_equity": self.total_equity,
            "missing_components": self.missing_components,
            "negative_equity": self.negative_equity,
            "claims_span_pct": round(self.claims_span_pct, 3),
            "notes": self.notes,
        }


def _val(group: dict[str, Any], key: str) -> float | None:
    cell = group.get(key)
    if cell is None or cell.missing or cell.value is None:
        return None
    return float(cell.value)


def build_view1(ticker: str, as_of: dt.date | None = None) -> View1 | None:
    """Compose a scale drawing of the most recent balance sheet.

    Returns None when the ticker has no usable fundamentals -- the caller says
    so to the reader rather than rendering an empty frame.
    """
    from src.company.balancesheet import get_balance_sheet

    bs = get_balance_sheet(ticker, as_of=as_of)
    if bs is None:
        return None

    total_assets = _val(bs.assets, "total_assets")
    if not total_assets or total_assets <= 0:
        # Nothing can be drawn to scale without a positive total to scale to.
        log.info("view1_no_total_assets", ticker=ticker)
        return None

    total_liabilities = _val(bs.liabilities, "total_liabilities")
    equity_incl = _val(bs.equity, "total_equity_incl_nci")
    equity_parent = _val(bs.equity, "shareholders_equity")
    total_equity = equity_incl if equity_incl is not None else equity_parent

    sector = _sector_for(ticker)
    view = View1(
        ticker=bs.ticker,
        company_name=bs.company_name,
        sector=sector,
        period_end=bs.period_end,
        filing_date=bs.filing_date,
        mode="detailed",
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        total_equity=total_equity,
    )

    pct = lambda v: v / total_assets * 100.0  # noqa: E731

    # ---- left column: what it owns ----
    named_total = 0.0
    missing: list[str] = []
    for tone, (key, label) in enumerate(ASSET_COMPONENTS):
        v = _val(bs.assets, key)
        if v is None:
            missing.append(label)
            continue
        if v <= 0:
            continue
        named_total += v
        view.assets.append(
            Block(key=key, label=label, value=v, pct=pct(v), kind="asset", tone=tone)
        )

    remainder = total_assets - named_total
    if remainder > 0:
        note = None
        if missing:
            # Say it. Otherwise "other" quietly absorbs the unreported items and
            # reads as a real line item the filer stated.
            note = f"includes {len(missing)} line item{'s' if len(missing) > 1 else ''} this filer does not report separately"
        view.assets.append(
            Block(
                key="other_assets", label="Other assets", value=remainder,
                pct=pct(remainder), kind="asset", tone=6, is_remainder=True, note=note,
            )
        )
    elif remainder < 0:
        # Components exceeding the total should be impossible after validation,
        # so if it happens the drawing is not trustworthy and must say so.
        view.notes.append(
            "Reported components exceed total assets; the drawing is clipped."
        )

    view.missing_components = missing

    # ---- right column: who has a claim on it ----
    if total_liabilities is not None and total_equity is not None:
        named_liab = 0.0
        missing_liab: list[str] = []
        for tone, (key, label) in enumerate(LIABILITY_COMPONENTS):
            v = _val(bs.liabilities, key)
            if v is None:
                missing_liab.append(label)
                continue
            if v <= 0:
                continue
            named_liab += v
            view.claims.append(
                Block(key=key, label=label, value=v, pct=pct(v),
                      kind="liability", tone=tone)
            )

        liab_remainder = total_liabilities - named_liab
        if liab_remainder > 0:
            note = None
            if missing_liab:
                note = f"includes {len(missing_liab)} line item{'s' if len(missing_liab) > 1 else ''} this filer does not report separately"
            view.claims.append(
                Block(
                    key="other_liabilities", label="Other liabilities",
                    value=liab_remainder, pct=pct(liab_remainder),
                    kind="liability", tone=3, is_remainder=True, note=note,
                )
            )
        view.missing_components += missing_liab

        view.claims.append(
            Block(key="equity", label="Shareholders' equity", value=total_equity,
                  pct=pct(abs(total_equity)), kind="equity")
        )
        if total_equity < 0:
            view.negative_equity = True
            view.claims_span_pct = pct(total_liabilities)
            view.notes.append(
                "Liabilities exceed total assets, so equity is negative. It is "
                "drawn below the baseline."
            )
    else:
        # Totals-only: still a real answer, and it is the common case.
        view.mode = "totals_only"
        if total_liabilities is None:
            view.notes.append("This filer does not report a total for liabilities.")
        if total_equity is None:
            view.notes.append("This filer does not report a total for equity.")

    if view.mode == "detailed" and len(view.assets) <= 1 and len(view.claims) <= 2:
        # Only remainders survived: nothing is broken down, so present it as the
        # simplified view rather than implying a detail that is not there.
        view.mode = "totals_only"

    return view


def _sector_for(ticker: str) -> str | None:
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import SectorMap

    try:
        with session_scope() as session:
            return session.execute(
                select(SectorMap.sector).where(SectorMap.ticker == ticker.upper())
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - a missing sector must never break the page
        return None


def describe_shape(view: View1) -> list[str]:
    """Two or three plain sentences describing the shape.

    Description, not judgement: no score, no ranking, no investment language.
    Every clause is a restatement of a number already on the page.
    """
    out: list[str] = []
    ta = view.total_assets

    by_key = {b.key: b for b in view.assets}
    hard = sum(
        by_key[k].value for k in ("property_plant_equipment", "inventory")
        if k in by_key
    )
    soft = sum(
        by_key[k].value for k in ("cash", "receivables") if k in by_key
    )
    if hard or soft:
        if hard / ta >= 0.4:
            out.append(
                f"Most of what {view.ticker} owns is physical — property, "
                f"equipment and inventory account for {hard / ta * 100:.0f}% of "
                "total assets."
            )
        elif soft / ta >= 0.4:
            out.append(
                f"Most of what {view.ticker} owns is financial — cash and money "
                f"owed to it account for {soft / ta * 100:.0f}% of total assets."
            )

    if view.total_equity is not None and view.total_liabilities is not None:
        eq_share = view.total_equity / ta * 100.0
        if view.total_equity < 0:
            out.append(
                "Liabilities exceed total assets, so shareholders' equity is "
                f"negative at {_money(view.total_equity)}."
            )
        elif eq_share >= 60:
            out.append(
                f"It is mostly owner-funded: equity is {eq_share:.0f}% of the "
                "balance sheet, borrowings and other claims the rest."
            )
        elif eq_share >= 40:
            # A near-even split is neither, and calling it either would be
            # editorialising past what the number says.
            out.append(
                f"Funding is split roughly evenly: equity is {eq_share:.0f}% of "
                f"the balance sheet and liabilities {100 - eq_share:.0f}%."
            )
        else:
            out.append(
                f"It is mostly funded by others: equity is {eq_share:.0f}% of "
                "the balance sheet and liabilities are the remaining "
                f"{100 - eq_share:.0f}%."
            )

    if view.missing_components:
        out.append(
            "Not every line item is reported separately by this filer: "
            + ", ".join(view.missing_components[:4]).lower()
            + (" and others" if len(view.missing_components) > 4 else "")
            + " are inside the totals rather than broken out."
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
        return f"{sign}${a / 1e6:,.1f} million"
    return f"{sign}${a:,.0f}"
