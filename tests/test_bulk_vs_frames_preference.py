"""When both SEC routes loaded a filing, the figure the company published wins.

The bulk data sets (num.txt) carry only the tags a filer presents on its financial
statements, with SEC's month-end-rounded dates. The frames route sees every fact,
notes included, and takes the best-ranked tag, with the real date. For one filing
that leaves two rows a few days apart, and the frames row is often a note-level
subset: KO's balance sheet says marketable securities 2,842M, the note 2,625M.

Inside such a pair the month-end-dated copy IS the bulk copy by construction (SEC
rounds, frames do not), which is what makes the preference possible without a
stored route. These tests pin it in the three places that resolve the pair: the
CSV collapse, the balance sheet, and the income statement.
"""

from __future__ import annotations

import datetime as dt

import pytest

D = dt.date
M = 1_000_000.0

# ticker, metric, (bulk date, bulk fp, bulk tag, bulk value), (frames date, frames tag, frames value), filed
CASES = {
    "KO": ("investment_securities", (D(2026, 6, 30), "Q2", "MarketableSecurities", 2842 * M),
           (D(2026, 7, 3), "AvailableForSaleSecuritiesDebtSecurities", 2625 * M), D(2026, 7, 29)),
    "DE": ("investment_securities", (D(2026, 7, 31), "Q3", "MarketableSecurities", 1350 * M),
           (D(2026, 8, 2), "DebtSecuritiesAvailableForSaleExcludingAccruedInterest", 1303 * M), D(2026, 8, 27)),
    "TPR": ("long_term_debt", (D(2026, 6, 30), "FY", "LongTermDebt", 2379.0 * M),
            (D(2026, 6, 27), "LongTermDebtNoncurrent", 2396.6 * M), D(2026, 8, 13)),
    "LDOS": ("revenue", (D(2026, 6, 30), "Q2", "Revenues", 4558 * M),
             (D(2026, 7, 3), "RevenueFromContractWithCustomerExcludingAssessedTax", 4548 * M), D(2026, 8, 4)),
    "SNA": ("revenue", (D(2026, 6, 30), "Q2", "Revenues", 1334.8 * M),
            (D(2026, 7, 4), "RevenueFromContractWithCustomerExcludingAssessedTax", 1225.8 * M), D(2026, 7, 23)),
}


def row(ticker, metric, value, period, filed, fp, tag):
    """A row in the SELECT order `_collapse_ticker` takes (source_tag last)."""
    return (ticker, metric, value, period, fp, filed, "sec", False, None, tag)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'route.db'}")
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


FYE = {"LDOS": D(2026, 1, 2), "SNA": D(2026, 1, 3), "HOFT": D(2026, 1, 31)}


def add_prior_year(ticker):
    """One filed fiscal year, so `statements()` can count quarters forward from it."""
    add(ticker, "revenue", 1_000 * M, FYE[ticker], FYE[ticker] + dt.timedelta(days=60), "FY", "Revenues")


def add(ticker, metric, value, period, filed, fp, tag):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        s.add(Fundamental(ticker=ticker, metric=metric, value=value, period_end=period,
                          fiscal_period=fp, filing_date=filed, source="sec", source_tag=tag))


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", sorted(CASES))
def test_the_csv_keeps_the_bulk_copy_under_the_real_date_and_drops_the_frames_copy(ticker):
    from src.dataset import Collapse, _collapse_ticker

    metric, (bd, bfp, btag, bval), (fd, ftag, fval), filed = CASES[ticker]
    out, stats = _collapse_ticker([
        row(ticker, metric, bval, bd, filed, bfp, btag),
        row(ticker, metric, fval, fd, filed, None, ftag),
    ])
    assert [(r[3], r[2]) for r in out] == [(fd, bval)], f"{ticker}: kept the note-level figure"
    assert len(out[0]) == 10 and out[0][-1] == "", "no conflict note, and source_tag is not written"
    assert stats == Collapse(bulk_preferred=1)


def test_the_same_tag_with_different_values_is_still_a_conflict():
    """Only a DIFFERENT concept is settled by the preference; one concept reported
    twice at two values is a real disagreement and stays flagged."""
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        row("T", "revenue", 100.0, D(2026, 6, 30), D(2026, 8, 1), "Q2", "Revenues"),
        row("T", "revenue", 90.0, D(2026, 7, 3), D(2026, 8, 1), None, "Revenues"),
    ])
    assert len(out) == 2 and stats == Collapse(conflicts=1)


def test_a_missing_tag_is_not_evidence_of_a_different_concept():
    from src.dataset import Collapse, _collapse_ticker

    out, stats = _collapse_ticker([
        row("T", "revenue", 100.0, D(2026, 6, 30), D(2026, 8, 1), "Q2", None),
        row("T", "revenue", 90.0, D(2026, 7, 3), D(2026, 8, 1), None, "Revenues"),
    ])
    assert len(out) == 2 and stats == Collapse(conflicts=1)


