"""Where a balance sheet came from: the filing, as a link (Business plan).

Filing-level, not fact-level, and said so in the payload. `fundamentals` keeps
the filing DATE of every figure but not its accession number, so the link is
found by matching that date against `filing_events` for the same company,
preferring the periodic report (10-K / 10-Q) filed that day. A figure whose
filing cannot be matched gets `matched: false` rather than a guessed link.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from src.storage.db import session_scope

_PERIODIC = ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F")


def for_filing(ticker: str, filing_date: dt.date | str | None) -> dict[str, Any]:
    from src.report.page_extras import filing_url
    from src.storage.models import FilingEvent

    if isinstance(filing_date, str):
        filing_date = dt.date.fromisoformat(filing_date)
    base: dict[str, Any] = {
        "level": "filing",
        "filing_date": filing_date.isoformat() if filing_date else None,
        "matched": False,
    }
    if filing_date is None:
        return base
    with session_scope() as session:
        rows = session.execute(
            select(FilingEvent).where(FilingEvent.ticker == ticker.upper(),
                                      FilingEvent.filing_date == filing_date)
        ).scalars().all()
        if not rows:
            return base
        rows.sort(key=lambda e: (e.form not in _PERIODIC, e.form))
        ev = rows[0]
        return {
            **base,
            "matched": True,
            "form": ev.form,
            "accession": ev.accession,
            "sec_url": filing_url(ev.cik, ev.accession, ev.primary_doc),
            "sec_index": (
                f"https://www.sec.gov/Archives/edgar/data/{(ev.cik or '').lstrip('0')}/"
                f"{ev.accession.replace('-', '')}/" if ev.cik else None
            ),
        }


def attach(sheet: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Add `provenance` to a serialised balance sheet, in place."""
    sheet["provenance"] = for_filing(ticker, sheet.get("filing_date"))
    return sheet
