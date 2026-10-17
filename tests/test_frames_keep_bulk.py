"""The frames sweep must fill gaps, not overwrite what the bulk datasets loaded.

`backfill_filings` re-runs every few hours (scheduler job `filings`, or
`POST /backfill?kind=filings`) over the last two calendar quarters' filings, and
the bulk quarter for those same filings is usually already loaded. Both routes
write `source="sec"`, so a frames fact for a filing the bulk path holds lands on
the SAME natural key, and `ON CONFLICT DO UPDATE` used to replace the bulk row's
value and tag with the frames figure while leaving its `fiscal_period` alone.

Two things are locked down:

(a) the sweep leaves a bulk row (a row with `fiscal_period` set) as it is;
(b) a frames row never carries a stale label: it states `fiscal_period=None`, so
    even a write that is not protected clears the label instead of keeping one
    that describes another figure.

Every test here is written to fail on the code before the patch.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

from src.storage import repository
from src.storage.models import Fundamental

FILED = dt.date(2026, 8, 6)
PERIOD = dt.date(2026, 6, 30)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'k.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    repository.reset_write_ledger()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()
        repository.reset_write_ledger()


def _bulk(value: float, tag: str = "Revenues", fp: str | None = "FY",
          metric: str = "revenue") -> dict:
    """A row the way xbrl.extract_facts writes it: fiscal_period is set."""
    return {
        "ticker": "UFI", "metric": metric, "value": value, "period_end": PERIOD,
        "fiscal_period": fp, "filing_date": FILED, "source": "sec",
        "restated": False, "source_tag": tag,
    }


def _frames_via_join(value: float, tag: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
                     metric: str = "revenue", ticker: str = "UFI") -> dict:
    """A row the way the frames route builds it: through join_filing_dates."""
    from src.ingest import sec_frames

    raw = sec_frames.rows_from_frame(
        [{"cik": 100, "accn": "0000100-26-000001", "end": PERIOD.isoformat(), "val": value}],
        metric, 0, tag,
    )
    index = {"0000100-26-000001": {"cik": 100, "filed": FILED, "form": "10-K"}}
    rows, dropped = sec_frames.join_filing_dates(raw, index, {"100": ticker})
    assert not any(dropped.values()), dropped
    return sec_frames.collapse_alias_rows(rows)[0]


def _stored(metric: str = "revenue") -> Fundamental:
    from src.storage.db import session_scope

    with session_scope() as s:
        return s.execute(
            select(Fundamental).where(Fundamental.ticker == "UFI", Fundamental.metric == metric)
        ).scalar_one()


def _seed(*rows: dict) -> None:
    from src.storage.db import session_scope

    with session_scope() as s:
        repository.save_fundamentals(s, list(rows))


def _run_filings_sweep(monkeypatch, frames_rows: list[dict]) -> int:
    """Run the real `backfill_filings` with only the network edges replaced."""
    from src import backfill
    from src.ingest import sec_frames

    async def fake_index(year, quarter, **_):
        return {"0000100-26-000001": {"cik": 100, "filed": FILED, "form": "10-K"}}

    async def fake_sweep(year, quarter, index, cik_to_ticker):
        # The same rows for both quarters is fine: the second pass is a no-op by key.
        rows = [dict(r) for r in frames_rows] if (year, quarter) == (2026, 2) else []
        return rows, {"quarter": f"{year}q{quarter}", "rows_kept": len(rows)}

    async def fake_map():
        return {"100": "UFI"}

    monkeypatch.setattr(sec_frames, "fetch_form_index", fake_index)
    monkeypatch.setattr(sec_frames, "sweep_quarter", fake_sweep)
    monkeypatch.setattr(backfill, "_cik_to_ticker", fake_map)
    return asyncio.run(backfill.backfill_filings(as_of=dt.date(2026, 10, 17), quarters=2))


# ---------------------------------------------------------------------------
# (a) the sweep does not overwrite a bulk row
# ---------------------------------------------------------------------------
def test_the_filings_sweep_leaves_a_bulk_row_alone(db, monkeypatch):
    """The annual figure (FY, 1,000) is on the key; the frames route reports the
    fourth QUARTER (250) for the same period and filing. It used to win."""
    _seed(_bulk(1000.0, tag="Revenues"))

    _run_filings_sweep(monkeypatch, [_frames_via_join(250.0)])

    row = _stored()
    assert row.value == 1000.0
    assert row.source_tag == "Revenues"
    assert row.fiscal_period == "FY"


def test_the_filings_sweep_still_inserts_what_the_bulk_path_does_not_have(db, monkeypatch):
    """The point of the route: a filing the bulk dataset has not published yet."""
    _seed(_bulk(1000.0, metric="revenue"))

    _run_filings_sweep(monkeypatch, [
        _frames_via_join(250.0, metric="revenue"),       # shadowed by the bulk row
        _frames_via_join(77.0, metric="net_income", tag="NetIncomeLoss"),  # new
    ])

    assert _stored("revenue").value == 1000.0
    new = _stored("net_income")
    assert new.value == 77.0
    assert new.fiscal_period is None


def test_the_filings_sweep_still_refreshes_its_own_rows(db, monkeypatch):
    """A frames row is not protected from a later frames sweep: a corrected
    figure from SEC must still land."""
    _run_filings_sweep(monkeypatch, [_frames_via_join(250.0)])
    assert _stored().value == 250.0

    _run_filings_sweep(monkeypatch, [_frames_via_join(260.0)])
    assert _stored().value == 260.0


def test_a_bulk_load_still_replaces_a_frames_row(db, monkeypatch):
    """The other direction is unchanged: bulk is the authoritative record."""
    _run_filings_sweep(monkeypatch, [_frames_via_join(250.0)])

    _seed(_bulk(1000.0))

    row = _stored()
    assert row.value == 1000.0
    assert row.fiscal_period == "FY"


def test_known_limit_a_bulk_row_with_a_blank_fp_is_treated_as_a_frames_row(db, monkeypatch):
    """Documents the one case the test cannot see. SEC publishes about 1 in
    11,000 periodic filings with a blank `fp`; stored, those bulk rows look
    exactly like frames rows. If this ever starts failing, the guard got better
    and the docstring on save_fundamentals(keep_bulk=...) should say so."""
    _seed(_bulk(1000.0, fp=None))

    _run_filings_sweep(monkeypatch, [_frames_via_join(250.0)])

    assert _stored().value == 250.0


# ---------------------------------------------------------------------------
# (b) a frames row never carries a stale label
# ---------------------------------------------------------------------------
def test_a_frames_row_states_that_it_has_no_fiscal_period():
    row = _frames_via_join(250.0)

    assert "fiscal_period" in row
    assert row["fiscal_period"] is None


def test_an_unprotected_frames_write_clears_the_label_it_lands_on(db):
    """Without keep_bulk (any caller that forgets it, or a future route): the
    value and tag come from frames, so the label must not stay 'FY'."""
    _seed(_bulk(1000.0, fp="FY"))

    from src.storage.db import session_scope

    with session_scope() as s:
        repository.save_fundamentals(s, [_frames_via_join(250.0)])

    row = _stored()
    assert row.value == 250.0
    assert row.fiscal_period is None


def test_keep_bulk_reports_what_it_kept(db):
    """The kept rows are logged, so the log says how many the guard refused."""
    from structlog.testing import capture_logs

    from src.storage.db import session_scope

    _seed(_bulk(1000.0))
    with capture_logs() as logs:
        with session_scope() as s:
            repository.save_fundamentals(s, [_frames_via_join(250.0)], keep_bulk=True)

    kept = [e for e in logs if e.get("event") == "upsert_kept_existing"]
    assert kept and kept[0]["kept"] == 1 and kept[0]["of"] == 1