def test_the_csv_stream_and_the_row_count_agree_after_the_preference(db):
    from src.dataset import Collapse, file_row_count, iter_csv, reset_count_cache
    import csv
    import io

    for ticker, (metric, (bd, bfp, btag, bval), (fd, ftag, fval), filed) in CASES.items():
        add(ticker, metric, bval, bd, filed, bfp, btag)
        add(ticker, metric, fval, fd, filed, None, ftag)
    done = Collapse()
    rows = list(csv.DictReader(io.StringIO("".join(iter_csv(done)))))
    assert done.bulk_preferred == 5 and len(rows) == 5
    assert {r["ticker"]: float(r["value"]) for r in rows} == {t: c[1][3] for t, c in CASES.items()}
    reset_count_cache()
    assert file_row_count() == len(rows)


# ---------------------------------------------------------------------------
# The balance sheet (_resolve_restatements)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["KO", "DE", "TPR"])
def test_the_balance_sheet_shows_the_figure_on_the_companys_balance_sheet(db, ticker):
    from src.company.balancesheet import get_balance_sheet

    metric, (bd, bfp, btag, bval), (fd, ftag, fval), filed = CASES[ticker]
    for d, fp, tag, val in ((bd, bfp, btag, bval), (fd, None, ftag, fval)):
        add(ticker, metric, val, d, filed, fp, tag)
        for m, v in (("total_assets", 10_000 * M), ("total_liabilities", 6_000 * M), ("total_equity", 4_000 * M)):
            add(ticker, m, v, d, filed, fp, "Assets" if m == "total_assets" else None)
    sheet = get_balance_sheet(ticker)
    line = next(v for g in (sheet.assets, sheet.liabilities, sheet.equity)
                for k, v in g.items() if k == metric and not v.missing)
    assert line.value == bval, f"{ticker}: showed {line.value:,.0f}, the note-level subset"
    assert line.period_end == fd, "dated by the real period end"


# ---------------------------------------------------------------------------
# The income statement (statements._normalise)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker", ["LDOS", "SNA"])
def test_the_income_statement_shows_the_revenue_line_the_company_published(db, ticker):
    from src.company.statements import statements

    metric, (bd, bfp, btag, bval), (fd, ftag, fval), filed = CASES[ticker]
    add_prior_year(ticker)
    add(ticker, metric, bval, bd, filed, bfp, btag)
    add(ticker, metric, fval, fd, filed, None, ftag)
    out = statements(ticker, "quarterly")
    assert out["periods"][0]["income_statement"]["revenue"] == bval


def test_hooker_revenue_cogs_and_gross_profit_close_after_the_fix(db):
    """63,250,000 - 43,151,000 = 20,099,000. Before: revenue -522,000 and cogs
    4,269,000 (frames), the gross-profit check failed by 24.9M. The bulk copies
    (revenue via the Including-assessed-tax tag, now mapped) are the statement's."""
    from src.company.statements import statements

    filed = D(2026, 9, 11)
    bulk, frames = D(2026, 7, 31), D(2026, 8, 2)
    add_prior_year("HOFT")
    for metric, bval, btag, fval, ftag in (
        ("revenue", 63_250_000.0, "RevenueFromContractWithCustomerIncludingAssessedTax", -522_000.0, "Revenues"),
        ("cogs", 43_151_000.0, "CostOfGoodsAndServicesSold", 4_269_000.0, "CostOfRevenue"),
        ("gross_profit", 20_099_000.0, "GrossProfit", 20_099_000.0, "GrossProfit"),
    ):
        add("HOFT", metric, bval, bulk, filed, "Q2", btag)
        add("HOFT", metric, fval, frames, filed, None, ftag)
    p = statements("HOFT", "quarterly")["periods"][0]
    inc = p["income_statement"]
    assert (inc["revenue"], inc["cogs"], inc["gross_profit"]) == (63_250_000.0, 43_151_000.0, 20_099_000.0)
    assert inc["revenue"] - inc["cogs"] == inc["gross_profit"]
    check = next(c for c in p["checks"] if c["check"] == "gross_profit")
    assert check["status"] == "passed" and check["gap"] == 0.0


def test_hooker_cogs_in_the_csv_is_the_cost_of_goods_sold_line():
    from src.dataset import _collapse_ticker

    out, stats = _collapse_ticker([
        row("HOFT", "cogs", 43_151_000.0, D(2026, 7, 31), D(2026, 9, 11), "Q2", "CostOfGoodsAndServicesSold"),
        row("HOFT", "cogs", 4_269_000.0, D(2026, 8, 2), D(2026, 9, 11), None, "CostOfRevenue"),
    ])
    assert [(r[3], r[2]) for r in out] == [(D(2026, 8, 2), 43_151_000.0)] and stats.bulk_preferred == 1
