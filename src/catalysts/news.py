"""Score GDELT news aggregates.

The key encoded distinction: layoffs AT the company are read by the market as
margin expansion (short-term positive, long-term ambiguous). Layoffs across the
sector or economy-wide are a demand signal and a negative. We separate them by
whether the company is the GKG-identified entity or merely co-mentioned --
gdelt.fetch_theme_hits does the splitting, this module scores it.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.config.factor_weights import CATALYST_POINTS

log = structlog.get_logger(__name__)

VOLUME_SPIKE_Z = 1.5
DIVERSITY_THRESHOLD = 0.6

# themes whose sign does not depend on who the article is about
_SIMPLE_THEMES = {
    "merger": "theme_merger",
    "bankruptcy": "theme_bankruptcy",
    "strike": "theme_strike",
    "recall": "theme_recall",
    "legal": "theme_legal",
}


def score_news(summary: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score one ticker's news bundle. Returns (points, detail)."""
    if not summary:
        return 0.0, {"themes": []}

    points = 0.0
    notes: list[str] = []

    vol_z = summary.get("volume_z")
    if vol_z is not None and vol_z >= VOLUME_SPIKE_Z:
        # A volume spike is an information event regardless of direction.
        points += CATALYST_POINTS["news_volume_spike"]
        notes.append(f"news volume z={vol_z:.1f}")

    slope = summary.get("tone_slope_7d")
    if slope is not None and slope > 0:
        # Slope, not level: improving-from-negative beats deteriorating-from-positive.
        points += CATALYST_POINTS["news_tone_slope_positive"]
        notes.append(f"tone improving ({slope:+.2f}/d)")

    diversity = summary.get("source_diversity")
    if diversity is not None and diversity >= DIVERSITY_THRESHOLD:
        # 50 articles from 40 outlets is a real story. 50 from 3 is a PR cycle.
        points += CATALYST_POINTS["source_diversity_high"]
        notes.append("broad source coverage")

    themes = summary.get("themes") or {}
    fired: list[str] = []
    for theme, counts in themes.items():
        if not isinstance(counts, dict):
            continue
        company = int(counts.get("company") or 0)
        context = int(counts.get("context") or 0)
        total = int(counts.get("total") or (company + context))
        if total == 0:
            continue

        if theme == "layoff":
            # The distinction that matters.
            if company > context:
                points += CATALYST_POINTS["theme_company_layoff"]
                fired.append("LAYOFF_COMPANY")
                notes.append("company-level layoffs (margin read)")
            else:
                points += CATALYST_POINTS["theme_sector_layoff"]
                fired.append("LAYOFF_SECTOR")
                notes.append("sector/economy layoffs (demand read)")
            continue

        key = _SIMPLE_THEMES.get(theme)
        if key:
            points += CATALYST_POINTS[key]
            fired.append(theme.upper())

    return points, {
        "themes": fired,
        "notes": notes,
        "volume_z": vol_z,
        "tone_avg": summary.get("tone_avg"),
        "tone_slope_7d": slope,
        "source_diversity": diversity,
        "article_count": summary.get("article_count"),
        "headlines": summary.get("headlines") or [],
    }
