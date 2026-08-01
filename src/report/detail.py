"""On-demand per-stock detail page, assembled from the database.

The daily report packs every finalist's charts into one big page. This renders
that same rich view for a SINGLE ticker on demand, so the site can link each
pick to its own detailed page: a full-size price chart with event markers, a
factor radar, an 8-quarter fundamentals panel, a news timeline, a sector-peer
comparison, and the written thesis (bull/bear/invalidation/risks/catalysts).

Everything comes from stored data, so any ticker the funnel has ever touched can
be opened -- not just today's top 10.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.factors.fundamentals import load_quarterly_wide
from src.report import charts as ch
from src.report.builder import _env, _f, _plotly_js
from src.storage.models import DailyScore, NewsAggregate, Thesis
from src.storage.pit import get_bars, get_filings, get_insider_transactions

log = structlog.get_logger(__name__)

_RUBRIC = ("trend", "fundamental", "catalyst", "news", "macro", "risk")

# Which stored factor_detail keys to surface as a "company health" table, and
# how to label them for a human.
_HEALTH_ROWS = [
    ("gross_profitability", "Gross profitability", "higher is better"),
    ("roic", "Return on invested capital", "higher is better"),
    ("fcf_yield", "Free-cash-flow yield", "higher is better"),
    ("accruals", "Accruals", "lower is better"),
    ("debt_trend", "Net-debt trend", "lower is better"),
    ("piotroski", "Piotroski F-score", "0-9, higher is better"),
    ("eps_rev_4w", "EPS revision (4w)", "consensus getting marked up"),
    ("sue", "Earnings surprise (SUE)", "standardized"),
]


def _latest_thesis(session: Session, symbol: str) -> Thesis | None:
    return session.execute(
        select(Thesis)
        .where(Thesis.ticker == symbol)
        .order_by(Thesis.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _latest_score(session: Session, symbol: str) -> DailyScore | None:
    return session.execute(
        select(DailyScore)
        .where(DailyScore.ticker == symbol)
        .order_by(DailyScore.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()


def _universe_median_subscores(session: Session, as_of: dt.date) -> dict[str, float]:
    """Median rubric subscores across that day's theses, for the radar backdrop."""
    rows = session.execute(
        select(Thesis.subscores).where(Thesis.as_of_date == as_of)
    ).scalars().all()
    acc: dict[str, list[float]] = {k: [] for k in _RUBRIC}
    for s in rows:
        if isinstance(s, dict):
            for k in _RUBRIC:
                v = s.get(k)
                if isinstance(v, (int, float)):
                    acc[k].append(float(v))
    return {k: (pd.Series(v).median() if v else 0.0) for k, v in acc.items()}


def _peer_frame(session: Session, as_of: dt.date, symbol: str, sector: str | None) -> pd.DataFrame:
    """Same-sector names on the composite z, for the peer bar chart."""
    if not sector:
        return pd.DataFrame()
    rows = session.execute(
        select(DailyScore.ticker, DailyScore.factor_composite, DailyScore.sector)
        .where(DailyScore.as_of_date == as_of)
        .where(DailyScore.sector == sector)
        .where(DailyScore.factor_composite.isnot(None))
    ).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ticker", "factor_composite", "sector"])
    df = df.set_index("ticker")
    # The subject plus its nearest peers by composite.
    if symbol in df.index:
        order = (df["factor_composite"] - df.loc[symbol, "factor_composite"]).abs().sort_values()
        keep = list(dict.fromkeys([symbol, *order.index[:6]]))
        df = df.loc[keep]
    else:
        df = df.sort_values("factor_composite", ascending=False).head(6)
    return df


def _health_table(score: DailyScore | None, quarterly: pd.DataFrame | None) -> list[dict[str, Any]]:
    detail = (score.factor_detail if score and isinstance(score.factor_detail, dict) else {}) or {}
    out: list[dict[str, Any]] = []
    for key, label, hint in _HEALTH_ROWS:
        v = detail.get(key)
        if isinstance(v, (int, float)):
            out.append({"label": label, "value": f"{float(v):.2f}", "hint": hint})
    # A couple of headline fundamentals straight off the latest quarter.
    if quarterly is not None and not quarterly.empty:
        last = quarterly.tail(1).iloc[0]
        if "revenue" in quarterly.columns and pd.notna(last.get("revenue")):
            out.append({"label": "Revenue (latest qtr)",
                        "value": f"${float(last['revenue']) / 1e6:,.0f}m", "hint": ""})
        if "gross_margin" in quarterly.columns and pd.notna(last.get("gross_margin")):
            out.append({"label": "Gross margin",
                        "value": f"{float(last['gross_margin']) * 100:.1f}%", "hint": ""})
    return out


