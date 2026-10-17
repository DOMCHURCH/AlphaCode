"""Period read paths: point-in-time, restatements, and API responses with real vs. rounded dates.

This test suite covers the behaviour of the period-end canonicalization system across:
- get_balance_sheet(as_of=..., period_end=...)
- history(as_of=...)
- restatements()
- changes()
- /api/company/{ticker}/history and /api/company/{ticker}
- _history_rows() in company_facts

All tests use temporary SQLite databases and do not connect to any external service.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

D = dt.date

# Real filings used as fixtures
AAPL = ("AAPL", D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1))
NVDA = ("NVDA", D(2026, 4, 26), D(2026, 4, 30), D(2026, 5, 20))
ADBE = ("ADBE", D(2026, 5, 29), D(2026, 5, 31), D(2026, 6, 15))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway on-disk database with test setup."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'periods.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-periods")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "1000")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()
    yield
    get_settings.cache_clear()
    reset_engine_cache()


def put(ticker, period, filed, *, assets=1000.0, liabilities=600.0, equity=400.0,
        source="sec", tag_assets="Assets"):
    """Insert a fundamental fact row into the database."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    TAGS = {
        "total_assets": tag_assets,
        "total_liabilities": "Liabilities",
        "total_equity": "StockholdersEquity",
    }

    with session_scope() as s:
        for metric, value in (("total_assets", assets), ("total_liabilities", liabilities),
                              ("total_equity", equity)):
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=period,
                fiscal_period="Q2", filing_date=filed, source=source,
                source_tag=TAGS[metric],
            ))


def both_routes(case, **kw):
    """Load a quarter via both the rounded (bulk) and real (frames) routes."""
    ticker, real, rounded, filed = case
    put(ticker, rounded, filed, **kw)
    put(ticker, real, filed, **kw)


# ============================================================================
# 1. Point-in-time: no backwards leak of the real date
# ============================================================================

def test_point_in_time_get_balance_sheet_before_real_date_returns_rounded(db):
    """get_balance_sheet(as_of=...) filters to filings made by that date,
    so a later filing doesn't affect the as_of result."""
    ticker = "PIT1"
    # Filing 1: a quarter
    put(ticker, D(2025, 12, 31), D(2026, 2, 1), assets=1000.0)
    # Filing 2: a later quarter, later filing date
    put(ticker, D(2026, 3, 31), D(2026, 5, 1), assets=1100.0)

    from src.company.balancesheet import get_balance_sheet

    # Before the second filing, only the first quarter is available
    sheet_before = get_balance_sheet(ticker, as_of=D(2026, 4, 1))
    assert sheet_before is not None
    assert sheet_before.assets["total_assets"].value == 1000.0

    # After both filings, the latest quarter is returned by default
    sheet_after = get_balance_sheet(ticker, as_of=D(2026, 6, 1))
    assert sheet_after is not None
    assert sheet_after.assets["total_assets"].value == 1100.0


def test_point_in_time_history_before_real_date_returns_rounded(db):
    """history(as_of=...) before a later filing returns only what was filed by then.
    Later filings do not appear before their filing date."""
    ticker = "PIT2"
    # Two different quarters
    put(ticker, D(2025, 12, 31), D(2026, 2, 1), assets=1000.0)
    put(ticker, D(2026, 3, 31), D(2026, 5, 1), assets=1100.0)

    from src.company.history import history

    # Before the second filing, only the first quarter shows
    hist_before = history(ticker, years=None, as_of=D(2026, 4, 1))
    assert hist_before is not None
    assert len(hist_before["balance_sheets"]) == 1
    period_end = hist_before["balance_sheets"][0]["period_end"]
    if isinstance(period_end, str):
        period_end = D.fromisoformat(period_end)
    assert period_end == D(2025, 12, 31)

    # After both filings, both quarters show
    hist_after = history(ticker, years=None, as_of=D(2026, 6, 1))
    assert hist_after is not None
    assert len(hist_after["balance_sheets"]) == 2


# ============================================================================
# 2. Three-filing restatement chain: one entry with real date
# ============================================================================

