"""The balance-sheet read path: which figure gets shown, and when it says so.

This file used to be 176 lines with ONE test function and ZERO assertions. It
queried the database, printed the result, and returned -- so it could not fail,
and it showed green next to `get_balance_sheet`, the exact function that
carried the "oldest filing wins" bug. A test that cannot fail is worse than no
test: it is a claim of coverage over the code least likely to have any.

What is asserted here is the four decisions this path makes that a wrong answer
would be invisible in:

* which of two filings for one period is the one shown;
* which of two sources wins when they filed on the same day;
* what the drawing says when A != L + E, and how far the claims column is
  allowed to run;
* that an unknown fiscal period is never quietly treated as a quarter.

Sample selection follows the original file's reasoning, which was sound: JPM as
a bank, AAL with negative equity, MSFT asset-light. The shapes are seeded here
rather than read from a live database, because a test that only passes when
somebody has run the backfill is a test that gets deleted.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

Q = dt.date(2025, 12, 31)
EARLIER = dt.date(2026, 2, 1)
LATER = dt.date(2026, 5, 1)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A throwaway on-disk database with the app around it.

    On disk, not `:memory:`: `session_scope` opens its own connections and an
    in-memory database is private to the connection that made it.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bs.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import app

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


# NOTE ON NAMES: rows are seeded with the stored METRIC name, which is not
# always the concept the read path exposes. `total_equity` in the table becomes
# `shareholders_equity` on the sheet -- see `BALANCE_SHEET_CONCEPTS`. Seeding
# with the concept name silently stores a metric nothing reads, and the sheet
# comes back looking like a filer that reports no equity.
def seed(ticker: str, rows: list[dict], *, name: str | None = None) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker=ticker, name=name or f"{ticker} Inc"
        ))
        for r in rows:
            s.add(Fundamental(**r))


def fact(ticker, metric, value, *, filed, source="sec", period=Q, fp="FY",
         restated=False):
    return {
        "ticker": ticker, "metric": metric, "value": value,
        "period_end": period, "fiscal_period": fp, "filing_date": filed,
        "source": source, "restated": restated,
    }


# ---------------------------------------------------------------------------
# 1. Restatements: the newest filing is the one shown
# ---------------------------------------------------------------------------

def test_a_restated_figure_supersedes_the_original(client):
    """JPM, as a bank: asset-heavy, and a restatement of total assets.

    The bug this guards was a dict comprehension over rows ordered
    `filing_date.desc()` -- last write wins, so descending order kept the
    EARLIEST filing and every restatement was silently discarded.
    """
    seed("JPM", [
        fact("JPM", "total_assets", 3_800_000.0, filed=EARLIER),
        fact("JPM", "total_assets", 3_875_000.0, filed=LATER, restated=True),
        fact("JPM", "total_liabilities", 3_500_000.0, filed=LATER),
        fact("JPM", "total_equity", 375_000.0, filed=LATER),
        fact("JPM", "deposits", 2_400_000.0, filed=LATER),
    ], name="JPMorgan Chase")
    from src.company.balancesheet import get_balance_sheet

    sheet = get_balance_sheet("JPM")
    assert sheet is not None
    assert sheet.assets["total_assets"].value == 3_875_000.0
    assert sheet.assets["total_assets"].restated is True
    # The header date must describe the figures actually on the page, not
    # whichever metric happened to be first in a dict.
    assert sheet.filing_date == LATER


def test_a_banks_sector_specific_lines_survive_the_read(client):
    """Deposits and loans are the whole shape of a bank's balance sheet. If the
    read path drops them, JPM draws as a company with one enormous
    unexplained block."""
    seed("JPM", [
        fact("JPM", "total_assets", 3_800_000.0, filed=EARLIER),
        fact("JPM", "loans", 1_300_000.0, filed=EARLIER),
        fact("JPM", "deposits", 2_400_000.0, filed=EARLIER),
        fact("JPM", "total_liabilities", 3_400_000.0, filed=EARLIER),
        fact("JPM", "total_equity", 400_000.0, filed=EARLIER),
    ])
    from src.company.balancesheet import get_balance_sheet

    sheet = get_balance_sheet("JPM")
    assert sheet.assets["loans"].value == 1_300_000.0
    assert sheet.liabilities["deposits"].value == 2_400_000.0


def test_a_ticker_with_no_filings_returns_none_not_an_empty_sheet(client):
    """"No data" and "a sheet full of zeroes" are different answers, and only
    one of them is honest. A zero is a claim."""
    from src.company.balancesheet import get_balance_sheet

    assert get_balance_sheet("NOSUCH") is None


def test_pinning_a_period_that_is_not_loaded_returns_none(client):
    """Distinct from "this ticker has no data", and must not collapse into it:
    `verify` compares against ONE filing and needs to know the difference."""
    seed("MSFT", [fact("MSFT", "total_assets", 512_000.0, filed=EARLIER)])
    from src.company.balancesheet import get_balance_sheet

    assert get_balance_sheet("MSFT") is not None
    assert get_balance_sheet("MSFT", period_end=dt.date(2019, 6, 30)) is None


def test_a_pinned_period_reads_that_period_and_not_the_newest(client):
    """An unpinned comparison reports the passage of time as an extraction
    error, because a balance sheet grows between filings."""
    older = dt.date(2024, 12, 31)
    seed("MSFT", [
        fact("MSFT", "total_assets", 470_000.0, filed=dt.date(2025, 2, 1),
             period=older),
        fact("MSFT", "total_assets", 512_000.0, filed=EARLIER, period=Q),
    ])
    from src.company.balancesheet import get_balance_sheet

    assert get_balance_sheet("MSFT").assets["total_assets"].value == 512_000.0
    pinned = get_balance_sheet("MSFT", period_end=older)
    assert pinned.assets["total_assets"].value == 470_000.0
    assert pinned.period_end == older


# ---------------------------------------------------------------------------
# 2. Same-day ties: as-reported wins
# ---------------------------------------------------------------------------

def test_the_sec_figure_wins_a_same_day_tie(client):
    """As-reported is what this site claims to show. A vendor's restated number
    filed the same day must not displace it."""
    seed("MSFT", [
        fact("MSFT", "total_assets", 999_999.0, filed=EARLIER, source="yahoo"),
        fact("MSFT", "total_assets", 512_000.0, filed=EARLIER, source="sec"),
    ])
    from src.company.balancesheet import get_balance_sheet

    sheet = get_balance_sheet("MSFT")
    assert sheet.assets["total_assets"].value == 512_000.0


def test_a_later_vendor_filing_still_beats_an_older_sec_one(client):
    """Source preference breaks a TIE. It does not outrank a newer filing --
    that would reintroduce the oldest-wins bug through the side door."""
    seed("MSFT", [
        fact("MSFT", "total_assets", 470_000.0, filed=EARLIER, source="sec"),
        fact("MSFT", "total_assets", 512_000.0, filed=LATER, source="yahoo"),
    ])
    from src.company.balancesheet import get_balance_sheet

    assert get_balance_sheet("MSFT").assets["total_assets"].value == 512_000.0


# ---------------------------------------------------------------------------
# 3. When A != L + E, and how far the claims column may run
# ---------------------------------------------------------------------------

def test_a_filing_that_does_not_balance_is_flagged_and_measured(client):
    """Assets 1000 against claims of 1800: the drawing must say so, and say by
    how much. "It does not balance" without a number is not actionable."""
    seed("ACME", [
        fact("ACME", "total_assets", 1000.0, filed=EARLIER),
        fact("ACME", "total_liabilities", 900.0, filed=EARLIER),
        fact("ACME", "total_equity", 900.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("ACME").as_dict()
    assert d["balances"] is False
    assert d["imbalance_pct"] == pytest.approx(80.0, abs=0.01)


def test_a_filing_inside_the_tolerance_is_not_flagged(client):
    """Rounding and a filer's own presentation slack are not errors. The
    tolerance is 0.5% of assets; 0.1% must pass."""
    seed("ACME", [
        fact("ACME", "total_assets", 1000.0, filed=EARLIER),
        fact("ACME", "total_liabilities", 600.0, filed=EARLIER),
        fact("ACME", "total_equity", 401.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("ACME").as_dict()
    assert d["balances"] is True
    assert d["imbalance_pct"] == pytest.approx(0.1, abs=0.01)


def test_negative_equity_is_named_and_its_span_reported(client):
    """AAL, the negative-equity case, and the reason `claims_span_pct` exists.

    Liabilities exceed assets, so the claims column is drawn to the LIABILITIES
    and equity goes below the baseline. Left unbounded the column would render
    past 100% of the drawing and overflow its own container.
    """
    seed("AAL", [
        fact("AAL", "total_assets", 1000.0, filed=EARLIER),
        fact("AAL", "total_liabilities", 1200.0, filed=EARLIER),
        fact("AAL", "total_equity", -200.0, filed=EARLIER),
    ], name="American Airlines")
    from src.company.view1 import build_view1

    d = build_view1("AAL").as_dict()
    assert d["negative_equity"] is True
    assert d["balances"] is True, "1000 = 1200 + (-200) balances exactly"
    assert any("negative" in n.lower() for n in d["notes"]), d["notes"]

    # `claims_span_pct` is the LIABILITIES as a share of assets, and it is
    # deliberately allowed past 100 -- 1200 against 1000 is 120%, and a column
    # that overshoots the assets it claims is the whole visual point of
    # negative equity. It is asserted here rather than capped because capping
    # it would draw AAL as though its liabilities fitted inside its assets.
    #
    # NOTE: nothing currently READS this field. It is set here and exposed in
    # `as_dict()` and has no consumer anywhere in the report layer, so the
    # drawing is not in fact using the number it computes.
    assert d["claims_span_pct"] == pytest.approx(120.0, abs=0.01)


def test_the_warning_reaches_the_page_a_reader_actually_loads(client):
    """The flag existing is not the fix. It has to render."""
    seed("ACME", [
        fact("ACME", "total_assets", 1000.0, filed=EARLIER),
        fact("ACME", "total_liabilities", 900.0, filed=EARLIER),
        fact("ACME", "total_equity", 900.0, filed=EARLIER),
    ])
    html = client.get("/company/ACME").text

    assert "does not balance" in html
    assert 'class="note warn"' in html, "it must render as a warning, not a note"


def test_a_filer_that_reports_only_totals_says_so(client):
    """A missing line is not a zero. The page has to name what is absent
    rather than absorbing it into "other" and reading as a real line item."""
    seed("ACME", [fact("ACME", "total_assets", 1000.0, filed=EARLIER)])
    from src.company.view1 import build_view1

    d = build_view1("ACME").as_dict()
    assert d["mode"] == "totals_only"
    assert any("liabilities" in n.lower() for n in d["notes"]), d["notes"]


# ---------------------------------------------------------------------------
# 4. An unknown fiscal period is never a quarter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "nan", "NaN", "None", None, "   "])
def test_an_unknown_fiscal_period_is_never_summed_as_a_quarter(raw):
    """The 4x revenue bug. A blank `fp` arrived as the string "nan", failed its
    `== "FY"` test, and fell through to the four-period sum -- so four ANNUAL
    filings were added together and labelled "the last four quarters"."""
    from src.company.view3 import _fiscal_label, _trailing

    assert _fiscal_label(raw) == ""

    years = [dt.date(y, 12, 31) for y in (2022, 2023, 2024, 2025)]
    assert _trailing([("revenue", 100.0, p, raw) for p in years]) is None


def test_four_real_quarters_still_sum(client):
    """The window has to keep working, or the fix above is just a deletion."""
    from src.company.view3 import _trailing

    years = [dt.date(y, 12, 31) for y in (2022, 2023, 2024, 2025)]
    got = _trailing([("revenue", 100.0, p, "Q1") for p in years])
    assert got is not None
    assert got[1] == "ttm"
    assert got[0]["revenue"] == 400.0


# ---------------------------------------------------------------------------
# 5. The claims column is drawn within the height it is allowed
# ---------------------------------------------------------------------------

def _claims_heights(html: str) -> list[float]:
    """Pixel heights of the bands ABOVE the baseline in the claims column.

    The page renders two `.bs-col` blocks -- assets, then claims -- and the
    claims one carries a second `.stack` under a `.baseline` when equity is
    negative. This reads only the first stack of the SECOND column, which is
    the part whose height `claims_span_pct` governs. Splitting on the first
    `.stack` in the document reads the ASSETS column, which is always 300px and
    would make every one of these assertions pass for the wrong reason.
    """
    import re

    cols = html.split('<div class="bs-col"')
    assert len(cols) >= 3, "the drawing did not render two columns"
    claims_col = cols[2].split('<div class="baseline">', 1)[0]
    return [float(h) for h in re.findall(r"height:([\d.]+)px", claims_col)]


def test_an_unbalanced_filing_is_not_drawn_past_its_own_column(client):
    """1,000 in assets against 1,800 in claims.

    `view1._check_identity` computes `claims_span_pct` and its comment says the
    column "is held at the assets column's height rather than drawn past it".
    Nothing read the field, so nothing held it: the claims column rendered 80%
    taller than the assets column beside it, on the page whose whole argument
    is that the two are the same money counted twice.
    """
    seed("ACME", [
        fact("ACME", "total_assets", 1000.0, filed=EARLIER),
        fact("ACME", "total_liabilities", 900.0, filed=EARLIER),
        fact("ACME", "total_equity", 900.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("ACME").as_dict()
    assert d["balances"] is False
    assert d["claims_span_pct"] <= 100.0, "the view must cap the span it allows"

    html = client.get("/company/ACME").text
    heights = _claims_heights(html)
    assert heights, "the drawing rendered no bands"
    # 300px is the column height the page draws to. A little slack for the 3px
    # floor every band gets so a hairline sliver is still visible.
    assert sum(heights) <= 310.0, (
        f"the claims column rendered {sum(heights):.0f}px against a 300px column"
    )


def test_negative_equity_still_overruns_the_assets_column(client):
    """The deliberate exemption, and the reason this is a cap and not a clamp.

    1,000 = 1,100 + (-100) BALANCES. `claims_span_pct` stays at 110 because a
    claims column overrunning the assets it claims is exactly what negative
    equity looks like; flattening it would draw the one thing worth seeing on
    the page as though it were not there.
    """
    seed("AAL", [
        fact("AAL", "total_assets", 1000.0, filed=EARLIER),
        fact("AAL", "total_liabilities", 1100.0, filed=EARLIER),
        fact("AAL", "total_equity", -100.0, filed=EARLIER),
    ], name="American Airlines")
    from src.company.view1 import build_view1

    d = build_view1("AAL").as_dict()
    assert d["balances"] is True
    assert d["negative_equity"] is True
    assert d["claims_span_pct"] == pytest.approx(110.0)

    html = client.get("/company/AAL").text
    heights = _claims_heights(html)
    assert sum(heights) > 300.0, (
        "negative equity must still be drawn overrunning the assets column"
    )


def test_a_balanced_filing_is_drawn_at_full_scale(client):
    """The ordinary case must be untouched by the cap."""
    seed("MSFT", [
        fact("MSFT", "total_assets", 1000.0, filed=EARLIER),
        fact("MSFT", "total_liabilities", 600.0, filed=EARLIER),
        fact("MSFT", "total_equity", 400.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("MSFT").as_dict()
    assert d["balances"] is True
    assert d["claims_span_pct"] == pytest.approx(100.0)

    html = client.get("/company/MSFT").text
    heights = _claims_heights(html)
    assert 295.0 <= sum(heights) <= 305.0, (
        f"a balanced filing should fill the column, got {sum(heights):.0f}px"
    )


# ---------------------------------------------------------------------------
# 6. The identity a consolidated filing is actually written on
# ---------------------------------------------------------------------------

def test_a_filing_with_separate_nci_balances_and_says_so(client):
    """A = L + E + NCI, which is the real identity for this filing.

    A consolidated filer can report the PARENT's equity and the noncontrolling
    interest as two lines with no combined total. 1,000 = 700 + 250 + 50 is
    correct; testing 1,000 = 700 + 250 calls it 5% out and flags a sound filing
    as broken -- our reading being wrong, not their arithmetic.

    `verify.py` and `universe_check.py` already preferred the NCI-inclusive
    basis, so the same filing was sound to the internal checker and "does not
    balance" to a reader. This is the largest single category of filings inside
    the 0.1%.
    """
    seed("NCICO", [
        fact("NCICO", "total_assets", 1000.0, filed=EARLIER),
        fact("NCICO", "total_liabilities", 700.0, filed=EARLIER),
        fact("NCICO", "total_equity", 250.0, filed=EARLIER),
        fact("NCICO", "minority_interest", 50.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("NCICO").as_dict()
    assert d["balances"] is True, "5% out on the parent-only basis, exact with NCI"
    assert d["identity_basis"] == "nci"
    assert d["imbalance_pct"] < 0.5
    assert any("noncontrolling interest" in n for n in d["notes"]), d["notes"]

    html = client.get("/company/NCICO").text
    # The WARNING class, not the phrase: "a filing does not balance" also
    # appears in the Learn-more blurb linking the identity note, and matching
    # on prose would pass or fail on unrelated copy.
    assert 'class="note warn"' not in html, "a sound filing was flagged"
    assert "noncontrolling interest" in html


def test_a_combined_equity_total_never_double_counts_the_nci(client):
    """A filer who publishes `total_equity_incl_nci` already has the NCI inside
    that figure. Adding `minority_interest` on top would break a filing that
    was right, so the NCI is only read when the combined total is absent."""
    seed("INCL", [
        fact("INCL", "total_assets", 1000.0, filed=EARLIER),
        fact("INCL", "total_liabilities", 700.0, filed=EARLIER),
        fact("INCL", "total_equity_incl_nci", 300.0, filed=EARLIER),
        fact("INCL", "total_equity", 250.0, filed=EARLIER),
        fact("INCL", "minority_interest", 50.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("INCL").as_dict()
    assert d["balances"] is True
    assert d["identity_basis"] == "", "the plain sum already closed; no NCI note"
    assert d["total_equity"] == 300.0


def test_an_nci_that_does_not_close_the_gap_still_fails(client):
    """The NCI is a second reading, not an excuse. A filing 40% out does not
    become sound because it happens to report a minority interest."""
    seed("BROKE", [
        fact("BROKE", "total_assets", 1000.0, filed=EARLIER),
        fact("BROKE", "total_liabilities", 500.0, filed=EARLIER),
        fact("BROKE", "total_equity", 100.0, filed=EARLIER),
        fact("BROKE", "minority_interest", 20.0, filed=EARLIER),
    ])
    from src.company.view1 import build_view1

    d = build_view1("BROKE").as_dict()
    assert d["balances"] is False
    assert d["identity_basis"] == ""
    assert any("does not balance" in n for n in d["notes"])