def build_stock_detail(
    session: Session, symbol: str, as_of: dt.date | None = None
) -> str | None:
    """Render the standalone HTML detail page for one ticker, or None if the
    ticker is completely unknown (no bars and no score)."""
    symbol = symbol.upper().strip()
    thesis = _latest_thesis(session, symbol)
    score = _latest_score(session, symbol)
    as_of = as_of or (
        thesis.as_of_date if thesis else score.as_of_date if score else dt.date.today()
    )

    bars = get_bars(session, [symbol], as_of - dt.timedelta(days=400), as_of)
    if bars.empty and thesis is None and score is None:
        return None

    filings = get_filings(session, [symbol], as_of, days=180).get(symbol, [])
    insiders = get_insider_transactions(session, [symbol], as_of, days=180).get(symbol, [])
    quarterly_all = load_quarterly_wide(session, [symbol], as_of, quarters=8)
    quarterly = (
        quarterly_all.get(symbol) if isinstance(quarterly_all, dict) else quarterly_all
    )

    events = ch.build_event_markers(
        earnings_dates=[], filings=filings, insider_buys=insiders, news_spikes=[]
    )

    # Latest price + 1-day change for the header.
    price = change = None
    if not bars.empty:
        b = bars.sort_values("date")
        price = _f(b["close"].iloc[-1])
        if len(b) > 1 and _f(b["close"].iloc[-2]):
            change = (b["close"].iloc[-1] / b["close"].iloc[-2] - 1) * 100

    subscores = (thesis.subscores if thesis and isinstance(thesis.subscores, dict) else {}) or {}
    sector = getattr(score, "sector", None) if score else None

    charts = {
        "price": ch.price_panel(symbol, bars, events=events) if not bars.empty else {"html": ""},
        "radar": ch.factor_radar(symbol, subscores, _universe_median_subscores(session, as_of))
        if subscores
        else {"html": ""},
        "fundamentals": ch.fundamental_trend(symbol, quarterly)
        if quarterly is not None
        else {"html": ""},
        "peers": ch.peer_comparison(symbol, _peer_frame(session, as_of, symbol, sector)),
    }
    news = session.execute(
        select(NewsAggregate)
        .where(NewsAggregate.ticker == symbol)
        .order_by(NewsAggregate.as_of_date.desc())
        .limit(1)
    ).scalar_one_or_none()

    needs_plotly = any(c.get("html") for c in charts.values())

    context = {
        "ticker": symbol,
        "name": (thesis.name if thesis and getattr(thesis, "name", None) else None),
        "sector": sector or "Unknown",
        "as_of": as_of.isoformat(),
        "price": price,
        "change": change,
        "has_thesis": thesis is not None,
        "total_score": thesis.total_score if thesis else None,
        "conviction": thesis.conviction if thesis else None,
        "time_horizon_days": thesis.time_horizon_days if thesis else None,
        "rank": (score.final_rank if score and score.final_rank else None),
        "stage_reached": (score.stage_reached if score else None),
        "subscores": subscores,
        "rubric": _RUBRIC,
        "thesis": thesis.thesis if thesis else None,
        "bull_case": thesis.bull_case if thesis else None,
        "bear_case": thesis.bear_case if thesis else None,
        "invalidation": thesis.invalidation if thesis else None,
        "key_risks": (thesis.key_risks or []) if thesis else [],
        "catalysts_ahead": (thesis.catalysts_ahead or []) if thesis else [],
        "health": _health_table(score, quarterly),
        "news_tone": _f(getattr(news, "tone_avg", None)) if news else None,
        "news_volume": _f(getattr(news, "article_count", None)) if news else None,
        "charts": charts,
        "needs_plotly": needs_plotly,
        "plotly_js": _plotly_js() if needs_plotly else "",
    }
    return _env().get_template("stock_detail.html.j2").render(**context)