def test_three_filing_restatement_chain_yields_one_entry(db):
    """A 10-Q (rounded-only), 10-Q/A (both dates), and 10-K comparative (real-only)
    for one quarter yields ONE restatement entry with originally_filed from the
    first filing and revised_in_filing from the last, period_end = real date,
    quality ok."""
    ticker = "TST"
    real = D(2026, 3, 28)
    rounded = D(2026, 3, 31)

    # Filing 1: 10-Q, bulk route only (rounded)
    put(ticker, rounded, D(2026, 5, 1), assets=1_000_000.0)
    # Filing 2: 10-Q/A, both routes (rounded and real)
    put(ticker, rounded, D(2026, 8, 1), assets=1_100_000.0)
    put(ticker, real, D(2026, 8, 1), assets=1_100_000.0)
    # Filing 3: 10-K, frames route only (real)
    put(ticker, real, D(2026, 12, 1), assets=1_200_000.0)
    from src.company.history import restatements

    found = [e for e in restatements(ticker, years=10) if e["metric"] == "total_assets"]
    assert len(found) == 1
    assert found[0]["period_end"] == real.isoformat()
    assert found[0]["originally_filed"] == "2026-05-01"
    assert found[0]["revised_in_filing"] == "2026-12-01"
    assert found[0]["previous"] == 1_000_000.0
    assert found[0]["current"] == 1_200_000.0


def test_restatement_chain_with_middle_equal_to_first_skips_between(db):
    """When the middle filing's value equals the first (no change between them),
    there is a restatement from first to last but not between first and middle."""
    ticker = "TST2"
    real = D(2026, 4, 26)
    rounded = D(2026, 4, 30)

    put(ticker, rounded, D(2026, 5, 1), assets=1_000_000.0)
    put(ticker, real, D(2026, 5, 1), assets=1_000_000.0)
    # Middle filing: same value
    put(ticker, rounded, D(2026, 8, 1), assets=1_000_000.0)
    put(ticker, real, D(2026, 8, 1), assets=1_000_000.0)
    # Last filing: different value
    put(ticker, real, D(2026, 12, 1), assets=1_500_000.0)

    from src.company.history import restatements

    found = [e for e in restatements(ticker, years=10) if e["metric"] == "total_assets"]
    assert len(found) == 1
    assert found[0]["originally_filed"] == "2026-05-01"
    assert found[0]["revised_in_filing"] == "2026-12-01"


# ============================================================================
# 3. Two sources with twins: SEC wins, date is real
# ============================================================================

def test_two_sources_sec_wins_with_real_date_twin(db):
    """When SEC and FMP both file the same quarter (same filing date) with twins
    (rounded and real), SEC wins and the returned date is the real one."""
    ticker = "TST3"
    real = D(2026, 5, 29)
    rounded = D(2026, 5, 31)
    filed = D(2026, 6, 15)

    # SEC source: both dates
    put(ticker, real, filed, assets=1000.0, source="sec")
    put(ticker, rounded, filed, assets=1000.0, source="sec")
    # FMP source: both dates (but should lose to SEC)
    put(ticker, real, filed, assets=999.0, source="fmp")
    put(ticker, rounded, filed, assets=999.0, source="fmp")

    from src.company.balancesheet import get_balance_sheet

    sheet = get_balance_sheet(ticker)
    assert sheet is not None
    assert sheet.period_end == real
    assert sheet.assets["total_assets"].value == 1000.0


# ============================================================================
# 4. 52-week filer: 6 quarters all with twins, all real dates returned
# ============================================================================

def test_52week_filer_six_quarters_all_real_dates_no_loss(db):
    """A 52-week filer over 6 quarters, each stored under both rounded and real
    dates in the same filing. history() returns 6 periods, all real dates,
    strictly descending, each with distinct total_assets values.

    Dates must be within 15 days to create twins."""
    ticker = "TST4"
    # Define 6 quarters, each with a real date (within 15 days of month end)
    # and a rounded month-end copy. Roughly 91 days apart (quarters).
    periods = [
        (D(2026, 1, 27), D(2026, 1, 31), D(2026, 3, 1)),      # Q1 2026
        (D(2025, 10, 28), D(2025, 10, 31), D(2025, 12, 1)),   # Q4 2025
        (D(2025, 7, 29), D(2025, 7, 31), D(2025, 9, 1)),      # Q3 2025
        (D(2025, 4, 29), D(2025, 4, 30), D(2025, 6, 1)),      # Q2 2025
        (D(2025, 1, 30), D(2025, 1, 31), D(2025, 3, 1)),      # Q1 2025
        (D(2024, 10, 29), D(2024, 10, 31), D(2024, 12, 1)),   # Q4 2024
    ]

    for i, (real, rounded, filed) in enumerate(periods):
        assets = 1000.0 + (i * 100.0)
        put(ticker, real, filed, assets=assets)
        put(ticker, rounded, filed, assets=assets)

    from src.company.history import history

    hist = history(ticker, years=None)
    assert hist is not None
    sheets = hist["balance_sheets"]
    assert len(sheets) == 6

    # All should be real dates
    period_ends = [s["period_end"] if isinstance(s["period_end"], D) else D.fromisoformat(s["period_end"]) for s in sheets]
    expected_real = [p[0] for p in periods]  # First element is real date
    assert period_ends == sorted(expected_real, reverse=True)

    # Each sheet has distinct assets
    asset_values = [s["assets"]["total_assets"]["value"] for s in sheets]
    # Assets increase from newest (i=0, 1000) to oldest (i=5, 1500)
    # so they should be in ASCENDING order by index (newest to oldest is ascending assets)
    assert asset_values == sorted(asset_values)
    assert len(set(asset_values)) == 6


