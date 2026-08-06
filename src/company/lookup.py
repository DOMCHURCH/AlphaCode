"""Turning what someone typed into a company.

Two things are being resolved, and the order between them is the whole design:

  1. An exact ticker wins outright. "AAL" is American Airlines. There is a real
     company called Aalberts and a dozen names containing "aal", and none of
     that matters -- somebody typing a ticker means the ticker.
  2. Otherwise it is a name, matched case-insensitively on any part.

Names come from `universe.name`, which is SEC's own company title. Nothing new
is fetched here: `sec_edgar.fetch_company_tickers()` already returns the title
alongside the CIK, and the fundamentals backfill already calls it on every
load -- it just keeps the CIK map and drops the names. See
`/admin` -> "Company names" for whether they are actually stored.

Only companies with filed fundamentals are offered. A name match that leads to
"nothing to draw" sends a reader from one empty page to another, which is the
thing every empty state in this project is built to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

# A list nobody scrolls is not a list. Beyond this the answer is "be more
# specific", not five more rows.
MAX_MATCHES = 12
# Below this a name search matches half the market. "in" is not a query.
MIN_QUERY_CHARS = 2


@dataclass(frozen=True)
class Match:
    ticker: str
    name: str | None
    sector: str | None


@dataclass(frozen=True)
class Resolution:
    """kind:
    ticker  -- an exact ticker; go straight there
    one     -- exactly one company matched the name; go straight there
    many    -- several matched; let the reader pick
    none    -- nothing matched
    no_names -- names are not loaded, so a name search cannot be answered
    """

    kind: Literal["ticker", "one", "many", "none", "no_names"]
    ticker: str | None = None
    matches: tuple[Match, ...] = ()


def _known_tickers(candidates: tuple[str, ...]) -> set[str]:
    """Which of these tickers this site has anything to draw for."""
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        rows = s.execute(
            select(Fundamental.ticker)
            .where(Fundamental.ticker.in_(candidates))
            .distinct()
        ).all()
    return {t for (t,) in rows}


def names_loaded() -> int:
    """How many tickers carry a company name. 0 means name search is off."""
    from sqlalchemy import distinct, func, select

    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    try:
        with session_scope() as s:
            return int(
                s.execute(
                    select(func.count(distinct(UniverseSnapshot.ticker))).where(
                        UniverseSnapshot.name.isnot(None),
                        UniverseSnapshot.name != "",
                    )
                ).scalar_one()
                or 0
            )
    except Exception as exc:  # noqa: BLE001 - search degrades, never 500s
        log.warning("names_loaded_failed", error=str(exc)[:200])
        return 0


def _search_names(query: str) -> list[Match]:
    """Companies whose name contains `query`, drawable ones only.

    One row per ticker: the universe is snapshotted daily, so an unfiltered
    LIKE returns the same company once per day it has existed.
    """
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    needle = f"%{query.lower()}%"
    with session_scope() as s:
        rows = s.execute(
            select(
                UniverseSnapshot.ticker,
                func.max(UniverseSnapshot.name),
                func.max(SectorMap.sector),
            )
            .join(Fundamental, Fundamental.ticker == UniverseSnapshot.ticker)
            .outerjoin(SectorMap, SectorMap.ticker == UniverseSnapshot.ticker)
            .where(
                func.lower(UniverseSnapshot.name).like(needle),
                Fundamental.metric == "total_assets",
                Fundamental.value > 0,
            )
            .group_by(UniverseSnapshot.ticker)
            .limit(200)
        ).all()
    return [Match(t, n, sec) for t, n, sec in rows]


# Legal form, not identity. "Walmart Inc." and "Walmart" are the same answer
# to somebody typing "walmart", and dropping these is what lets that query
# through without stopping to ask about Walmart de Mexico.
_SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co",
    "limited", "ltd", "plc", "llc", "lp", "nv", "sa", "ag",
    "holdings", "holding", "group", "the",
)


def _bare(name: str) -> str:
    """A company name with its legal-form words and punctuation removed."""
    words = [w for w in "".join(
        c if c.isalnum() or c.isspace() else " " for c in name.lower()
    ).split() if w]
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] in _SUFFIXES:
        words.pop(0)
    return " ".join(words)


def _rank(matches: list[Match], query: str) -> list[Match]:
    """Best first: exact name, exact once legal form is dropped, then
    starts-with, then contains anywhere.

    Within a tier, shorter names first -- "WALMART INC" before "WALMART DE
    MEXICO SAB DE CV", because the shorter one is almost always the company
    somebody typing "walmart" meant.
    """
    q = query.lower().strip()
    qb = _bare(q)

    def tier(m: Match) -> int:
        name = (m.name or "").lower()
        if name == q:
            return 0
        if qb and _bare(name) == qb:
            return 1
        if name.startswith(q):
            return 2
        return 3

    return sorted(matches, key=lambda m: (tier(m), len(m.name or ""), m.ticker))


def _best_tier(matches: list[Match], query: str) -> list[Match]:
    """The matches sharing the strongest tier, in ranked order."""
    q = query.lower().strip()
    qb = _bare(q)

    def tier(m: Match) -> int:
        name = (m.name or "").lower()
        if name == q:
            return 0
        if qb and _bare(name) == qb:
            return 1
        if name.startswith(q):
            return 2
        return 3

    if not matches:
        return []
    top = min(tier(m) for m in matches)
    return [m for m in matches if tier(m) == top]


def resolve(query: str) -> Resolution:
    """What the reader meant, as far as the data can say."""
    raw = query.strip()
    if not raw:
        return Resolution("none")

    # 1. An exact ticker, whatever else it might spell.
    symbol = raw.lstrip("$").strip().upper()
    if symbol and len(symbol) <= 16 and _known_tickers((symbol,)):
        return Resolution("ticker", ticker=symbol)

    if len(raw) < MIN_QUERY_CHARS:
        return Resolution("none")

    # 2. A name. If none are stored, say that rather than "no match" -- the two
    #    have completely different fixes and only one is the reader's problem.
    if names_loaded() == 0:
        return Resolution("no_names")

    try:
        found = _rank(_search_names(raw), raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("name_search_failed", query=raw[:40], error=str(exc)[:200])
        return Resolution("none")

    if not found:
        return Resolution("none")
    if len(found) == 1:
        return Resolution("one", ticker=found[0].ticker, matches=tuple(found))

    # One clear winner in the best tier still goes straight through: "walmart"
    # should not stop to ask when only one company is actually called that,
    # even with Walmart de Mexico in the same result set.
    best = _best_tier(found, raw)
    if len(best) == 1:
        return Resolution("one", ticker=best[0].ticker, matches=tuple(found))

    return Resolution("many", matches=tuple(found[:MAX_MATCHES]))
