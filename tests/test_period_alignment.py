"""One quarter, one period end -- on the read paths that return a date.

SEC's bulk datasets round every `ddate` to the nearest month end; the XBRL
frames carry the real one. A quarter loaded by both routes is stored twice
(`uq_fundamental_point` includes `period_end`), and `get_balance_sheet` used to
answer `max(period_end)`: the ROUNDED date for Apple, Nvidia, Adobe and J&J
(rounded forward), the real one for Deere (rounded back) -- right only by
accident. The fixtures below are those real filings.

Seeded rather than read from a live database, like the rest of the suite.
"""

from __future__ import annotations

import datetime as dt

import pytest

D = dt.date

# ticker, real period end, rounded (bulk) period end, filing date
AAPL = ("AAPL", D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1))
NVDA = ("NVDA", D(2026, 4, 26), D(2026, 4, 30), D(2026, 5, 20))
ADBE = ("ADBE", D(2026, 5, 29), D(2026, 5, 31), D(2026, 6, 15))
JNJ = ("JNJ", D(2026, 3, 29), D(2026, 3, 31), D(2026, 4, 22))
DEERE = ("DE", D(2026, 5, 3), D(2026, 4, 30), D(2026, 5, 28))
COSTCO = ("COST", D(2026, 5, 10), D(2026, 4, 30), D(2026, 6, 3))
FORWARD_AND_BACK = [AAPL, NVDA, ADBE, JNJ, DEERE, COSTCO]