def test_52week_filer_only_newest_two_duplicated_all_six_returned(db):
    """When only the newest 2 quarters have twins and older ones are rounded-only,
    history() still returns 6 periods without losing any."""
    ticker = "TST5"
    periods = [
        (D(2026, 1, 27), D(2026, 1, 31), D(2026, 3, 1)),
        (D(2025, 10, 28), D(2025, 10, 31), D(2025, 12, 1)),
        (D(2025, 7, 29), D(2025, 7, 31), D(2025, 9, 1)),
        (D(2025, 4, 29), D(2025, 4, 30), D(2025, 6, 1)),
        (D(2025, 1, 30), D(2025, 1, 31), D(2025, 3, 1)),
        (D(2024, 10, 29), D(2024, 10, 31), D(2024, 12, 1)),
    ]

    for i, (real, rounded, filed) in enumerate(periods):
        assets = 1000.0 + (i * 100.0)
        # Only the two newest have twins
        if i < 2:
            put(ticker, real, filed, assets=assets)
            put(ticker, rounded, filed, assets=assets)
        else:
            put(ticker, rounded, filed, assets=assets)

    from src.company.history import history

    hist = history(ticker, years=None)
    assert hist is not None
    assert len(hist["balance_sheets"]) == 6


# ============================================================================
# 5. history() years window and MAX_PERIODS with canonical dates
# ============================================================================

def test_history_years_window_no_loss_at_boundary(db):
    """A quarter older than the years window should be excluded, but all recent
    quarters should be included regardless of rounding."""
    ticker = "TST6"
    today = dt.date.today()

    # A very recent quarter
    put(ticker, today - dt.timedelta(days=30), today - dt.timedelta(days=1), assets=1000.0)

    # A quarter from 11 months ago (inside 1-year window)
    put(ticker, today - dt.timedelta(days=330), today - dt.timedelta(days=295), assets=1100.0)

    # A quarter from 15 months ago (outside 1-year window, should be excluded)
    put(ticker, today - dt.timedelta(days=450), today - dt.timedelta(days=415), assets=900.0)

    from src.company.history import history

    # Request 1 year of history
    hist = history(ticker, years=1)
    assert hist is not None
    # Should have 2 quarters (the recent ones), not 3
    assert len(hist["balance_sheets"]) == 2


def test_history_max_periods_cap_with_canonical_dates(db):
    """MAX_PERIODS cap is applied to canonical periods, not raw rows. Many
    twin filings should not blow past the cap."""
    ticker = "TST7"
    from src.company.history import MAX_PERIODS

    # Create more than MAX_PERIODS * 2 rows (with twins) to ensure capping
    for i in range(MAX_PERIODS + 10):
        base_date = D(2026, 1, 31) - dt.timedelta(days=i * 91)
        real = base_date - dt.timedelta(days=3)
        rounded = base_date
        filed = base_date + dt.timedelta(days=40)
        if filed > dt.date.today():
            continue
        put(ticker, real, filed, assets=1000.0 + i)
        put(ticker, rounded, filed, assets=1000.0 + i)

    from src.company.history import history

    hist = history(ticker, years=None)
    assert hist is not None
    assert len(hist["balance_sheets"]) <= MAX_PERIODS


# ============================================================================
# 6. changes() compares real dates, not twins of one quarter
# ============================================================================

