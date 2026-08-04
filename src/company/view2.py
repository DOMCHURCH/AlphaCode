"""View 2: company revenue against national GDP, on one scale.

Nobody has intuition for a billion dollars. Everybody has intuition for
countries. Putting a company's annual sales next to the economies nearest it in
size makes the magnitude land in a way no number can.

The comparison is of MAGNITUDE ONLY, and the page says so where the reader will
see it rather than in a footnote. GDP is a country's total output over a year --
everything everyone produced. Revenue is one company's sales. They are measured
in the same unit and are not the same kind of thing, and a company is never
"bigger than" a country. The design keeps the two visually distinct so the
comparison cannot be misread as a ranking.

GDP comes from a static file committed to the repo. It changes once a year and
must never be a runtime dependency of a page render.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_GDP_FILE = Path(__file__).parent / "data" / "world_bank_gdp.json"

# How many countries to show either side of the company.
NEIGHBOURS = 3


@lru_cache(maxsize=1)
def gdp_table() -> dict[str, Any]:
    """The committed World Bank figures, sorted descending. Read once."""
    try:
        data = json.loads(_GDP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("gdp_file_unreadable", error=str(exc)[:200])
        return {"countries": [], "year": None, "source": None}
    data["countries"] = sorted(
        data.get("countries", []), key=lambda c: -c.get("gdp_usd", 0)
    )
    return data


@dataclass
class ScaleRow:
    label: str
    value: float
    kind: str          # country | company
    pct_of_max: float


@dataclass
class View2:
    ticker: str
    company_name: str | None
    revenue: float
    period_basis: str
    period_end: dt.date
    gdp_year: int | None
    gdp_source: str | None
    rows: list[ScaleRow] = field(default_factory=list)
    rank_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "revenue": self.revenue,
            "period_basis": self.period_basis,
            "period_end": self.period_end.isoformat(),
            "gdp_year": self.gdp_year,
            "gdp_source": self.gdp_source,
            "rank_note": self.rank_note,
            "rows": [
                {"label": r.label, "value": r.value, "kind": r.kind,
                 "pct_of_max": round(r.pct_of_max, 3)}
                for r in self.rows
            ],
        }


def build_view2(
    ticker: str, revenue: float, period_basis: str, period_end: dt.date,
    company_name: str | None = None,
) -> View2 | None:
    """Place the company among the economies nearest it in size."""
    if not revenue or revenue <= 0:
        return None
    table = gdp_table()
    countries = table.get("countries") or []
    if not countries:
        return None

    # Countries immediately above and below, by size.
    above = [c for c in countries if c["gdp_usd"] >= revenue]
    below = [c for c in countries if c["gdp_usd"] < revenue]
    near = above[-NEIGHBOURS:] + below[:NEIGHBOURS]
    if not near:
        return None

    view = View2(
        ticker=ticker.upper(),
        company_name=company_name,
        revenue=revenue,
        period_basis=period_basis,
        period_end=period_end,
        gdp_year=table.get("year"),
        gdp_source=table.get("source"),
    )

    entries = [(c["name"], float(c["gdp_usd"]), "country") for c in near]
    entries.append((company_name or ticker.upper(), revenue, "company"))
    entries.sort(key=lambda e: -e[1])
    biggest = entries[0][1]

    view.rows = [
        ScaleRow(label=n, value=v, kind=k, pct_of_max=v / biggest * 100.0)
        for n, v, k in entries
    ]

    n_above = len(above)
    if n_above == 0:
        view.rank_note = (
            f"Its annual sales are larger than every economy in this table."
        )
    else:
        view.rank_note = (
            f"Its annual sales sit between {above[-1]['name']} and "
            f"{below[0]['name']}." if below else
            f"Its annual sales sit just below {above[-1]['name']}."
        )
    return view
