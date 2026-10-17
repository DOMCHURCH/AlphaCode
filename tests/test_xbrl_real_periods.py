"""XBRL real_periods parameter: test coverage for accession-based period substitution.

SEC bulk datasets round period_end dates to month-end. The XBRL frames API carries
the real date. This test suite covers threading a real_periods mapping through
extract_facts and extract_and_validate, with period substitution only when the
fact's rounded date matches the filing's own period (not prior-year comparatives).
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from src.ingest import xbrl

# Test data fixtures, following the pattern from tests/test_xbrl.py
NUM_COLS = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom", "segments", "value"]
SUB_COLS = ["adsh", "cik", "name", "form", "period", "filed", "fp"]


def _num(**kw) -> dict:
    """Helper to build a num.txt row."""
    row = dict.fromkeys(NUM_COLS, "")
    row.update(adsh="test_acn", version="us-gaap/2026", ddate="20260331", uom="USD")
    row.update(kw)
    return row


def _sub(**kw) -> dict:
    """Helper to build a sub.txt row."""
    row = dict.fromkeys(SUB_COLS, "")
    row.update(adsh="test_acn", cik="0000000000", form="10-K", period="20260331", filed="20260501", fp="FY")
    row.update(kw)
    return row


def _df(rows: list[dict], cols: list[str]) -> pd.DataFrame:
    """Build a DataFrame from rows and columns."""
    return pd.DataFrame([{c: str(r.get(c, "")) for c in cols} for r in rows], columns=cols)


# --------------------------------------------------------------- Test Case (a): Basic real_periods substitution


def test_aapl_real_period_minus_3_days():
    """AAPL: rounded 2026-03-31 -> real 2026-03-28 (3 days earlier)."""
    sub = _df([_sub(adsh="aapl1", cik="0000320193", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="aapl1", tag="Assets", qtrs="0", ddate="20260331", value="100")], NUM_COLS)

    real_periods = {"aapl1": dt.date(2026, 3, 28)}
    rows, report = xbrl.extract_facts(sub, num, {"320193": "AAPL"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 3, 28), "Should use real date"
    assert report.period_end_exact == 1, "Should count the substitution"


def test_nvda_real_period_plus_4_days():
    """NVDA: rounded 2026-03-31 -> real 2026-04-04 (4 days later, within 15-day window)."""
    sub = _df([_sub(adsh="nvda1", cik="0001045810", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="nvda1", tag="Assets", qtrs="0", ddate="20260331", value="200")], NUM_COLS)

    real_periods = {"nvda1": dt.date(2026, 4, 4)}
    rows, report = xbrl.extract_facts(sub, num, {"1045810": "NVDA"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 4, 4), "Should use real date"
    assert report.period_end_exact == 1


def test_cost_real_period_minus_10_days():
    """COST: rounded 2026-04-30 -> real 2026-05-10 (10 days later, within window)."""
    sub = _df([_sub(adsh="cost1", cik="0000909832", period="20260430", filed="20260601")], SUB_COLS)
    num = _df([_num(adsh="cost1", tag="Assets", qtrs="0", ddate="20260430", value="300")], NUM_COLS)

    real_periods = {"cost1": dt.date(2026, 5, 10)}
    rows, report = xbrl.extract_facts(sub, num, {"909832": "COST"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 5, 10), "Should use real date"
    assert report.period_end_exact == 1


def test_de_real_period_minus_3_days():
    """DE: rounded 2026-11-30 -> real 2026-11-27 (3 days earlier)."""
    sub = _df([_sub(adsh="de1", cik="0000315189", period="20261130", filed="20261215")], SUB_COLS)
    num = _df([_num(adsh="de1", tag="Assets", qtrs="0", ddate="20261130", value="400")], NUM_COLS)

    real_periods = {"de1": dt.date(2026, 11, 27)}
    rows, report = xbrl.extract_facts(sub, num, {"315189": "DE"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 11, 27), "Should use real date"
    assert report.period_end_exact == 1


# --------------------------------------------------------------- Test Case (b): Missing accession -> keeps rounded date


def test_accession_not_in_mapping_keeps_rounded():
    """When accession is absent from real_periods, keeps the rounded date."""
    sub = _df([_sub(adsh="unknown1", cik="0000000000", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="unknown1", tag="Assets", qtrs="0", ddate="20260331", value="500")], NUM_COLS)

    real_periods = {}  # Accession not present
    rows, report = xbrl.extract_facts(sub, num, {"0": "TEST"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 3, 31), "Should keep rounded date"
    assert report.period_end_exact == 0, "Should not count as exact"


# --------------------------------------------------------------- Test Case (c): real_periods=None and {} -> identical behavior


def test_real_periods_none_vs_without_argument():
    """Calling with real_periods=None should be identical to not passing it."""
    sub = _df([_sub(adsh="test1", cik="0000000001", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="test1", tag="Assets", qtrs="0", ddate="20260331", value="600")], NUM_COLS)
    cik_map = {"1": "TEST"}

    # Call without real_periods
    rows1, report1 = xbrl.extract_facts(sub, num, cik_map)

    # Call with real_periods=None
    rows2, report2 = xbrl.extract_facts(sub, num, cik_map, real_periods=None)

    assert rows1 == rows2, "Results should be identical"
    assert report1.period_end_exact == report2.period_end_exact == 0


def test_real_periods_empty_dict_vs_without_argument():
    """Calling with empty real_periods dict should be identical to not passing it."""
    sub = _df([_sub(adsh="test2", cik="0000000002", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="test2", tag="Assets", qtrs="0", ddate="20260331", value="700")], NUM_COLS)
    cik_map = {"2": "TEST"}

    # Call without real_periods
    rows1, report1 = xbrl.extract_facts(sub, num, cik_map)

    # Call with empty dict
    rows2, report2 = xbrl.extract_facts(sub, num, cik_map, real_periods={})

    assert rows1 == rows2, "Results should be identical"
    assert report1.period_end_exact == report2.period_end_exact == 0


# --------------------------------------------------------------- Test Case (d): Real date >15 days away is ignored


def test_real_period_too_far_outside_window():
    """When real date is >15 days away (e.g., 20 days), the rounded date is used."""
    sub = _df([_sub(adsh="window1", cik="0000000003", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="window1", tag="Assets", qtrs="0", ddate="20260331", value="800")], NUM_COLS)

    # 20 days away: 2026-03-31 + 20 = 2026-04-20, outside 15-day window
    real_periods = {"window1": dt.date(2026, 4, 20)}
    rows, report = xbrl.extract_facts(sub, num, {"3": "TEST"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 3, 31), "Should keep rounded date (outside window)"
    assert report.period_end_exact == 0, "Should not count as exact"


def test_real_period_exactly_at_window_boundary():
    """Real date exactly 15 days away should be accepted."""
    sub = _df([_sub(adsh="boundary1", cik="0000000004", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="boundary1", tag="Assets", qtrs="0", ddate="20260331", value="900")], NUM_COLS)

    # Exactly 15 days away
    real_periods = {"boundary1": dt.date(2026, 4, 15)}
    rows, report = xbrl.extract_facts(sub, num, {"4": "TEST"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 4, 15), "Should use real date (at boundary)"
    assert report.period_end_exact == 1


# --------------------------------------------------------------- Test Case (e): Comparative facts keep rounded date


def test_prior_year_comparative_keeps_rounded():
    """Facts from prior fiscal periods (comparatives) keep their rounded dates."""
    sub = _df([_sub(adsh="comp1", cik="0000000005", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([
        # Filing's own period - should get real date
        _num(adsh="comp1", tag="Assets", qtrs="0", ddate="20260331", value="1000"),
        # Prior-year comparative - should keep rounded date
        _num(adsh="comp1", tag="Assets", qtrs="0", ddate="20250331", value="950"),
    ], NUM_COLS)

    real_periods = {"comp1": dt.date(2026, 3, 28)}
    rows, report = xbrl.extract_facts(sub, num, {"5": "TEST"}, real_periods)

    assert len(rows) == 2
    filing_period = [r for r in rows if r["period_end"] == dt.date(2026, 3, 28)]
    comparative = [r for r in rows if r["period_end"] == dt.date(2025, 3, 31)]

    assert len(filing_period) == 1, "Filing period should have real date"
    assert len(comparative) == 1, "Comparative should keep rounded date"
    assert report.period_end_exact == 1, "Only filing period counts"


# --------------------------------------------------------------- Test Case (f): Calendar-aligned filer (real == rounded)


def test_real_period_same_as_rounded_not_counted():
    """When real_date == rounded_date, no substitution occurs, not counted."""
    sub = _df([_sub(adsh="aligned1", cik="0000000006", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="aligned1", tag="Assets", qtrs="0", ddate="20260331", value="1100")], NUM_COLS)

    # Real date same as rounded
    real_periods = {"aligned1": dt.date(2026, 3, 31)}
    rows, report = xbrl.extract_facts(sub, num, {"6": "TEST"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 3, 31)
    # Not counted because no actual substitution happened
    assert report.period_end_exact == 0, "Same date should not be counted as exact"


# --------------------------------------------------------------- Test Case (g): extract_and_validate passes through


def test_extract_and_validate_with_real_periods():
    """extract_and_validate should pass real_periods to extract_facts and validate."""
    sub = _df([_sub(adsh="validate1", cik="0000000007", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([
        _num(adsh="validate1", tag="Assets", qtrs="0", ddate="20260331", value="10000000000"),
        _num(adsh="validate1", tag="Liabilities", qtrs="0", ddate="20260331", value="7000000000"),
        _num(adsh="validate1", tag="StockholdersEquity", qtrs="0", ddate="20260331", value="3000000000"),
    ], NUM_COLS)

    real_periods = {"validate1": dt.date(2026, 3, 28)}
    rows, report = xbrl.extract_and_validate(sub, num, {"7": "TEST"}, real_periods)

    # Should have validated results with real period dates
    assets_rows = [r for r in rows if r["metric"] == "total_assets"]
    assert len(assets_rows) > 0, "Should have extracted assets"
    assert assets_rows[0]["period_end"] == dt.date(2026, 3, 28), "Should have real date"
    # 3 facts extracted, all had their period_end replaced
    assert report.period_end_exact == 3, "Should count all facts whose period_end was replaced"


# --------------------------------------------------------------- Test Case (h): One filing's own period -> all same period_end


def test_one_filing_one_period_all_same_period_end():
    """All facts for a filing's own period (not comparatives) should end with same period_end."""
    sub = _df([_sub(adsh="same1", cik="0000000008", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([
        _num(adsh="same1", tag="Assets", qtrs="0", ddate="20260331", value="1200"),
        _num(adsh="same1", tag="Liabilities", qtrs="0", ddate="20260331", value="800"),
        _num(adsh="same1", tag="StockholdersEquity", qtrs="0", ddate="20260331", value="400"),
    ], NUM_COLS)

    real_periods = {"same1": dt.date(2026, 3, 25)}
    rows, report = xbrl.extract_facts(sub, num, {"8": "TEST"}, real_periods)

    assert len(rows) == 3
    period_ends = set(r["period_end"] for r in rows)
    assert len(period_ends) == 1, "All rows should have the same period_end"
    assert period_ends.pop() == dt.date(2026, 3, 25), "Should all have the real date"
    assert report.period_end_exact == 3, "All 3 facts should be counted"


# --------------------------------------------------------------- Test Case (i): filing_date untouched


def test_filing_date_untouched():
    """filing_date should never be modified, only period_end."""
    sub = _df([_sub(adsh="filedate1", cik="0000000009", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([_num(adsh="filedate1", tag="Assets", qtrs="0", ddate="20260331", value="1300")], NUM_COLS)

    real_periods = {"filedate1": dt.date(2026, 3, 20)}
    rows, report = xbrl.extract_facts(sub, num, {"9": "TEST"}, real_periods)

    assert len(rows) == 1
    assert rows[0]["period_end"] == dt.date(2026, 3, 20), "period_end changed"
    assert rows[0]["filing_date"] == dt.date(2026, 5, 1), "filing_date unchanged"


# --------------------------------------------------------------- Test Case: Dedupe semantics with real_periods


def test_dedupe_with_real_periods_changes_key():
    """Changing period_end via real_periods changes the dedup key, avoiding collisions."""
    # Two aliases of the same concept, same filing, both with rounded ddate
    sub = _df([_sub(adsh="dedupe1", cik="0000000010", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([
        # Two tags for the same metric, same value - should dedupe to one
        _num(adsh="dedupe1", tag="Assets", qtrs="0", ddate="20260331", value="2000"),
        _num(adsh="dedupe1", tag="TotalAssets", qtrs="0", ddate="20260331", value="2000"),
    ], NUM_COLS)

    real_periods = {"dedupe1": dt.date(2026, 3, 28)}
    rows, report = xbrl.extract_facts(sub, num, {"10": "TEST"}, real_periods)

    # Both should map to same metric (total_assets), so dedup should reduce to 1
    assets = [r for r in rows if r["metric"] == "total_assets"]
    assert len(assets) == 1, "Aliases should dedupe to single row"
    assert assets[0]["period_end"] == dt.date(2026, 3, 28), "Winner should have real date"
    assert report.period_end_exact == 1, "One fact had period replaced"


# --------------------------------------------------------------- Test Case: Q4 relabeling with real_periods


def test_validation_consistency_with_real_periods():
    """Validation (A=L+E) should work correctly with real period dates."""
    # A perfect balance sheet with real period
    sub = _df([_sub(adsh="valid1", cik="0000000011", period="20260331", filed="20260501")], SUB_COLS)
    num = _df([
        _num(adsh="valid1", tag="Assets", qtrs="0", ddate="20260331", value="10000"),
        _num(adsh="valid1", tag="Liabilities", qtrs="0", ddate="20260331", value="6000"),
        _num(adsh="valid1", tag="StockholdersEquity", qtrs="0", ddate="20260331", value="4000"),
    ], NUM_COLS)

    real_periods = {"valid1": dt.date(2026, 3, 28)}
    rows, report = xbrl.extract_and_validate(sub, num, {"11": "TEST"}, real_periods)

    # Should have 3 facts with real period
    assert len(rows) == 3, "Should extract all metrics"
    assert report.validated_periods == 1, "Should validate the one period"
    assert report.rejected_periods == 0, "Should not reject valid balance sheet"
    # All should have the real date
    for row in rows:
        assert row["period_end"] == dt.date(2026, 3, 28), "All should have real period"
    assert report.period_end_exact == 3, "All 3 facts should have period replaced"


# --------------------------------------------------------------- Q4 labelling must not depend on the rounding


@pytest.mark.parametrize("real", [dt.date(2026, 5, 10), dt.date(2026, 4, 26), None], ids=["COST-10", "NVDA-4", "no-lookup"])
def test_a_fourth_quarter_in_a_ten_k_stays_a_fourth_quarter_whatever_the_real_date(real):
    """The 10-K's three-month fact ends on the fiscal year end and is the fourth quarter. The check
    that decides that compares against SEC's rounded `period` with a 7-day tolerance; the real date
    (Costco's is 10 days from its rounded one) must not push it outside."""
    sub = _df([_sub(adsh="k1", cik="0000909832", form="10-K", period="20260430", filed="20260610", fp="FY")], SUB_COLS)
    num = _df([_num(adsh="k1", tag="Revenues", qtrs="1", ddate="20260430", value="500")], NUM_COLS)
    rows, _ = xbrl.extract_facts(sub, num, {"909832": "COST"}, {"k1": real} if real else None)
    q1 = [r for r in rows if r["value"] == 500.0]
    assert q1 and q1[0]["fiscal_period"] == "Q4"
    if real:
        assert {r["period_end"] for r in rows} == {real}