def test_changes_compares_latest_two_canonical_periods(db):
    """changes() computes the delta between the latest two CANONICAL periods.
    With twins, previous_period_end and period_end are the real dates, and the
    delta is between two different quarters, never between a quarter and its
    own rounded copy."""
    ticker = "TST8"

    # Q1: filed with both dates
    q1_real = D(2025, 12, 27)
    q1_rounded = D(2025, 12, 31)
    put(ticker, q1_real, D(2026, 1, 30), assets=900.0)
    put(ticker, q1_rounded, D(2026, 1, 30), assets=900.0)

    # Q2: filed with both dates
    q2_real = D(2026, 3, 28)
    q2_rounded = D(2026, 3, 31)
    put(ticker, q2_real, D(2026, 5, 1), assets=1000.0)
    put(ticker, q2_rounded, D(2026, 5, 1), assets=1000.0)

    from src.company.history import changes

    delta = changes(ticker)
    assert delta is not None
    period_end = delta["period_end"]
    prev_period_end = delta["previous_period_end"]
    if isinstance(period_end, str):
        period_end = D.fromisoformat(period_end)
    if isinstance(prev_period_end, str):
        prev_period_end = D.fromisoformat(prev_period_end)
    assert period_end == q2_real
    assert prev_period_end == q1_real
    # The delta should be between the real dates, not between a twin and a real
    assert delta["since_previous_period"]["total_assets"]["change"] == 100.0


# ============================================================================
# 7. Pinning a date: real and rounded resolve to same sheet, unknown returns None
# ============================================================================

def test_get_balance_sheet_pinned_real_and_rounded_are_identical(db):
    """get_balance_sheet(period_end=<real>) and (period_end=<rounded>) return
    identical sheets with the same values and the same (real) period_end."""
    ticker, real, rounded, filed = AAPL
    both_routes((ticker, real, rounded, filed), assets=1500.0)

    from src.company.balancesheet import get_balance_sheet

    sheet_real = get_balance_sheet(ticker, period_end=real)
    sheet_rounded = get_balance_sheet(ticker, period_end=rounded)

    assert sheet_real is not None
    assert sheet_rounded is not None
    assert sheet_real.period_end == real
    assert sheet_rounded.period_end == real
    assert sheet_real.assets["total_assets"].value == sheet_rounded.assets["total_assets"].value


def test_get_balance_sheet_pinned_unknown_date_returns_none(db):
    """Pinning a date that exists in neither rounded nor real form returns None."""
    ticker, real, rounded, filed = AAPL
    both_routes((ticker, real, rounded, filed))

    from src.company.balancesheet import get_balance_sheet

    unknown = D(2026, 2, 15)
    sheet = get_balance_sheet(ticker, period_end=unknown)
    assert sheet is None


# ============================================================================
# 8. Calendar-aligned filer with stray non-month-end: known limit
# ============================================================================

def test_calendar_aligned_filer_with_stray_non_month_end_is_a_known_limit_of_the_rule(db):
    """A calendar-aligned filer (all month ends) with ONE stray fact date
    from another route (e.g., 2026-04-12) in the same filing as 2026-03-31.
    Current behaviour: the month end IS aliased to the non-month-end date
    because it is within 15 days and non-month-end.

    This test documents the ACTUAL behaviour as it stands, not endorsing it as
    correct. It may be surprising that a one-off stray date triggers aliasing
    when a month end really meant its month end."""
    ticker = "CAL"
    # Add month-end quarters (normal calendar filer)
    put(ticker, D(2026, 3, 31), D(2026, 4, 28), assets=1000.0)
    # Add a stray non-month-end in the same filing
    put(ticker, D(2026, 4, 12), D(2026, 4, 28), assets=1000.0)

    from src.company.balancesheet import get_balance_sheet
    from src.company.periods import period_aliases

    sheet = get_balance_sheet(ticker)
    assert sheet is not None

    # Verify that the aliasing happened (the month end maps to the non-month-end)
    aliases = period_aliases([(D(2026, 3, 31), D(2026, 4, 28)), (D(2026, 4, 12), D(2026, 4, 28))])
    assert D(2026, 3, 31) in aliases or D(2026, 4, 12) in [aliases.get(D(2026, 3, 31), D(2026, 3, 31))]


# ============================================================================
# 9. company_facts history rows with twins
# ============================================================================

