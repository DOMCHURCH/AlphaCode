"""Reading and writing `company_page_extras`.

Split from `page_extras.py` on purpose: that module is pure functions over
figures and can be tested without a database, and this one is the only place
that talks to one. The split is what makes "how many queries does a page
render cost?" a question with an answer you can read rather than trace.

    READ  `load_extras()`      one primary-key read, on the hot path
    WRITE `compute_and_store()` several reads, never on the hot path

The write path is run by `scripts/backfill_page_extras.py` and by the ingest
when a ticker's fundamentals are reloaded.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog

from src.report.page_extras import (
    FILING_COUNT,
    PEER_COUNT,
    PERIODIC_FORMS,
    build_company_ld,
    build_filings_html,
    build_intro,
    build_peers_html,
    filing_url,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# The read path: the whole of this feature's per-render cost
# ---------------------------------------------------------------------------
def load_extras(ticker: str) -> dict[str, str] | None:
    """One primary-key read. That is the entire hot-path cost.

    Returns None when there is no row, and the page then renders exactly as it
    did before this module existed. That is why every fragment lives in ONE
    row: a missing row is a missing section rather than a half-built page, and
    adding a fifth fragment later cannot turn one query into two.
    """
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import CompanyPageExtras

    try:
        with session_scope() as session:
            row = session.execute(
                select(
                    CompanyPageExtras.intro_html,
                    CompanyPageExtras.peers_html,
                    CompanyPageExtras.filings_html,
                    CompanyPageExtras.jsonld,
                ).where(CompanyPageExtras.ticker == ticker.upper())
            ).first()
    except Exception as exc:  # noqa: BLE001 - decoration must never take the page
        log.warning("page_extras_read_failed", ticker=ticker, error=str(exc)[:200])
        return None
    if row is None:
        return None
    return {
        "intro": row[0] or "",
        "peers": row[1] or "",
        "filings": row[2] or "",
        "jsonld": row[3] or "",
    }


# ---------------------------------------------------------------------------
# The write path
# ---------------------------------------------------------------------------
def _prior_assets(
    session: Any, ticker: str, before: dt.date
) -> tuple[float | None, dt.date | None]:
    """Total assets at the newest period STRICTLY before `before`.

    This is the read that made the year-on-year sentence too expensive to do
    per request, and it is also what makes that sentence worth having: a
    balance sheet with no previous one beside it is a photograph, not a trend.
    """
    from sqlalchemy import select

    from src.storage.models import Fundamental

    rows = session.execute(
        select(Fundamental.period_end, Fundamental.value, Fundamental.filing_date)
        .where(Fundamental.ticker == ticker)
        .where(Fundamental.metric == "total_assets")
        .where(Fundamental.period_end < before)
        .order_by(Fundamental.period_end.desc())
    ).all()
    if not rows:
        return None, None
    newest = rows[0][0]
    # Latest filing wins for that period, matching every other read path.
    best: tuple[dt.date, float] | None = None
    for period_end, value, filed in rows:
        if period_end != newest or value is None:
            continue
        if best is None or filed > best[0]:
            best = (filed, float(value))
    return (best[1] if best else None), newest


def _peers(
    session: Any, ticker: str, sector: str | None, assets: float | None
) -> tuple[list[dict[str, Any]], int | None, int | None]:
    """(peers, rank, sector_total). Same sector, nearest in size.

    The rank falls out of the same read the peer list needs, so the
    sentence it feeds costs nothing beyond what peers already cost.

    Nearest by assets rather than the four largest, because the four largest
    are the same four on every page in the sector -- a list that never changes
    is a navigation element pretending to be a comparison.
    """
    from sqlalchemy import func, select

    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    if not sector:
        return [], None, None
    candidates = [
        t
        for (t,) in session.execute(
            select(SectorMap.ticker)
            .where(SectorMap.sector == sector)
            .where(SectorMap.ticker != ticker)
        ).all()
    ]
    if not candidates:
        return [], None, None

    latest = dict(
        session.execute(
            select(Fundamental.ticker, func.max(Fundamental.period_end))
            .where(Fundamental.ticker.in_(candidates))
            .where(Fundamental.metric == "total_assets")
            .group_by(Fundamental.ticker)
        ).all()
    )
    if not latest:
        return [], None, None
    rows = session.execute(
        select(Fundamental.ticker, Fundamental.period_end, Fundamental.value)
        .where(Fundamental.ticker.in_(list(latest)))
        .where(Fundamental.metric == "total_assets")
    ).all()
    sized: dict[str, float] = {}
    for t, period_end, value in rows:
        if value is not None and latest.get(t) == period_end:
            sized[t] = float(value)
    if not sized:
        return [], None, None

    names = dict(
        session.execute(
            select(UniverseSnapshot.ticker, UniverseSnapshot.name)
            .where(UniverseSnapshot.ticker.in_(list(sized)))
        ).all()
    )
    if assets:
        ordered = sorted(sized, key=lambda t: abs(sized[t] - assets))
    else:
        ordered = sorted(sized, key=lambda t: -sized[t])
    peers = [
        {"ticker": t, "name": names.get(t), "assets": sized[t]}
        for t in ordered[:PEER_COUNT]
    ]
    # Rank counts this ticker too, hence the +1 on both sides: `sized` holds
    # every OTHER company in the sector that has a total-assets figure.
    rank = (
        1 + sum(1 for v in sized.values() if v > assets) if assets else None
    )
    return peers, rank, len(sized) + 1


def _filings(session: Any, ticker: str) -> list[dict[str, Any]]:
    """The last few periodic filings, newest first."""
    from sqlalchemy import select

    from src.storage.models import FilingEvent

    rows = session.execute(
        select(
            FilingEvent.form,
            FilingEvent.filing_date,
            FilingEvent.cik,
            FilingEvent.accession,
            FilingEvent.primary_doc,
        )
        .where(FilingEvent.ticker == ticker)
        .where(FilingEvent.form.in_(PERIODIC_FORMS))
        .order_by(FilingEvent.filing_date.desc())
        .limit(FILING_COUNT)
    ).all()
    return [
        {
            "form": form,
            "filing_date": filing_date.isoformat(),
            "url": filing_url(cik, accession, primary_doc),
        }
        for form, filing_date, cik, accession, primary_doc in rows
    ]


def compute_and_store(ticker: str, origin: str) -> bool:
    """Build every fragment for one ticker and write the single row.

    Returns False where the ticker has no drawable balance sheet: there is
    nothing to introduce, and writing an empty row would add a query to the
    page's read path in exchange for nothing.
    """
    from sqlalchemy import select

    from src.company.view1 import build_view1
    from src.storage.db import session_scope
    from src.storage.models import CompanyPageExtras, FilingEvent

    symbol = ticker.upper()
    view = build_view1(symbol)
    if view is None or view.period_end is None:
        return False

    with session_scope() as session:
        prior, prior_period = _prior_assets(session, symbol, view.period_end)
        peers, sector_rank, sector_total = _peers(
            session, symbol, view.sector, view.total_assets
        )
        filings = _filings(session, symbol)
        form = session.execute(
            select(FilingEvent.form)
            .where(FilingEvent.ticker == symbol)
            .where(FilingEvent.form.in_(PERIODIC_FORMS))
            .order_by(FilingEvent.filing_date.desc())
            .limit(1)
        ).scalar_one_or_none()

        row = session.get(CompanyPageExtras, symbol)
        if row is None:
            row = CompanyPageExtras(ticker=symbol)
            session.add(row)
        d = view.as_dict()
        row.intro_html = build_intro(
            ticker=symbol,
            company_name=view.company_name,
            sector=view.sector,
            asset_lines=d.get("assets") or [],
            claim_lines=d.get("claims") or [],
            negative_equity=bool(d.get("negative_equity")),
            missing_components=bool(d.get("missing_components")),
            sector_rank=sector_rank,
            sector_total=sector_total,
            assets=view.total_assets,
            liabilities=view.total_liabilities,
            equity=view.total_equity,
            prior_assets=prior,
            prior_period_end=prior_period,
            period_end=view.period_end,
            filing_date=view.filing_date,
            form=form,
            balances=view.balances,
            identity_basis=view.identity_basis,
            imbalance_pct=view.imbalance_pct,
        )
        row.peers_html = build_peers_html(peers)
        row.filings_html = build_filings_html(symbol, filings)
        row.jsonld = build_company_ld(
            ticker=symbol,
            company_name=view.company_name,
            sector=view.sector,
            origin=origin,
            assets=view.total_assets,
            period_end=view.period_end,
        )
        row.source_period_end = view.period_end
        row.computed_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    return True
