"""Test that extract_earnings uses real period ends when available."""

from __future__ import annotations

import datetime as dt
import io
import zipfile

from src.ingest import sec_datasets as ds


def _zip(sub_rows: list[dict], num_rows: list[dict]) -> bytes:
    """Build a synthetic ZIP in SEC bulk dataset format."""
    sub_cols = ["adsh", "cik", "name", "form", "period", "filed", "fp"]
    num_cols = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom",
                "segments", "value"]

    def tsv(cols, rows):
        out = ["\t".join(cols)]
        for r in rows:
            out.append("\t".join(str(r.get(c, "")) for c in cols))
        return "\n".join(out)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub.txt", tsv(sub_cols, sub_rows))
        z.writestr("num.txt", tsv(num_cols, num_rows))
    return buf.getvalue()


# Test data: various companies with real periods different from rounded ones
SUB_REAL_PERIODS = [
    # AAPL +3 days: filed 2026-04-03, period rounded to 2026-03-31, real 2026-03-28
    {"adsh": "aapl_q1", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
     "period": "20260331", "filed": "20260403", "fp": "Q2"},
    # NVDA +4 days: filed 2026-05-28, period rounded to 2026-04-30, real 2026-04-26
    {"adsh": "nvda_q1", "cik": "1045810", "name": "NVIDIA CORP", "form": "10-Q",
     "period": "20260430", "filed": "20260528", "fp": "Q1"},
    # COST -10 days: filed 2026-06-24, period rounded to 2026-05-31, real 2026-06-10
    {"adsh": "cost_q3", "cik": "0000912057", "name": "COSTCO WHOLESALE", "form": "10-Q",
     "period": "20260531", "filed": "20260624", "fp": "Q3"},
    # DE -3 days: filed 2026-07-30, period rounded to 2026-06-30, real 2026-07-03
    {"adsh": "de_fy", "cik": "0000315189", "name": "DEERE & COMPANY", "form": "10-K",
     "period": "20260630", "filed": "20260730", "fp": "FY"},
]

NUM_REAL_PERIODS = [
    # AAPL EPS
    {"adsh": "aapl_q1", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
     "ddate": "20260331", "qtrs": "2", "uom": "USD/shares", "value": "1.95"},
    # NVDA EPS
    {"adsh": "nvda_q1", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
     "ddate": "20260430", "qtrs": "1", "uom": "USD/shares", "value": "0.81"},
    # COST EPS
    {"adsh": "cost_q3", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
     "ddate": "20260531", "qtrs": "3", "uom": "USD/shares", "value": "3.22"},
    # DE EPS
    {"adsh": "de_fy", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
     "ddate": "20260630", "qtrs": "4", "uom": "USD/shares", "value": "28.45"},
]

CIK_MAP = {
    "320193": "AAPL",
    "1045810": "NVDA",
    "912057": "COST",
    "315189": "DE",
}

# Real periods mapping: accession -> real date
REAL_PERIODS = {
    "aapl_q1": dt.date(2026, 3, 28),  # +3 days from 2026-03-31
    "nvda_q1": dt.date(2026, 4, 26),  # +4 days from 2026-04-30
    "cost_q3": dt.date(2026, 6, 10),  # -10 days from 2026-05-31 (note: -10 means it's AFTER the rounded date)
    "de_fy": dt.date(2026, 7, 3),     # -3 days from 2026-06-30 (note: -3 means it's AFTER the rounded date)
}


def test_extract_earnings_uses_real_periods_aapl():
    """AAPL: +3 days offset should use real period."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[:1], NUM_REAL_PERIODS[:1]))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "AAPL"
    assert evt["report_date"] == dt.date(2026, 4, 3)  # filing date unchanged
    assert evt["period_end"] == dt.date(2026, 3, 28)  # real period, not rounded 2026-03-31
    assert evt["actual_eps"] == 1.95


def test_extract_earnings_uses_real_periods_nvda():
    """NVDA: +4 days offset should use real period."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[1:2], NUM_REAL_PERIODS[1:2]))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "NVDA"
    assert evt["report_date"] == dt.date(2026, 5, 28)
    assert evt["period_end"] == dt.date(2026, 4, 26)  # real period, not rounded 2026-04-30
    assert evt["actual_eps"] == 0.81


