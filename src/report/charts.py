"""Plotly charts, exported as base64 PNG (or inline SVG fallback).

Dark theme, no chartjunk, direct labels over legends where possible.

Kaleido is required for static export. If it is unavailable we fall back to
embedding the interactive figure as HTML rather than failing the report -- a
report with live charts still ships; a crashed report does not.
"""

from __future__ import annotations

import base64
import datetime as dt
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

BG = "#0d1117"
PANEL = "#161b22"
GRID = "#21262d"
FG = "#c9d1d9"
MUTED = "#8b949e"
UP = "#3fb950"
DOWN = "#f85149"
ACCENT = "#58a6ff"
WARN = "#d29922"

_kaleido_ok: bool | None = None


def _go():
    import plotly.graph_objects as go

    return go


def _layout(fig, title: str, height: int = 400) -> Any:
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=FG), x=0.01, xanchor="left"),
        paper_bgcolor=BG,
        plot_bgcolor=PANEL,
        font=dict(color=FG, size=11, family="Inter, system-ui, sans-serif"),
        height=height,
        margin=dict(l=50, r=25, t=45, b=40),
        showlegend=False,
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID)
    return fig


def to_base64(fig, width: int = 1000, height: int | None = None) -> str:
    """PNG as a data URI. Falls back to inline HTML if kaleido is missing."""
    global _kaleido_ok
    if _kaleido_ok is not False:
        try:
            png = fig.to_image(format="png", width=width, height=height, scale=2)
            _kaleido_ok = True
            return "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception as exc:  # noqa: BLE001 - kaleido optional
            if _kaleido_ok is None:
                log.warning("kaleido_unavailable_embedding_html", error=str(exc)[:200])
            _kaleido_ok = False
    return ""


def to_html(fig) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=False, config={
        "displayModeBar": False, "responsive": True,
    })


def render(fig, width: int = 1000, height: int | None = None) -> dict[str, str]:
    """Return {'img': data-uri} or {'html': ...} for the template."""
    uri = to_base64(fig, width, height)
    return {"img": uri} if uri else {"html": to_html(fig)}


