"""GDELT 2.0: news volume, tone, and event themes at scale.

Free, no key, slow. Every response is cached in Redis for 6 hours.

Two APIs:
  * DOC 2.0 -- company-level article volume and tone timelines
  * GKG themes -- event detection (layoffs, bankruptcy, M&A, strikes, recalls)

The distinction that matters, and which the theme classifier encodes: layoffs
*at the company* are read by the market as margin expansion (short-term
positive). Layoffs *across the sector or economy* are a demand signal and a
negative. We separate them by checking whether the company is the GKG-identified
entity or merely co-mentioned.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable
from typing import Any

import numpy as np
import structlog

from src.ingest.base import APIClient

log = structlog.get_logger(__name__)

BASE_URL = "https://api.gdeltproject.org/api/v2"

# GKG themes with a directional read for equities.
THEME_MAP: dict[str, str] = {
    "ECON_LAYOFF": "layoff",
    "LAYOFFS": "layoff",
    "ECON_BANKRUPTCY": "bankruptcy",
    "ECON_STOCKMARKET": "market",
    "MERGER_ACQUISITION": "merger",
    "ACQUIRE": "merger",
    "STRIKE": "strike",
    "RECALL": "recall",
    "TRIAL": "legal",
    "LEGISLATION": "legal",
    "ECON_EARNINGSREPORT": "earnings",
}

TRACKED_THEMES = tuple(THEME_MAP)


def make_client(concurrency: int = 4) -> APIClient:
    return APIClient(
        "gdelt",
        BASE_URL,
        concurrency=concurrency,
        timeout=45.0,
        cache_ttl=6 * 3600,
    )


def _query_for(company_name: str, ticker: str) -> str:
    """Quote the company name so GDELT treats it as a phrase.

    We query on the name, not the ticker: three-letter tickers collide with
    ordinary English words and poison the volume series.
    """
    name = (company_name or ticker).strip()
    # Strip corporate suffixes -- GDELT matches headline text, which rarely
    # includes them, and their presence collapses recall.
    for suffix in (
        " Inc.", " Inc", " Corporation", " Corp.", " Corp", " Company",
        " Co.", " Ltd.", " Ltd", " plc", " PLC", " Holdings", " Group",
        " N.V.", " S.A.", " LLC", ",",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return f'"{name}" sourcelang:english'


async def fetch_timeline(
    client: APIClient, ticker: str, company_name: str, timespan: str = "90d"
) -> dict[str, Any]:
    """DOC 2.0 timelinevolinfo: article volume + tone over the window."""
    try:
        return await client.get_json(
            "/doc/doc",
            params={
                "query": _query_for(company_name, ticker),
                "mode": "timelinevolinfo",
                "timespan": timespan,
                "format": "json",
            },
        )
    except Exception as exc:  # noqa: BLE001 - GDELT is best-effort
        log.debug("gdelt_timeline_failed", ticker=ticker, error=str(exc))
        return {}


async def fetch_tone_timeline(
    client: APIClient, ticker: str, company_name: str, timespan: str = "30d"
) -> dict[str, Any]:
    try:
        return await client.get_json(
            "/doc/doc",
            params={
                "query": _query_for(company_name, ticker),
                "mode": "timelinetone",
                "timespan": timespan,
                "format": "json",
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("gdelt_tone_failed", ticker=ticker, error=str(exc))
        return {}


async def fetch_articles(
    client: APIClient,
    ticker: str,
    company_name: str,
    *,
    timespan: str = "14d",
    maxrecords: int = 40,
) -> list[dict[str, Any]]:
    """Headlines only. We never send article bodies anywhere near the LLM."""
    try:
        data = await client.get_json(
            "/doc/doc",
            params={
                "query": _query_for(company_name, ticker),
                "mode": "artlist",
                "timespan": timespan,
                "maxrecords": maxrecords,
                "sort": "hybridrel",
                "format": "json",
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("gdelt_artlist_failed", ticker=ticker, error=str(exc))
        return []
    out = []
    for a in (data or {}).get("articles") or []:
        out.append(
            {
                "title": (a.get("title") or "")[:300],
                "domain": a.get("domain"),
                "url": a.get("url"),
                "seendate": a.get("seendate"),
                "tone": _safe_float(a.get("tone")),
            }
        )
    return out


async def fetch_theme_hits(
    client: APIClient,
    ticker: str,
    company_name: str,
    themes: Iterable[str] = TRACKED_THEMES,
    *,
    timespan: str = "30d",
) -> dict[str, dict[str, int]]:
    """Per-theme article counts, split into company-level vs contextual.

    `company` = the company name appears in the headline alongside the theme.
    `context` = the theme fires on articles that merely co-mention the company.
    That split is what separates "this company laid off staff" (margin story)
    from "the sector is laying off" (demand story).
    """
    out: dict[str, dict[str, int]] = {}
    name = (company_name or ticker).strip()
    for theme in themes:
        try:
            data = await client.get_json(
                "/doc/doc",
                params={
                    "query": f'{_query_for(company_name, ticker)} theme:{theme}',
                    "mode": "artlist",
                    "timespan": timespan,
                    "maxrecords": 25,
                    "format": "json",
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("gdelt_theme_failed", ticker=ticker, theme=theme, error=str(exc))
            continue
        articles = (data or {}).get("articles") or []
        if not articles:
            continue
        company_hits = sum(
            1 for a in articles if _headline_names_company(a.get("title"), name)
        )
        out[THEME_MAP.get(theme, theme.lower())] = {
            "total": len(articles),
            "company": company_hits,
            "context": len(articles) - company_hits,
        }
    return out


def _headline_names_company(title: str | None, name: str) -> bool:
    if not title or not name:
        return False
    head = title.lower()
    # First token of the company name is the discriminating one ("Ford" in
    # "Ford Motor Company"). Requiring the full string is too strict for
    # headlines, requiring any token is too loose.
    first = name.split()[0].lower()
    return len(first) > 2 and first in head


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Aggregation -- turns raw timelines into the four numbers Stage 3 scores on
# ---------------------------------------------------------------------------
def _timeline_points(payload: dict[str, Any]) -> list[tuple[str, float]]:
    series = (payload or {}).get("timeline") or []
    if not series:
        return []
    data = series[0].get("data") or []
    out = []
    for p in data:
        v = _safe_float(p.get("value"))
        if v is None:
            continue
        out.append((str(p.get("date")), v))
    return out


def summarise_news(
    volume_payload: dict[str, Any],
    tone_payload: dict[str, Any],
    articles: list[dict[str, Any]],
    themes: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Collapse GDELT output into: volume z, tone slope, source diversity.

    Volume z is against the ticker's OWN 90-day baseline, not a cross-sectional
    one -- a spike is an information event relative to how much this company is
    normally written about.
    """
    vol = [v for _, v in _timeline_points(volume_payload)]
    volume_z = None
    if len(vol) >= 10:
        baseline, recent = vol[:-3], vol[-3:]
        mu = statistics.fmean(baseline)
        sd = statistics.pstdev(baseline)
        if sd > 0:
            volume_z = (statistics.fmean(recent) - mu) / sd

    tone_pts = _timeline_points(tone_payload)
    tone_vals = [v for _, v in tone_pts]
    tone_avg = statistics.fmean(tone_vals) if tone_vals else None

    # 7-day slope, not level. A company at tone -2 and improving beats one at
    # +1 and deteriorating.
    tone_slope = None
    tail = tone_vals[-7:]
    if len(tail) >= 4:
        x = np.arange(len(tail), dtype=float)
        y = np.asarray(tail, dtype=float)
        tone_slope = float(np.polyfit(x, y, 1)[0])

    domains = [a.get("domain") for a in articles if a.get("domain")]
    diversity = (len(set(domains)) / len(domains)) if domains else None

    return {
        "volume_z": volume_z,
        "tone_avg": tone_avg,
        "tone_slope_7d": tone_slope,
        "source_diversity": diversity,
        "article_count": len(articles),
        "themes": themes,
        "headlines": [
            {"title": a["title"], "tone": a.get("tone"), "domain": a.get("domain")}
            for a in sorted(
                articles, key=lambda a: abs(a.get("tone") or 0), reverse=True
            )[:5]
        ],
    }


async def fetch_news_bundle(
    client: APIClient, ticker: str, company_name: str, as_of: dt.date
) -> dict[str, Any]:
    """Everything Stage 3 needs for one ticker, in four cached calls."""
    volume = await fetch_timeline(client, ticker, company_name, "90d")
    tone = await fetch_tone_timeline(client, ticker, company_name, "30d")
    articles = await fetch_articles(client, ticker, company_name)
    themes = await fetch_theme_hits(client, ticker, company_name)
    summary = summarise_news(volume, tone, articles, themes)
    summary.update({"ticker": ticker, "as_of_date": as_of})
    return summary