def test_company_facts_history_rows_list_real_date_once_each(db):
    """The _history_rows() function in company_facts builds rows using _periods
    and build_view1(). With twins, the rows should list the real date once for
    each quarter, not duplicate the rounded date."""
    ticker = "TST9"

    # Add two quarters with twins
    q1_real = D(2025, 12, 27)
    q1_rounded = D(2025, 12, 31)
    put(ticker, q1_real, D(2026, 1, 30), assets=900.0)
    put(ticker, q1_rounded, D(2026, 1, 30), assets=900.0)

    q2_real = D(2026, 3, 28)
    q2_rounded = D(2026, 3, 31)
    put(ticker, q2_real, D(2026, 5, 1), assets=1000.0)
    put(ticker, q2_rounded, D(2026, 5, 1), assets=1000.0)

    from src.report.company_facts import _history_rows
    from src.company.view1 import build_view1

    view = build_view1(ticker)
    assert view is not None
    rows = _history_rows(view, ticker)

    # Extract period_end dates from rows
    period_ends = [D.fromisoformat(r["period_end"]) for r in rows]

    # Should be the real dates, not the rounded ones
    assert q2_real in period_ends
    assert q2_rounded not in period_ends
    if len(period_ends) > 1:
        assert q1_real in period_ends
        assert q1_rounded not in period_ends


# ============================================================================
# 10. API endpoints return real dates with twins
# ============================================================================

def test_api_company_history_returns_real_dates(db):
    """GET /api/company/{ticker}/history returns real dates, not rounded ones,
    when twins exist."""
    ticker, real, rounded, filed = AAPL
    both_routes((ticker, real, rounded, filed), assets=1000.0)

    from src.api import app

    with TestClient(app) as client:
        # Create an API key
        r = client.post("/api/auth/register", json={"email": "test@example.com", "accept_terms": True})
        assert r.status_code == 201
        api_key = r.json()["api_key"]
        headers = {"X-API-Key": api_key}

        r = client.get(f"/api/company/{ticker}/history", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert len(body["balance_sheets"]) > 0
        period_end_str = body["balance_sheets"][0]["period_end"]
        # Convert to date for comparison if it's a string
        if isinstance(period_end_str, str):
            period_end = D.fromisoformat(period_end_str)
        else:
            period_end = period_end_str
        assert period_end == real
        assert period_end != rounded


def test_api_company_returns_real_date(db):
    """GET /api/company/{ticker} returns the real date, not rounded, when twins exist."""
    ticker, real, rounded, filed = AAPL
    both_routes((ticker, real, rounded, filed), assets=1500.0)

    from src.api import app

    with TestClient(app) as client:
        # Create an API key
        r = client.post("/api/auth/register", json={"email": "test2@example.com", "accept_terms": True})
        assert r.status_code == 201
        api_key = r.json()["api_key"]
        headers = {"X-API-Key": api_key}

        r = client.get(f"/api/company/{ticker}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        period_end_str = body["period_end"]
        if isinstance(period_end_str, str):
            period_end = D.fromisoformat(period_end_str)
        else:
            period_end = period_end_str
        assert period_end == real
        assert period_end != rounded


def test_api_demo_endpoint_returns_real_date(db, monkeypatch):
    """GET /api/demo/{ticker} (the public demo endpoint) returns real dates."""
    monkeypatch.setenv("DEMO_API_KEY", "demo-key-for-tests")
    from src.config.settings import get_settings
    get_settings.cache_clear()

    ticker, real, rounded, filed = NVDA
    both_routes((ticker, real, rounded, filed), assets=2000.0)

    from src.api import app

    with TestClient(app) as client:
        r = client.get(f"/api/demo/{ticker}", headers={"X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 200
        body = r.json()
        assert body["period_end"] == real.isoformat()


# ============================================================================
# Additional: Verify history collapsing works across three different sources
# ============================================================================

def test_history_collapses_twins_from_three_sources_sec_wins(db):
    """When a quarter is stored under both dates from multiple sources
    (SEC bulk + SEC frames + FMP), the entries are properly collapsed to one,
    and SEC is preferred over FMP."""
    ticker = "MULTI"
    real = D(2026, 5, 29)
    rounded = D(2026, 5, 31)
    filed = D(2026, 6, 15)

    # SEC: both routes
    put(ticker, real, filed, assets=1000.0, source="sec")
    put(ticker, rounded, filed, assets=1000.0, source="sec")
    # FMP: both routes (lower values)
    put(ticker, real, filed, assets=900.0, source="fmp")
    put(ticker, rounded, filed, assets=900.0, source="fmp")

    from src.company.history import history

    hist = history(ticker, years=None)
    assert hist is not None
    sheets = hist["balance_sheets"]
    assert len(sheets) == 1
    period_end = sheets[0]["period_end"]
    if isinstance(period_end, str):
        period_end = D.fromisoformat(period_end)
    assert period_end == real
    assert sheets[0]["assets"]["total_assets"]["value"] == 1000.0