# ---------------------------------------------------------------------------
# 1. Price panel with event annotations
# ---------------------------------------------------------------------------
def price_panel(
    ticker: str,
    bars: pd.DataFrame,
    *,
    events: Sequence[dict[str, Any]] = (),
    high_52w: float | None = None,
    low_52w: float | None = None,
) -> dict[str, str]:
    """1-year candles, EMA20/SMA50/SMA200, volume below, event markers.

    The event markers are the "notable events" overlay: earnings dates, 8-K
    filings, insider buys, and news volume spikes, all on the price axis so
    they line up with what the stock actually did.
    """
    go = _go()
    from plotly.subplots import make_subplots

    if bars.empty:
        return {"html": ""}

    df = bars.tail(252).copy()
    x = pd.to_datetime(df["date"])

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.76, 0.24], vertical_spacing=0.03,
    )
    fig.add_trace(
        go.Candlestick(
            x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            increasing_line_color=UP, decreasing_line_color=DOWN,
            increasing_fillcolor=UP, decreasing_fillcolor=DOWN,
            line=dict(width=1), name="",
        ),
        row=1, col=1,
    )

    close = df["close"].astype(float)
    for span, colour, label, kind in (
        (20, ACCENT, "EMA20", "ema"),
        (50, WARN, "SMA50", "sma"),
        (200, MUTED, "SMA200", "sma"),
    ):
        series = (
            close.ewm(span=span, adjust=False).mean()
            if kind == "ema"
            else close.rolling(span, min_periods=max(2, span // 2)).mean()
        )
        fig.add_trace(
            go.Scatter(x=x, y=series, line=dict(color=colour, width=1.2), name=label),
            row=1, col=1,
        )
        # Direct label at the right edge instead of a legend.
        if series.notna().any():
            fig.add_annotation(
                x=x.iloc[-1], y=series.iloc[-1], text=label, showarrow=False,
                xanchor="left", xshift=4, font=dict(color=colour, size=9), row=1, col=1,
            )

    for level, label, colour in (
        (high_52w, "52w high", MUTED), (low_52w, "52w low", MUTED)
    ):
        if level and np.isfinite(level):
            fig.add_hline(
                y=level, line=dict(color=colour, width=0.8, dash="dot"),
                annotation_text=label, annotation_font=dict(size=9, color=colour),
                row=1, col=1,
            )

    colours = np.where(df["close"] >= df["open"], UP, DOWN)
    fig.add_trace(
        go.Bar(x=x, y=df["volume"], marker_color=colours, opacity=0.55, name=""),
        row=2, col=1,
    )

    ymin = float(df["low"].min())
    for ev in events:
        d = ev.get("date")
        if d is None:
            continue
        colour = {
            "earnings": WARN, "insider_buy": UP, "filing": ACCENT, "news_spike": "#bc8cff",
        }.get(ev.get("kind", ""), MUTED)
        fig.add_vline(
            x=pd.Timestamp(d), line=dict(color=colour, width=1, dash="dot"), row=1, col=1
        )
        fig.add_annotation(
            x=pd.Timestamp(d), y=ymin, text=ev.get("label", "")[:14], showarrow=False,
            font=dict(size=8, color=colour), textangle=-90, yanchor="bottom",
            row=1, col=1,
        )

    fig.update_xaxes(rangeslider_visible=False)
    _layout(fig, f"{ticker} — price, moving averages and events", height=520)
    fig.update_yaxes(title_text="Volume", row=2, col=1)
    return render(fig, height=520)


# ---------------------------------------------------------------------------
# 2. Factor radar
# ---------------------------------------------------------------------------
def factor_radar(
    ticker: str, subscores: dict[str, int], median: dict[str, float] | None = None
) -> dict[str, str]:
    """Hexagon of the six rubric subscores, with the universe median behind."""
    go = _go()
    from src.config.factor_weights import RUBRIC_MAX

    labels = ["trend", "fundamental", "catalyst", "news", "macro", "risk"]
    # Normalise to 0-100 so the six axes are comparable despite different caps.
    vals = [subscores.get(k, 0) / RUBRIC_MAX[k] * 100 for k in labels]
    fig = go.Figure()

    if median:
        med = [median.get(k, 0) / RUBRIC_MAX[k] * 100 for k in labels]
        fig.add_trace(
            go.Scatterpolar(
                r=med + med[:1], theta=labels + labels[:1], fill="toself",
                fillcolor="rgba(139,148,158,0.18)", line=dict(color=MUTED, width=1),
                name="universe median",
            )
        )
    fig.add_trace(
        go.Scatterpolar(
            r=vals + vals[:1], theta=labels + labels[:1], fill="toself",
            fillcolor="rgba(88,166,255,0.35)", line=dict(color=ACCENT, width=2),
            name=ticker,
        )
    )
    fig.update_layout(
        polar=dict(
            bgcolor=PANEL,
            radialaxis=dict(range=[0, 100], gridcolor=GRID, tickfont=dict(size=9)),
            angularaxis=dict(gridcolor=GRID, tickfont=dict(size=10, color=FG)),
        )
    )
    _layout(fig, f"{ticker} — rubric subscores (% of max)", height=330)
    return render(fig, width=520, height=330)


# ---------------------------------------------------------------------------
# 3. Fundamental trend
# ---------------------------------------------------------------------------
def fundamental_trend(ticker: str, quarterly: pd.DataFrame) -> dict[str, str]:
    """8-quarter revenue and EPS bars, FCF margin as a line on a second axis."""
    go = _go()
    from plotly.subplots import make_subplots

    if quarterly is None or quarterly.empty:
        return {"html": ""}

    df = quarterly.tail(8)
    x = [str(i)[:10] for i in df.index]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if "revenue" in df.columns:
        fig.add_trace(
            go.Bar(x=x, y=df["revenue"] / 1e6, marker_color=ACCENT, opacity=0.85,
                   name="Revenue ($m)"),
            secondary_y=False,
        )
    if "eps" in df.columns:
        fig.add_trace(
            go.Scatter(x=x, y=df["eps"], line=dict(color=WARN, width=2),
                       mode="lines+markers", name="EPS"),
            secondary_y=True,
        )
    if {"free_cash_flow", "revenue"}.issubset(df.columns):
        margin = (df["free_cash_flow"] / df["revenue"].replace(0, np.nan)) * 100
        fig.add_trace(
            go.Scatter(x=x, y=margin, line=dict(color=UP, width=2, dash="dot"),
                       mode="lines+markers", name="FCF margin %"),
            secondary_y=True,
        )
    fig.update_yaxes(title_text="Revenue ($m)", secondary_y=False, gridcolor=GRID)
    fig.update_yaxes(title_text="EPS / FCF margin %", secondary_y=True, gridcolor=GRID)
    fig.update_layout(showlegend=True, legend=dict(
        orientation="h", y=1.12, x=0, font=dict(size=9), bgcolor="rgba(0,0,0,0)"
    ))
    _layout(fig, f"{ticker} — 8-quarter fundamentals", height=330)
    return render(fig, height=330)


# ---------------------------------------------------------------------------
# 4. News timeline
# ---------------------------------------------------------------------------
def news_timeline(
    ticker: str, timeline: list[dict[str, Any]], *, x_range: tuple | None = None
) -> dict[str, str]:
    """GDELT volume bars with tone as a coloured line.

    Shares the x-axis range with the price panel so events line up visually.
    """
    go = _go()
    from plotly.subplots import make_subplots

    if not timeline:
        return {"html": ""}

    dates = [t.get("date") for t in timeline]
    vols = [t.get("volume") for t in timeline]
    tones = [t.get("tone") for t in timeline]

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(x=dates, y=vols, marker_color=MUTED, opacity=0.6, name="Article volume"),
        secondary_y=False,
    )
    if any(t is not None for t in tones):
        colours = [UP if (t or 0) >= 0 else DOWN for t in tones]
        fig.add_trace(
            go.Scatter(x=dates, y=tones, mode="lines+markers", name="Tone",
                       line=dict(color=ACCENT, width=1.6),
                       marker=dict(color=colours, size=5)),
            secondary_y=True,
        )
        fig.add_hline(y=0, line=dict(color=GRID, width=1), secondary_y=True)

    if x_range:
        fig.update_xaxes(range=list(x_range))
    fig.update_yaxes(title_text="Articles", secondary_y=False)
    fig.update_yaxes(title_text="Tone", secondary_y=True)
    _layout(fig, f"{ticker} — news volume and tone", height=260)
    return render(fig, height=260)


# ---------------------------------------------------------------------------
# 5. Peer comparison
# ---------------------------------------------------------------------------
def peer_comparison(
    ticker: str, peers: pd.DataFrame, value_col: str = "factor_composite"
) -> dict[str, str]:
    """The ticker vs its 5 nearest sector peers on the composite score."""
    go = _go()
    if peers is None or peers.empty:
        return {"html": ""}

    df = peers.sort_values(value_col)
    colours = [ACCENT if i == ticker else MUTED for i in df.index]
    fig = go.Figure(
        go.Bar(
            x=df[value_col], y=list(df.index), orientation="h",
            marker_color=colours,
            text=[f"{v:+.2f}" for v in df[value_col]],
            textposition="outside", textfont=dict(size=9, color=FG),
        )
    )
    fig.add_vline(x=0, line=dict(color=GRID, width=1))
    _layout(fig, f"{ticker} vs sector peers — composite z", height=280)
    fig.update_xaxes(title_text="composite z-score")
    return render(fig, width=620, height=280)


# ---------------------------------------------------------------------------
# 6. Macro dashboard
# ---------------------------------------------------------------------------
def macro_dashboard(macro_summary: dict[str, Any], series: dict[str, list]) -> dict[str, str]:
    go = _go()
    from plotly.subplots import make_subplots

    panels = [
        ("T10Y2Y", "10y-2y curve"),
        ("BAMLH0A0HYM2", "HY OAS"),
        ("ICSA", "Initial claims"),
        ("NFCI", "Financial conditions"),
        ("VIXCLS", "VIX"),
        ("UNRATE", "Unemployment"),
    ]
    fig = make_subplots(
        rows=2, cols=3, subplot_titles=[p[1] for p in panels],
        vertical_spacing=0.18, horizontal_spacing=0.08,
    )
    for i, (sid, _) in enumerate(panels):
        obs = series.get(sid) or []
        if not obs:
            continue
        x = [o["date"] for o in obs]
        y = [o["value"] for o in obs]
        colour = UP if (y and y[-1] >= (y[0] if y else 0)) else DOWN
        fig.add_trace(
            go.Scatter(x=x, y=y, line=dict(color=colour, width=1.4), mode="lines"),
            row=i // 3 + 1, col=i % 3 + 1,
        )
    regime = macro_summary.get("regime", "?")
    _layout(fig, f"Macro dashboard — regime: {regime}", height=420)
    for ann in fig.layout.annotations:
        ann.font.size = 10
        ann.font.color = MUTED
    return render(fig, height=420)


# ---------------------------------------------------------------------------
# 7. Sector heatmap
# ---------------------------------------------------------------------------
def sector_heatmap(stage_sectors: dict[str, dict[str, int]]) -> dict[str, str]:
    """Where survivors clustered at each funnel stage.

    Shown as share-of-stage rather than raw counts, so the narrowing funnel does
    not make later stages look uniformly empty.
    """
    go = _go()
    if not stage_sectors:
        return {"html": ""}

    stages = list(stage_sectors)
    sectors = sorted({s for d in stage_sectors.values() for s in d})
    if not sectors:
        return {"html": ""}

    z = []
    text = []
    for sec in sectors:
        row, trow = [], []
        for st in stages:
            counts = stage_sectors[st]
            total = sum(counts.values()) or 1
            n = counts.get(sec, 0)
            row.append(n / total * 100)
            trow.append(str(n))
        z.append(row)
        text.append(trow)

    fig = go.Figure(
        go.Heatmap(
            z=z, x=stages, y=sectors, text=text, texttemplate="%{text}",
            textfont=dict(size=9), colorscale="Blues", showscale=True,
            colorbar=dict(title="% of stage", tickfont=dict(size=9)),
        )
    )
    _layout(fig, "Sector composition by funnel stage (cell = count)", height=420)
    return render(fig, height=420)


# ---------------------------------------------------------------------------
# 8. Funnel visualisation
# ---------------------------------------------------------------------------
def funnel_chart(counts: dict[str, int], rejects: dict[str, dict[str, int]] | None = None) -> dict[str, str]:
    go = _go()
    if not counts:
        return {"html": ""}

    stages = list(counts)
    values = [counts[s] for s in stages]
    labels = []
    for i, s in enumerate(stages):
        dropped = values[i - 1] - values[i] if i else 0
        top_reason = ""
        if rejects and s in rejects and rejects[s]:
            top = max(rejects[s].items(), key=lambda kv: kv[1])
            top_reason = f"<br><span style='font-size:9px'>top cut: {top[0]}</span>"
        labels.append(
            f"{s}: {values[i]:,}"
            + (f" (-{dropped:,})" if dropped > 0 else "")
            + top_reason
        )

    fig = go.Figure(
        go.Funnel(
            y=labels, x=values, textinfo="none",
            marker=dict(color=[ACCENT, "#4b91e2", "#3f7cc9", WARN, "#c78a1f", UP][: len(stages)]),
            connector=dict(line=dict(color=GRID, width=1)),
        )
    )
    _layout(fig, "Funnel — what each stage rejected", height=430)
    return render(fig, height=430)


def build_event_markers(
    *,
    earnings_dates: Sequence[dt.date] = (),
    filings: Sequence[dict[str, Any]] = (),
    insider_buys: Sequence[dict[str, Any]] = (),
    news_spikes: Sequence[dt.date] = (),
) -> list[dict[str, Any]]:
    """Assemble the price-panel event overlay."""
    out: list[dict[str, Any]] = []
    for d in earnings_dates:
        out.append({"date": d, "kind": "earnings", "label": "earnings"})
    for f in filings:
        form = f.get("form", "")
        if form.startswith("8-K") or form in {"SC 13D", "S-3", "424B5"}:
            out.append({"date": f.get("filing_date"), "kind": "filing", "label": form})
    for t in insider_buys:
        if (t.get("code") or "").upper() == "P":
            out.append({"date": t.get("date"), "kind": "insider_buy", "label": "insider buy"})
    for d in news_spikes:
        out.append({"date": d, "kind": "news_spike", "label": "news spike"})
    return [e for e in out if e.get("date") is not None]