def test_extract_earnings_uses_real_periods_cost():
    """COST: -10 days offset (real is later) should use real period."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[2:3], NUM_REAL_PERIODS[2:3]))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "COST"
    assert evt["report_date"] == dt.date(2026, 6, 24)
    assert evt["period_end"] == dt.date(2026, 6, 10)  # real period, not rounded 2026-05-31
    assert evt["actual_eps"] == 3.22


def test_extract_earnings_uses_real_periods_de():
    """DE: -3 days offset (real is later) should use real period."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[3:4], NUM_REAL_PERIODS[3:4]))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "DE"
    assert evt["report_date"] == dt.date(2026, 7, 30)
    assert evt["period_end"] == dt.date(2026, 7, 3)  # real period, not rounded 2026-06-30
    assert evt["actual_eps"] == 28.45


def test_extract_earnings_missing_accession_fallback():
    """Accession not in real_periods mapping should use rounded date."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[:1], NUM_REAL_PERIODS[:1]))
    # Only provide NVDA, not AAPL
    partial_real_periods = {k: v for k, v in REAL_PERIODS.items() if k != "aapl_q1"}
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=partial_real_periods)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "AAPL"
    assert evt["period_end"] == dt.date(2026, 3, 31)  # rounded, not real


def test_extract_earnings_none_real_periods():
    """With real_periods=None, should use only rounded dates (same as before)."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[:1], NUM_REAL_PERIODS[:1]))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=None)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "AAPL"
    assert evt["period_end"] == dt.date(2026, 3, 31)  # rounded


def test_extract_earnings_empty_real_periods_dict():
    """With real_periods={}, should use only rounded dates (same as before)."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[:1], NUM_REAL_PERIODS[:1]))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods={})
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "AAPL"
    assert evt["period_end"] == dt.date(2026, 3, 31)  # rounded


def test_extract_earnings_no_real_periods_arg():
    """Omitting real_periods argument should use rounded dates (backward compat)."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[:1], NUM_REAL_PERIODS[:1]))
    events = ds.extract_earnings(sub, num, CIK_MAP)
    assert len(events) == 1
    evt = events[0]
    assert evt["ticker"] == "AAPL"
    assert evt["period_end"] == dt.date(2026, 3, 31)  # rounded


