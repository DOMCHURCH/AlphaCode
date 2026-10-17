"""The frames loader must clear the same bar as the bulk loader.

Frames and the bulk datasets both write `source="sec"` onto the same natural
key, so a row either path stores is indistinguishable afterwards. The bulk path
ran `validate_facts` (positive total assets, no component above the total,
identity drift and scale-jump flags); the frames path did not, and the live
table holds `total_assets = 0` rows for ten small filers.

No network: `fetch_frame` is replaced with canned frame payloads.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

PERIOD = "2026-06-30"
FILED = dt.date(2026, 8, 5)

TAGS = {
    "total_assets": "Assets",
    "total_liabilities": "Liabilities",
    "total_equity": "StockholdersEquity",
    "cash": "CashAndCashEquivalentsAtCarryingValue",
}


def _company(n: int) -> tuple[str, int, str]:
    return f"T{n:03d}", 1000 + n, f"0000000000-26-{n:06d}"


def _run(companies, monkeypatch):
    """Sweep one quarter over `companies`: {n: {metric: value}}."""
    from src.ingest import sec_frames

    by_tag: dict[str, list[dict]] = {}
    index: dict[str, dict] = {}
    cik_to_ticker: dict[str, str] = {}
    for n, metrics in companies.items():
        ticker, cik, accn = _company(n)
        index[accn] = {"cik": cik, "filed": FILED}
        cik_to_ticker[str(cik)] = ticker
        for metric, value in metrics.items():
            by_tag.setdefault(TAGS[metric], []).append(
                {"accn": accn, "cik": cik, "end": PERIOD, "val": value}
            )

    async def fake_fetch(tag, uom, ccp, **kw):
        return by_tag.get(tag)

    monkeypatch.setattr(sec_frames, "fetch_frame", fake_fetch)
    return asyncio.run(sec_frames.sweep_quarter(2026, 2, index, cik_to_ticker))


def _balanced(assets=1000.0, **extra):
    return {"total_assets": assets, "total_liabilities": assets * 0.6,
            "total_equity": assets * 0.4, **extra}


def _by_ticker(rows):
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["ticker"], {})[r["metric"]] = r["value"]
    return out


def test_a_zero_asset_period_is_not_stored(monkeypatch):
    rows, report = _run(
        {1: _balanced(), 2: {"total_assets": 0.0, "total_liabilities": 0.0,
                             "total_equity": 0.0}},
        monkeypatch,
    )
    stored = _by_ticker(rows)
    assert "T001" in stored
    assert "T002" not in stored, "a company-period with total assets of 0 was kept"
    assert report["rejected_periods"] == 1
    assert report["rows_rejected"] == 3


def test_a_component_larger_than_total_assets_is_dropped_but_the_period_stays(monkeypatch):
    rows, report = _run(
        {1: _balanced(cash=5000.0)},  # cash 5000 > assets 1000
        monkeypatch,
    )
    stored = _by_ticker(rows)["T001"]
    assert "cash" not in stored
    assert stored["total_assets"] == 1000.0
    assert report["rows_rejected"] == 1


def test_a_balance_sheet_that_does_not_reconcile_is_flagged_and_kept(monkeypatch):
    sheet = {"total_assets": 1000.0, "total_liabilities": 600.0, "total_equity": 300.0}
    rows, report = _run({1: sheet}, monkeypatch)
    assert _by_ticker(rows)["T001"]["total_equity"] == 300.0
    assert report["validation_flags"] == 1
    assert report["rejected_periods"] == 0


def test_a_clean_sweep_keeps_everything_and_flags_nothing(monkeypatch):
    rows, report = _run({1: _balanced(), 2: _balanced(2000.0)}, monkeypatch)
    assert len(rows) == 6
    assert report["rows_kept"] == 6
    assert report["rows_rejected"] == 0
    assert report["validation_flags"] == 0


def test_a_mostly_invalid_quarter_refuses_to_write_anything(monkeypatch):
    """The bulk loader's stance: past the rejection-rate limit the PARSE is
    wrong, so nothing from the quarter is written."""
    from src.ingest.xbrl import ExtractionError

    bad = {"total_assets": 0.0, "total_liabilities": 0.0, "total_equity": 0.0}
    companies = {n: (bad if n % 3 == 0 else _balanced()) for n in range(1, 61)}
    with pytest.raises(ExtractionError):
        _run(companies, monkeypatch)
