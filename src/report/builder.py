"""Stage 6 -- assemble charts and render the report.

Output is a standalone HTML file: CSS inlined, images base64'd, one portable
file. A PDF is rendered alongside it via weasyprint when available.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from sqlalchemy.orm import Session

from src.catalysts.macro import MacroState, summarise_for_report
from src.config.factor_weights import (
    CATEGORY_WEIGHTS,
    MODE_FULL,
    MODE_LABEL,
    RUBRIC_MAX,
    mode_config,
)
from src.config.settings import get_settings
from src.llm.schemas import DeepDive
from src.report import charts as ch
from src.storage import repository
from src.storage.pit import get_bars, get_filings, get_insider_transactions

log = structlog.get_logger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["render_chart"] = _render_chart
    return env


def _render_chart(chart: dict[str, str] | None) -> Markup:
    """Charts arrive as {'img': data-uri} or {'html': ...}. Both are ours, so
    the raw embed is safe; nothing here comes from an external source."""
    if not chart:
        return Markup("")
    if chart.get("img"):
        return Markup(f'<img src="{chart["img"]}" alt="chart" loading="lazy">')
    if chart.get("html"):
        return Markup(chart["html"])
    return Markup("")


def build_report(
    session: Session,
    *,
    as_of: dt.date,
    run_id: str,
    dives: Sequence[DeepDive],
    scores: pd.DataFrame,
    trend_features: pd.DataFrame,
    detail: dict[str, dict[str, Any]],
    macro: MacroState,
    funnel_counts: dict[str, int],
    funnel_rejects: dict[str, dict[str, int]],
    stage_sectors: dict[str, dict[str, int]],
    near_misses: list[dict[str, Any]],
    api_calls: dict[str, int],
    cost: dict[str, Any],
    duration_s: float,
    warnings: Sequence[str] = (),
    fundamentals: dict[str, pd.DataFrame] | None = None,
    deterministic_names: Sequence[dict[str, Any]] = (),
    output_dir: str | None = None,
    mode: str = MODE_FULL,
) -> dict[str, str]:
    """Render HTML (+PDF). Returns {'html': path, 'pdf': path|''}"""
    out_dir = Path(output_dir or get_settings().report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mode_cat_w, mode_cat_f = mode_config(mode)
    universe_median = _universe_median_subscores(dives)
    names = []
    for d in dives:
        names.append(
            _build_name_block(
                session, d, as_of, scores, trend_features, detail, macro,
                universe_median, (fundamentals or {}).get(d.ticker),
            )
        )

    context = {
        "meta": {
            "as_of": as_of.isoformat(),
            "run_id": run_id,
            "generated_at": dt.datetime.now(dt.UTC).strftime(
                "%Y-%m-%d %H:%M UTC"
            ),
            "duration_s": duration_s,
            # Non-empty ONLY for a non-full mode. The template renders it in the
            # header and on every ticker card, so a partial-data artifact can
            # never be mistaken for the full composite -- including when a single
            # card is screenshotted out of context.
            "mode": mode,
            "mode_label": MODE_LABEL.get(mode, ""),
        },
        "macro": summarise_for_report(macro),
        "funnel": funnel_counts,
        "names": names,
        "deterministic_names": list(deterministic_names),
        "near_misses": near_misses,
        "warnings": list(warnings),
        "rubric_max": RUBRIC_MAX,
        # The weights ACTUALLY used to score this run, not the full-mode constants.
        # In momentum-only mode the composite is 100% momentum; printing the five
        # full-composite categories here would misrepresent what produced the
        # ranking on the very page that reports it.
        "weights": {
            "categories": mode_cat_w,
            "factors": {k: list(v) for k, v in mode_cat_f.items()},
        },
        "completeness": _completeness_rows(scores, [d.ticker for d in dives]),
        "completeness_categories": list(mode_cat_w),
        "api_calls": api_calls,
        "cost": cost,
        "charts": {
            "funnel": ch.funnel_chart(funnel_counts, funnel_rejects),
            "sector_heatmap": ch.sector_heatmap(stage_sectors),
            "macro": ch.macro_dashboard(summarise_for_report(macro), macro.series),
        },
    }

    needs_plotly = _needs_plotly(context)
    context["needs_plotly"] = needs_plotly
    context["plotly_js"] = _plotly_js() if needs_plotly else ""

    html = _env().get_template("report.html.j2").render(**context)
    html_path = out_dir / f"report_{as_of.isoformat()}.html"
    html_path.write_text(html, encoding="utf-8")

    pdf_path = _render_pdf(html, out_dir / f"report_{as_of.isoformat()}.pdf")

    # Persist the rendered bytes to the DB so the api service can serve them
    # regardless of which filesystem the worker rendered on. The disk copy stays
    # for local convenience.
    pdf_bytes = Path(pdf_path).read_bytes() if pdf_path else None
    repository.save_report_artifact(
        session, as_of, run_id=run_id, html=html, pdf=pdf_bytes
    )

    log.info(
        "report_written", html=str(html_path), pdf=pdf_path,
        names=len(names), size_kb=round(len(html) / 1024, 1),
        stored_in_db=True,
    )
    return {"html": str(html_path), "pdf": pdf_path}


def _build_name_block(
    session: Session,
    dive: DeepDive,
    as_of: dt.date,
    scores: pd.DataFrame,
    trend_features: pd.DataFrame,
    detail: dict[str, dict[str, Any]],
    macro: MacroState,
    universe_median: dict[str, float],
    quarterly: pd.DataFrame | None,
) -> dict[str, Any]:
    t = dive.ticker
    row = scores.loc[t] if t in scores.index else pd.Series(dtype=float)
    trend = trend_features.loc[t] if t in trend_features.index else pd.Series(dtype=float)
    det = detail.get(t, {})

    bars = get_bars(session, [t], as_of - dt.timedelta(days=400), as_of)
    filings = get_filings(session, [t], as_of, days=120).get(t, [])
    insiders = get_insider_transactions(session, [t], as_of, days=180).get(t, [])
    news = det.get("news") or {}

    events = ch.build_event_markers(
        earnings_dates=_earnings_dates(det, as_of),
        filings=filings,
        insider_buys=insiders,
        news_spikes=[],
    )

    sector = row.get("sector") if "sector" in row else "Unknown"
    peers = _peer_frame(scores, t, sector)

    return {
        "ticker": t,
        "sector": sector or "Unknown",
        "total_score": dive.total_score,
        "subscores": dive.subscores.model_dump(),
        "conviction": dive.conviction,
        "time_horizon_days": dive.time_horizon_days,
        "thesis": dive.thesis,
        "one_liner": _one_liner(dive.thesis),
        "bull_case": dive.bull_case,
        "bear_case": dive.bear_case,
        "invalidation": dive.invalidation,
        "key_risks": dive.key_risks,
        "catalysts_ahead": [c.model_dump() for c in dive.catalysts_ahead],
        "charts": {
            "price": ch.price_panel(
                t, bars, events=events,
                high_52w=_f(trend.get("high_52w")), low_52w=_f(trend.get("low_52w")),
            ),
            "radar": ch.factor_radar(t, dive.subscores.model_dump(), universe_median),
            "fundamentals": ch.fundamental_trend(t, quarterly)
            if quarterly is not None
            else {"html": ""},
            "news": ch.news_timeline(t, _news_timeline_points(news)),
            "peers": ch.peer_comparison(t, peers),
        },
    }


def _one_liner(thesis: str, max_chars: int = 150) -> str:
    first = thesis.split(". ")[0].strip()
    if len(first) > max_chars:
        first = first[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    return first if first.endswith((".", "…")) else first + "."


def _earnings_dates(detail: dict[str, Any], as_of: dt.date) -> list[dt.date]:
    raw = (detail.get("earnings") or {}).get("dates") or []
    out = []
    for d in raw:
        if isinstance(d, dt.date):
            out.append(d)
        elif isinstance(d, str):
            try:
                out.append(dt.date.fromisoformat(d[:10]))
            except ValueError:
                continue
    return out


def _news_timeline_points(news: dict[str, Any]) -> list[dict[str, Any]]:
    """The stored aggregate is a single day; the chart wants a series.

    We only draw what we actually have -- if no daily series was persisted, the
    chart is omitted rather than fabricated from one point.
    """
    series = news.get("timeline")
    if isinstance(series, list) and series:
        return series
    return []


def _peer_frame(scores: pd.DataFrame, ticker: str, sector: Any) -> pd.DataFrame:
    if "sector" not in scores.columns or "factor_composite" not in scores.columns:
        return pd.DataFrame()
    same = scores[scores["sector"] == sector]
    if ticker not in same.index or len(same) < 2:
        return pd.DataFrame()
    target = same.at[ticker, "factor_composite"]
    others = same.drop(index=ticker)
    nearest = (others["factor_composite"] - target).abs().nsmallest(5).index
    return same.loc[list(nearest) + [ticker], ["factor_composite"]]


def _universe_median_subscores(dives: Sequence[DeepDive]) -> dict[str, float]:
    if not dives:
        return {}
    frame = pd.DataFrame([d.subscores.model_dump() for d in dives])
    return {k: float(v) for k, v in frame.median().items()}


def _completeness_rows(scores: pd.DataFrame, tickers: Sequence[str]) -> list[dict]:
    rows = []
    for t in tickers:
        if t not in scores.index:
            continue
        r = scores.loc[t]
        row = {"ticker": t, "overall": _f(r.get("data_completeness")) or 0.0}
        for cat in CATEGORY_WEIGHTS:
            row[cat] = _f(r.get(f"completeness_{cat}")) or 0.0
        rows.append(row)
    return rows


def _needs_plotly(context: dict[str, Any]) -> bool:
    """True if any chart fell back to interactive HTML (kaleido missing)."""
    def walk(o: Any) -> bool:
        if isinstance(o, dict):
            if o.get("html"):
                return True
            return any(walk(v) for v in o.values())
        if isinstance(o, list):
            return any(walk(v) for v in o)
        return False

    return walk(context.get("charts")) or walk(context.get("names"))


def _plotly_js() -> str:
    """Inline plotly.js so the HTML stays a single portable file."""
    try:
        from plotly.offline import get_plotlyjs

        return get_plotlyjs()
    except Exception as exc:  # noqa: BLE001
        log.warning("plotlyjs_inline_failed", error=str(exc))
        return ""


def _render_pdf(html: str, path: Path) -> str:
    try:
        from weasyprint import HTML  # type: ignore

        HTML(string=html).write_pdf(str(path))
        return str(path)
    except Exception as exc:  # noqa: BLE001 - PDF is optional, HTML is the deliverable
        log.info("pdf_render_skipped", error=str(exc)[:200])
        return ""


def _f(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else f


def write_json_summary(
    path: Path, *, as_of: dt.date, dives: Sequence[DeepDive], macro: MacroState,
    funnel_counts: dict[str, int],
) -> None:
    """Machine-readable sibling of the HTML, for the API and for backtests."""
    payload = {
        "as_of": as_of.isoformat(),
        "regime": macro.regime,
        "funnel": funnel_counts,
        "names": [d.model_dump() for d in dives],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
