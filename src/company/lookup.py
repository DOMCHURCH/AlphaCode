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

import time
from dataclasses import dataclass
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

# A list nobody scrolls is not a list. Beyond this the answer is "be more
# specific", not five more rows.
MAX_MATCHES = 12
# Below this a name search matches half the market. "in" is not a query.
MIN_QUERY_CHARS = 2
# Similarity a misspelling has to reach before it is worth offering. High
# enough that "walmart"/"walgreens" (0.62) is not proposed as a typo, low
# enough to catch a dropped or swapped letter.
FUZZY_THRESHOLD = 0.78
# Under four characters everything is close to everything.
MIN_FUZZY_CHARS = 4
_NAMES_TTL_S = 1800.0

_named_cache: list[Match] | None = None
_named_cache_at: float = 0.0


def reset_cache() -> None:
    """Forget the cached name list.

    Needed because the cache is module-level and outlives a database: tests
    swapping DBs, and a process that has just reloaded fundamentals, would
    otherwise both be answering from a list that no longer describes what is
    stored.
    """
    global _named_cache, _named_cache_at
    _named_cache, _named_cache_at = None, 0.0


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
    fuzzy   -- nothing matched, but these are close. NEVER redirected to.
    none    -- nothing matched
    no_names -- names are not loaded, so a name search cannot be answered
    """

    kind: Literal["ticker", "one", "many", "fuzzy", "none", "no_names"]
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


def _all_named() -> list[Match]:
    """Every drawable company that has a name. Cached: this is the haystack a
    misspelling is compared against, and it changes only on a reload."""
    global _named_cache, _named_cache_at
    if _named_cache is not None and (time.monotonic() - _named_cache_at) < _NAMES_TTL_S:
        return _named_cache

    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

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
                UniverseSnapshot.name.isnot(None),
                UniverseSnapshot.name != "",
                Fundamental.metric == "total_assets",
                Fundamental.value > 0,
            )
            .group_by(UniverseSnapshot.ticker)
        ).all()
    _named_cache = [Match(t, n, sec) for t, n, sec in rows]
    _named_cache_at = time.monotonic()
    return _named_cache


def _search_names_normalised(query: str) -> list[Match]:
    """Substring match ignoring punctuation and legal form.

    SQL LIKE compares the stored name literally, so "freeport mcmoran" misses
    "FREEPORT-MCMORAN INC" over one hyphen. That is an exact match as far as
    the reader is concerned, and answering it with "did you mean" would be
    wrong twice: it did match, and the site would be asking about a name it
    holds.
    """
    q = _bare(query.lower().strip())
    if not q:
        return []
    try:
        return [m for m in _all_named() if q in _bare((m.name or "").lower())]
    except Exception as exc:  # noqa: BLE001
        log.warning("normalised_search_failed", error=str(exc)[:200])
        return []


def close_matches(query: str, limit: int = 6) -> list[Match]:
    """Companies whose name is nearly what was typed.

    Only reached when nothing matched exactly, so the cost lands on the miss
    rather than on every search. Compared against the name with its legal form
    stripped, so "walmrt" is measured against "walmart" rather than against
    "walmart inc" -- the suffix would otherwise dilute the ratio and push a
    real typo below the threshold.

    difflib is stdlib and its ratio short-circuits hard on length, so a few
    thousand candidates is a handful of milliseconds.
    """
    import difflib

    q = _bare(query.lower().strip())
    if len(q) < MIN_FUZZY_CHARS:
        return []
    try:
        candidates = _all_named()
    except Exception as exc:  # noqa: BLE001 - a miss stays a miss
        log.warning("close_matches_failed", error=str(exc)[:200])
        return []

    scored: list[tuple[float, Match]] = []
    for m in candidates:
        bare = _bare((m.name or "").lower())
        if not bare:
            continue
        ratio = difflib.SequenceMatcher(None, q, bare).ratio()
        if ratio < FUZZY_THRESHOLD:
            # A typo in one word of a longer name -- "walmrt de mexico" -- can
            # score low overall while matching a word almost exactly.
            first = bare.split()[0]
            ratio = max(ratio, difflib.SequenceMatcher(None, q, first).ratio())
        if ratio >= FUZZY_THRESHOLD:
            scored.append((ratio, m))

    scored.sort(key=lambda p: (-p[0], len(p[1].name or ""), p[1].ticker))
    return [m for _r, m in scored[:limit]]


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
        # Literal substring first -- it is the common case and the database
        # does the work. Only if that finds nothing is the punctuation- and
        # suffix-insensitive pass worth the scan.
        hits = _search_names(raw) or _search_names_normalised(raw)
        found = _rank(hits, raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("name_search_failed", query=raw[:40], error=str(exc)[:200])
        return Resolution("none")

    if not found:
        near = close_matches(raw)
        # Offered, never followed. A misspelling resolved automatically would
        # put a company on screen that the reader never asked for, under a
        # heading that reads as the site asserting it is the right one.
        return Resolution("fuzzy", matches=tuple(near)) if near else Resolution("none")
    if len(found) == 1:
        return Resolution("one", ticker=found[0].ticker, matches=tuple(found))

    # One clear winner in the best tier still goes straight through: "walmart"
    # should not stop to ask when only one company is actually called that,
    # even with Walmart de Mexico in the same result set.
    best = _best_tier(found, raw)
    if len(best) == 1:
        return Resolution("one", ticker=best[0].ticker, matches=tuple(found))

    return Resolution("many", matches=tuple(found[:MAX_MATCHES]))