def test_extract_earnings_real_period_too_far_ignored():
    """Real period >15 days away should be ignored (outside WINDOW_DAYS)."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS[:1], NUM_REAL_PERIODS[:1]))
    # Provide a real period 20 days away (outside 15-day window)
    far_real_periods = {"aapl_q1": dt.date(2026, 4, 20)}  # 20 days after rounded 2026-03-31
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=far_real_periods)
    assert len(events) == 1
    evt = events[0]
    assert evt["period_end"] == dt.date(2026, 3, 31)  # falls back to rounded


def test_extract_earnings_multiple_filings_same_company():
    """Two filings by one company, each with own real date, not merged."""
    sub_two_quarters = [
        {"adsh": "aapl_q1", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
         "period": "20260331", "filed": "20260403", "fp": "Q2"},
        {"adsh": "aapl_q2", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
         "period": "20260630", "filed": "20260704", "fp": "Q3"},
    ]
    num_two_quarters = [
        {"adsh": "aapl_q1", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
         "ddate": "20260331", "qtrs": "2", "uom": "USD/shares", "value": "1.95"},
        {"adsh": "aapl_q2", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
         "ddate": "20260630", "qtrs": "2", "uom": "USD/shares", "value": "2.05"},
    ]
    real_periods_two = {
        "aapl_q1": dt.date(2026, 3, 28),  # ~91 days before Q2
        "aapl_q2": dt.date(2026, 6, 27),  # ~91 days after Q1
    }
    sub, num = ds.parse_dataset(_zip(sub_two_quarters, num_two_quarters))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=real_periods_two)

    # Should have 2 distinct events, not merged
    assert len(events) == 2

    evt_q1 = next(e for e in events if e["report_date"] == dt.date(2026, 4, 3))
    evt_q2 = next(e for e in events if e["report_date"] == dt.date(2026, 7, 4))

    assert evt_q1["period_end"] == dt.date(2026, 3, 28)
    assert evt_q1["actual_eps"] == 1.95

    assert evt_q2["period_end"] == dt.date(2026, 6, 27)
    assert evt_q2["actual_eps"] == 2.05


def test_extract_earnings_period_none_no_error():
    """Period missing/None should stay None without raising."""
    sub_missing_period = [
        {"adsh": "test_none", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
         "period": "", "filed": "20260403", "fp": "Q2"},  # Empty period
    ]
    num_empty = [
        {"adsh": "test_none", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
         "ddate": "", "qtrs": "2", "uom": "USD/shares", "value": "1.95"},
    ]
    sub, num = ds.parse_dataset(_zip(sub_missing_period, num_empty))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)

    # Should have 1 event with period_end = None
    assert len(events) == 1
    assert events[0]["period_end"] is None
    assert events[0]["actual_eps"] == 1.95


def test_extract_earnings_non_periodic_forms_skipped():
    """Non-periodic forms (e.g. 8-K) should not appear in earnings events."""
    sub_mixed = [
        {"adsh": "aapl_q1", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
         "period": "20260331", "filed": "20260403", "fp": "Q2"},
        {"adsh": "aapl_8k", "cik": "0000320193", "name": "APPLE INC", "form": "8-K",
         "period": "20260404", "filed": "20260404", "fp": ""},  # 8-K is non-periodic
    ]
    num_mixed = [
        {"adsh": "aapl_q1", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
         "ddate": "20260331", "qtrs": "2", "uom": "USD/shares", "value": "1.95"},
        {"adsh": "aapl_8k", "tag": "NetIncomeLoss", "version": "us-gaap/2026",
         "ddate": "20260404", "qtrs": "1", "uom": "USD", "value": "25000000000"},
    ]
    sub, num = ds.parse_dataset(_zip(sub_mixed, num_mixed))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)

    # Should have only the 10-Q, not the 8-K
    assert len(events) == 1
    assert events[0]["report_date"] == dt.date(2026, 4, 3)


def test_extract_earnings_all_four_with_real_periods():
    """All four companies with real periods applied correctly."""
    sub, num = ds.parse_dataset(_zip(SUB_REAL_PERIODS, NUM_REAL_PERIODS))
    events = ds.extract_earnings(sub, num, CIK_MAP, real_periods=REAL_PERIODS)

    # Should have 4 events
    assert len(events) == 4

    ev = {e["ticker"]: e for e in events}

    # Verify each has the real period_end (not rounded)
    assert ev["AAPL"]["period_end"] == dt.date(2026, 3, 28)
    assert ev["NVDA"]["period_end"] == dt.date(2026, 4, 26)
    assert ev["COST"]["period_end"] == dt.date(2026, 6, 10)
    assert ev["DE"]["period_end"] == dt.date(2026, 7, 3)

    # report_date (filing date) unchanged
    assert ev["AAPL"]["report_date"] == dt.date(2026, 4, 3)
    assert ev["NVDA"]["report_date"] == dt.date(2026, 5, 28)
    assert ev["COST"]["report_date"] == dt.date(2026, 6, 24)
    assert ev["DE"]["report_date"] == dt.date(2026, 7, 30)

    # EPS values preserved
    assert ev["AAPL"]["actual_eps"] == 1.95
    assert ev["NVDA"]["actual_eps"] == 0.81
    assert ev["COST"]["actual_eps"] == 3.22
    assert ev["DE"]["actual_eps"] == 28.45