TAGS = {
    "total_assets": "Assets",
    "total_liabilities": "Liabilities",
    "total_equity": "StockholdersEquity",
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """A throwaway on-disk database (not :memory: -- `session_scope` opens its
    own connections, and an in-memory database is private to one of them)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'periods.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

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
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        for metric, value in (("total_assets", assets), ("total_liabilities", liabilities),
                              ("total_equity", equity)):
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=period,
                fiscal_period="Q2", filing_date=filed, source=source,
                source_tag=tag_assets if metric == "total_assets" else TAGS[metric],
            ))


def both_routes(case, **kw):
    """The filing as the bulk loader stored it (rounded) AND as the frames did (real)."""
    ticker, real, rounded, filed = case
    put(ticker, rounded, filed, **kw)
    put(ticker, real, filed, **kw)


# ---- get_balance_sheet ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", FORWARD_AND_BACK, ids=[c[0] for c in FORWARD_AND_BACK])
def test_the_sheet_carries_the_real_period_end(db, case):
    """Forward (AAPL +3, NVDA +4, ADBE +2, JNJ +2), back (DE -3) and the edge
    (COST -10): the date returned, and printed beside every figure, is the real one."""
    from src.company.balancesheet import get_balance_sheet

    ticker, real, _rounded, filed = case
    both_routes(case)
    sheet = get_balance_sheet(ticker)
    assert sheet is not None
    assert sheet.period_end == real
    assert sheet.filing_date == filed
    assert sheet.assets["total_assets"].period_end == real
    assert sheet.liabilities["total_liabilities"].period_end == real
    assert sheet.assets["total_assets"].value == 1000.0


@pytest.mark.parametrize("case", FORWARD_AND_BACK, ids=[c[0] for c in FORWARD_AND_BACK])
def test_the_real_date_is_returned_whichever_way_the_rows_were_loaded(db, case):
    """Only the rounded row, or only the real row: nothing to merge, nothing invented."""
    from src.company.balancesheet import get_balance_sheet

    ticker, real, rounded, filed = case
    put(ticker, real, filed)
    assert get_balance_sheet(ticker).period_end == real


def test_a_lone_rounded_row_keeps_its_rounded_date(db):
    """A bulk-only filing has no twin to prove the date is rounded. It is left as
    filed (those are the REDATE candidates, which need EDGAR, not a guess)."""
    from src.company.balancesheet import get_balance_sheet

    ticker, _real, rounded, filed = AAPL
    put(ticker, rounded, filed)
    assert get_balance_sheet(ticker).period_end == rounded


def test_a_calendar_aligned_filer_is_unchanged(db):
    from src.company.balancesheet import get_balance_sheet

    for period, filed in ((D(2025, 12, 31), D(2026, 1, 29)), (D(2026, 3, 31), D(2026, 4, 30)),
                          (D(2026, 6, 30), D(2026, 7, 30))):
        put("MSFT", period, filed)
    sheet = get_balance_sheet("MSFT")
    assert sheet.period_end == D(2026, 6, 30)
    assert get_balance_sheet("MSFT", period_end=D(2026, 3, 31)).period_end == D(2026, 3, 31)


def test_a_pinned_rounded_date_resolves_to_the_same_quarter(db):
    """A caller holding the old, rounded date (a bookmark, a cached link) still
    reaches the sheet, under its real date."""
    from src.company.balancesheet import get_balance_sheet

    both_routes(AAPL)
    sheet = get_balance_sheet("AAPL", period_end=AAPL[2])
    assert sheet is not None and sheet.period_end == AAPL[1]
    assert get_balance_sheet("AAPL", period_end=AAPL[1]).period_end == AAPL[1]


def test_two_real_quarters_91_days_apart_stay_two_periods(db):
    """Apple's Q1 and Q2, each loaded twice: the newest is returned by default and
    the older one is still reachable under its own real date, with its own figures."""
    from src.company.balancesheet import get_balance_sheet

    both_routes(("AAPL", D(2025, 12, 27), D(2025, 12, 31), D(2026, 1, 30)), assets=900.0)
    both_routes(AAPL, assets=1000.0)
    assert get_balance_sheet("AAPL").period_end == D(2026, 3, 28)
    older = get_balance_sheet("AAPL", period_end=D(2025, 12, 27))
    assert older.period_end == D(2025, 12, 27)
    assert older.assets["total_assets"].value == 900.0


def test_when_twins_disagree_the_real_dated_row_wins_the_tie(db):
    """Same filing, same source, two rows for one metric: row order must not decide
    which value is shown. The real-dated row is the one the frames API vouches for."""
    from src.company.balancesheet import get_balance_sheet

    ticker, real, rounded, filed = AAPL
    put(ticker, rounded, filed, liabilities=111.0)
    put(ticker, real, filed, liabilities=222.0)
    assert get_balance_sheet(ticker).liabilities["total_liabilities"].value == 222.0
    # ... and the same if the real row was written first
    ticker2, real2, rounded2, filed2 = DEERE
    put(ticker2, real2, filed2, liabilities=222.0)
    put(ticker2, rounded2, filed2, liabilities=111.0)
    assert get_balance_sheet(ticker2).liabilities["total_liabilities"].value == 222.0


def test_a_later_filing_still_beats_an_earlier_one_across_the_rounding(db):
    """Restatement precedence is untouched: the newest filing's figure is shown even
    when the older filing holds the real-dated row -- and it is shown under the REAL
    date, because the rule joins a quarter across filings, not only inside one."""
    from src.company.balancesheet import get_balance_sheet

    ticker, real, rounded, _filed = AAPL
    put(ticker, real, D(2026, 5, 1), assets=1000.0)
    put(ticker, rounded, D(2026, 8, 1), assets=1100.0)
    sheet = get_balance_sheet(ticker)
    assert sheet.assets["total_assets"].value == 1100.0
    assert sheet.period_end == real
    assert sheet.filing_date == D(2026, 8, 1)


# ---- the demo endpoint (same code path as /api/company) ----------------------------------------------------------------

def test_the_demo_endpoint_returns_the_real_apple_date(db, monkeypatch):
    from fastapi.testclient import TestClient

    from src.config.settings import get_settings

    monkeypatch.setenv("DEMO_API_KEY", "demo-key-for-tests-do-not-use")
    get_settings.cache_clear()
    both_routes(AAPL)
    from src.api import app

    with TestClient(app) as c:
        r = c.get("/api/demo/AAPL", headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period_end"] == "2026-03-28"
    assert body["assets"]["total_assets"]["period_end"] == "2026-03-28"


# ---- history ------------------------------------------------------------------------------------------------------------

def test_history_lists_each_quarter_once_under_its_real_date(db):
    from src.company.history import history

    both_routes(("AAPL", D(2025, 12, 27), D(2025, 12, 31), D(2026, 1, 30)), assets=900.0)
    both_routes(AAPL, assets=1000.0)
    out = history("AAPL", None)
    assert [s["period_end"] for s in out["balance_sheets"]] == [D(2026, 3, 28), D(2025, 12, 27)]
    assert out["periods"] == 2
    assert out["available_from"] == "2025-12-27"
    assert [s["assets"]["total_assets"]["value"] for s in out["balance_sheets"]] == [1000.0, 900.0]


def test_history_collapses_a_costco_style_gap_the_old_ten_day_rule_missed(db):
    """COST is 10 days apart (the old rule's edge); a 12-day rounding is still the
    same quarter and used to be listed twice."""
    from src.company.history import _periods

    # Two DIFFERENT filings, so only the cross-filing, month-end-aware collapse can join them.
    put("XYZ", D(2026, 4, 30), D(2026, 6, 1))
    put("XYZ", D(2026, 5, 12), D(2026, 9, 1))
    periods, oldest = _periods("XYZ", None)
    assert periods == [D(2026, 5, 12)]
    assert oldest == D(2026, 5, 12)


def test_history_never_merges_two_real_dates(db):
    from src.company.history import _periods

    put("XYZ", D(2026, 3, 21), D(2026, 5, 1))
    put("XYZ", D(2026, 3, 28), D(2026, 5, 2))
    assert _periods("XYZ", None)[0] == [D(2026, 3, 28), D(2026, 3, 21)]


def test_history_resolves_a_rounded_only_filing_restated_at_its_real_date(db):
    """A bulk-only 10-Q (rounded) and a later 10-K comparative at the real date, in a
    different filing: one period, the real date, and a sheet that is not None."""
    from src.company.history import history

    put("AAPL", D(2026, 3, 31), D(2026, 5, 1), assets=1000.0)       # 10-Q, bulk route only
    put("AAPL", D(2026, 3, 28), D(2026, 9, 1), assets=1010.0)      # later comparative, real date
    out = history("AAPL", None)
    assert out["periods"] == 1
    assert out["balance_sheets"][0]["period_end"] == D(2026, 3, 28)
    assert out["balance_sheets"][0]["assets"]["total_assets"]["value"] == 1010.0


def test_history_leaves_a_calendar_filer_alone(db):
    from src.company.history import _periods

    for period, filed in ((D(2025, 9, 30), D(2025, 10, 30)), (D(2025, 12, 31), D(2026, 1, 29)),
                          (D(2026, 3, 31), D(2026, 4, 30)), (D(2026, 6, 30), D(2026, 7, 30))):
        put("MSFT", period, filed)
    assert _periods("MSFT", None)[0] == [D(2026, 6, 30), D(2026, 3, 31), D(2025, 12, 31), D(2025, 9, 30)]


def test_history_as_of_before_the_real_date_existed_keeps_the_rounded_one(db):
    """Point-in-time: before the later filing existed, the rounded date was all there
    was. The real date must not leak backwards."""
    from src.company.history import _periods

    put("AAPL", D(2026, 3, 31), D(2026, 5, 1))
    put("AAPL", D(2026, 3, 28), D(2026, 9, 1))
    assert _periods("AAPL", None, as_of=D(2026, 6, 1))[0] == [D(2026, 3, 31)]


# ---- restatements -------------------------------------------------------------------------------------------------------

def test_a_restatement_across_a_rounded_and_a_real_filing_is_found_and_not_suspect(db):
    """Filing A stored the quarter under BOTH dates at 1000; filing B, both dates at
    1200. Before: four period keys, a restatement split in two, and each half flagged
    "duplicate_period" (the same value under two periods in one filing) and hidden.
    Now: one quarter, one restatement, quality ok."""
    from src.company.history import restatements

    ticker, real, rounded, _ = AAPL
    for period in (rounded, real):
        put(ticker, period, D(2026, 5, 1), assets=1_000_000.0)
        put(ticker, period, D(2026, 8, 1), assets=1_200_000.0)
    found = [e for e in restatements(ticker) if e["metric"] == "total_assets"]
    assert len(found) == 1
    assert found[0]["period_end"] == real.isoformat()
    assert found[0]["previous"] == 1_000_000.0 and found[0]["current"] == 1_200_000.0
    # and it is visible by default, which is the point
    assert [e["period_end"] for e in restatements(ticker) if e["metric"] == "total_assets"] == [real.isoformat()]


def test_a_restatement_between_a_rounded_only_filing_and_a_real_dated_one_is_found(db):
    """Original loaded by the bulk route (rounded), the revision by the frames (real):
    the same quarter, so the revision is reported once, under the real date."""
    from src.company.history import restatements

    ticker, real, rounded, _ = AAPL
    put(ticker, rounded, D(2026, 5, 1), assets=1_000_000.0)
    put(ticker, real, D(2026, 8, 1), assets=1_200_000.0)
    found = [e for e in restatements(ticker) if e["metric"] == "total_assets"]
    assert len(found) == 1
    assert found[0]["period_end"] == real.isoformat()
    assert found[0]["originally_filed"] == "2026-05-01"
    assert found[0]["revised_in_filing"] == "2026-08-01"


def test_twins_that_differ_inside_one_filing_are_not_a_restatement(db):
    """Two rows of one filing are the same filing: a disagreement between them is a
    data question for the dedupe check, not a revision for customers."""
    from src.company.history import restatements

    ticker, real, rounded, filed = AAPL
    put(ticker, rounded, filed, assets=1_000_000.0)
    put(ticker, real, filed, assets=1_500_000.0)
    assert [e for e in restatements(ticker) if e["metric"] == "total_assets"] == []




# ---- a quarter quoted by a LATER filing that was loaded through the other route -------------------------------------

def _fye_case(env_put=put):
    """Apple's fiscal year end. The 10-K (frames route) holds the real date 2025-09-27; the next 10-Q quotes the
    same balance sheet date as a comparative from the BULK route, rounded to 2025-09-30, with a restated figure."""
    env_put("AAPL", D(2025, 9, 27), D(2025, 10, 31), assets=1000.0, liabilities=600.0, equity=400.0)
    env_put("AAPL", D(2025, 9, 30), D(2026, 1, 30), assets=1010.0, liabilities=610.0, equity=400.0)   # comparative, restated
    env_put("AAPL", D(2025, 12, 27), D(2026, 1, 30), assets=1100.0, liabilities=650.0, equity=450.0)  # the 10-Q's own period


def test_a_comparative_quoted_by_a_later_filing_resolves_to_the_same_period_everywhere(db):
    """The year end exists under two keys (real in the 10-K, rounded in the 10-Q). Every read path must agree
    on ONE period, and on the figure the newest filing gives it."""
    from src.company.balancesheet import get_balance_sheet
    from src.company.history import history, restatements

    _fye_case()
    assert get_balance_sheet("AAPL").period_end == D(2025, 12, 27)          # headline is still the newest quarter
    real = get_balance_sheet("AAPL", period_end=D(2025, 9, 27))
    rounded = get_balance_sheet("AAPL", period_end=D(2025, 9, 30))
    assert real.period_end == rounded.period_end == D(2025, 9, 27)
    assert real.assets["total_assets"].value == rounded.assets["total_assets"].value == 1010.0   # the restated one
    assert real.filing_date == D(2026, 1, 30)
    sheets = history("AAPL", None)["balance_sheets"]
    assert [s["period_end"] for s in sheets] == [D(2025, 12, 27), D(2025, 9, 27)]
    assert sheets[1]["assets"]["total_assets"]["value"] == 1010.0                                  # history == pinned sheet
    [r] = [e for e in restatements("AAPL") if e["metric"] == "total_assets"]
    assert r["period_end"] == "2025-09-27" and (r["previous"], r["current"]) == (1000.0, 1010.0)


def test_the_newest_period_helper_agrees_with_the_sheet_it_describes(db):
    """api.py's page-staleness check compares a stored page against this; it must name the date the sheet shows."""
    from sqlalchemy import select

    from src.company.balancesheet import get_balance_sheet
    from src.company.periods import newest_periods
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    _fye_case()
    both_routes(("MSFT", D(2026, 6, 27), D(2026, 6, 30), D(2026, 7, 30)))
    with session_scope() as s:
        rows = s.execute(select(Fundamental.ticker, Fundamental.period_end, Fundamental.filing_date).distinct()).all()
    newest = newest_periods(rows)
    for t in ("AAPL", "MSFT"):
        assert newest[t] == get_balance_sheet(t).period_end
