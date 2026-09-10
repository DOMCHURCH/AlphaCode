"""The five ways the site was showing figures that were not true.

Each test seeds the exact shape that produced a wrong number and asserts the
right one. Every one of them fails against the code as it was.

These are separate from the ingest tests because the ingest filters were never
the problem -- what reaches the table was already correct. The bugs were all in
the READ paths, which is why they survived a heavily-tested extractor: nothing
compared what was stored against what was shown.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

Q = dt.date(2025, 12, 31)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'correct.db'}")
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
    from src.dataset import reset_count_cache

    reset_count_cache()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def seed(rows: list[dict]) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker="ACME", name="Acme Corp"
        ))
        for r in rows:
            s.add(Fundamental(**r))


def fact(metric, value, *, filed, source="sec", period=Q, fp="FY", restated=False):
    return {
        "ticker": "ACME", "metric": metric, "value": value,
        "period_end": period, "fiscal_period": fp, "filing_date": filed,
        "source": source, "restated": restated,
    }


# ---------------------------------------------------------------------------
# 1. The restatement that was being thrown away
# ---------------------------------------------------------------------------

def test_the_newest_filing_wins_not_the_oldest(client):
    """The read path ordered `filing_date.desc()` and then collapsed with a
    dict comprehension -- last write wins, so descending order meant the
    EARLIEST filing survived and every restatement was silently discarded.

    A company that revised its balance sheet showed its original figures on
    the drawing, on the company page and through the API, while
    `pit.get_fundamentals` returned the revised ones for the same ticker and
    period. Two answers, one database.
    """
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_assets", 1500.0, filed=dt.date(2026, 5, 1), restated=True),
        fact("total_liabilities", 600.0, filed=dt.date(2026, 2, 1)),
        fact("total_liabilities", 900.0, filed=dt.date(2026, 5, 1), restated=True),
        fact("total_equity", 400.0, filed=dt.date(2026, 2, 1)),
        fact("total_equity", 600.0, filed=dt.date(2026, 5, 1), restated=True),
    ])
    from src.company.balancesheet import get_balance_sheet

    sheet = get_balance_sheet("ACME")
    assert sheet is not None
    assert sheet.assets["total_assets"].value == 1500.0, "kept the superseded figure"
    assert sheet.liabilities["total_liabilities"].value == 900.0
    assert sheet.equity["shareholders_equity"].value == 600.0
    # And the header date describes the figures actually shown.
    assert sheet.filing_date == dt.date(2026, 5, 1)


def test_the_read_path_and_the_pit_module_now_agree(client):
    """`storage/pit.py` says in its own docstring that nothing else may query
    the table directly, and that the latest filing wins. This path does query
    it directly, so it has to apply the same rule -- the bug was that it
    applied the opposite one."""
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_assets", 1500.0, filed=dt.date(2026, 5, 1), restated=True),
    ])
    from src.company.balancesheet import get_balance_sheet
    from src.storage.db import session_scope
    from src.storage.pit import get_fundamentals

    sheet = get_balance_sheet("ACME")
    with session_scope() as s:
        df = get_fundamentals(s, "ACME", as_of=dt.date(2026, 6, 1))
    pit_value = float(df[df["metric"] == "total_assets"]["value"].iloc[0])

    assert sheet.assets["total_assets"].value == pit_value


def test_a_same_day_tie_prefers_the_sec_figure(client):
    """As-reported is what this site claims to show, so on a filing-date tie
    SEC beats a vendor's number -- the same preference `pit` applies."""
    seed([
        fact("total_assets", 999.0, filed=dt.date(2026, 2, 1), source="yahoo"),
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1), source="sec"),
    ])
    from src.company.balancesheet import get_balance_sheet

    assert get_balance_sheet("ACME").assets["total_assets"].value == 1000.0


# ---------------------------------------------------------------------------
# 2. The blank fiscal period that became 'nan'
# ---------------------------------------------------------------------------

def test_a_blank_fiscal_period_reads_as_blank_not_nan():
    """`dtype=str` alone does NOT stop pandas turning a blank field into float
    NaN, and NaN is truthy -- so `str(x or "")` produced the literal "nan"."""
    import io as _io

    import pandas as pd

    raw = "adsh\tfp\nX\t\n"
    without = pd.read_csv(_io.StringIO(raw), sep="\t", dtype=str)
    with_fix = pd.read_csv(
        _io.StringIO(raw), sep="\t", dtype=str, keep_default_na=False
    )
    assert str(without["fp"][0]) == "nan", "the bug this guards is gone upstream"
    assert with_fix["fp"][0] == ""


@pytest.mark.parametrize("raw", ["", "nan", "NaN", None, "  ", "None"])
def test_an_unknown_fiscal_period_is_never_treated_as_a_quarter(raw):
    from src.company.view3 import _fiscal_label

    assert _fiscal_label(raw) == ""


def test_four_annual_filings_are_not_summed_into_a_trailing_year():
    """The 4x revenue bug, at the function that produced it.

    A blank `fp` failed the `== "FY"` test and fell through to the four-period
    sum, so four ANNUAL filings were added together and labelled "the last
    four quarters". Revenue was reported at four times the truth, and it
    propagated into the GDP comparison, placing the company against the wrong
    peers.
    """
    from src.company.view3 import _trailing

    years = [dt.date(y, 12, 31) for y in (2022, 2023, 2024, 2025)]

    def rows(fp):
        return [("revenue", 100.0, p, fp) for p in years]

    # Four annual filings, recorded as the string the bug produced.
    assert _trailing(rows("nan")) is None
    # Genuinely unknown is refused for the same reason: nobody knows what it
    # covers, so it cannot be assumed to be a quarter.
    assert _trailing(rows("")) is None
    assert _trailing(rows(None)) is None

    # Four real quarters still sum, which is the whole point of the window.
    got = _trailing(rows("Q1"))
    assert got is not None and got[1] == "ttm" and got[0]["revenue"] == 400.0

    # And a single annual filing is still taken whole rather than summed.
    mixed = [("revenue", 100.0, p, "Q1") for p in years[:-1]]
    mixed.append(("revenue", 100.0, years[-1], "FY"))
    annual = _trailing(mixed)
    assert annual[1] == "annual" and annual[0]["revenue"] == 100.0


# ---------------------------------------------------------------------------
# 3. The export that disagreed with the API
# ---------------------------------------------------------------------------

def test_the_csv_emits_one_row_per_company_metric_and_period(client):
    """It used to dump the table: four rows for one company and period, two
    different values for total_assets, and no column telling the buyer which
    one the site drew. They paid $79.99 for a file that disagreed with the API
    they could have bought instead."""
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_assets", 1500.0, filed=dt.date(2026, 5, 1), restated=True),
        fact("total_equity", 400.0, filed=dt.date(2026, 2, 1)),
        fact("total_equity", 600.0, filed=dt.date(2026, 5, 1), restated=True),
    ])
    from src.dataset import iter_csv

    lines = "".join(iter_csv()).strip().splitlines()
    assert lines[0].startswith("ticker,metric,value"), "the header must come first"

    body = [ln.split(",") for ln in lines[1:]]
    keys = [(r[0], r[1], r[3]) for r in body]
    assert len(keys) == len(set(keys)), f"duplicate rows in the export: {keys}"

    values = {r[1]: float(r[2]) for r in body}
    assert values == {"total_assets": 1500.0, "total_equity": 600.0}


def test_the_csv_and_the_api_return_the_same_figures(client):
    """The property that actually matters to a buyer, asserted directly."""
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_assets", 1500.0, filed=dt.date(2026, 5, 1), restated=True),
        fact("total_liabilities", 900.0, filed=dt.date(2026, 5, 1)),
        fact("total_equity", 600.0, filed=dt.date(2026, 5, 1)),
    ])
    from src.company.balancesheet import get_balance_sheet
    from src.dataset import iter_csv

    csv_rows = {
        ln.split(",")[1]: float(ln.split(",")[2])
        for ln in "".join(iter_csv()).strip().splitlines()[1:]
    }
    sheet = get_balance_sheet("ACME")

    assert csv_rows["total_assets"] == sheet.assets["total_assets"].value
    assert csv_rows["total_liabilities"] == sheet.liabilities["total_liabilities"].value
    assert csv_rows["total_equity"] == sheet.equity["shareholders_equity"].value


def test_a_same_day_tie_picks_the_same_source_in_the_csv_and_the_api(client):
    """The export's tie-break was the INVERSE of the read path's.

    `iter_csv` keeps the last row of each run and ordered source rank
    descending, so `sec` came first and `yahoo` last -- and the least-preferred
    source was the one written. `balancesheet._recency` does the opposite:
    `max()` over `(filing_date, -rank)`, which picks `sec`.

    The restatement case hid it, because two different filing dates settle the
    order before source rank is ever consulted. It only appears when two
    sources file the SAME day, which is exactly the case this seeds: the API
    returned 200 and the CSV wrote 999, for one company, one metric, one
    period, on a site whose entire claim is that the file and the API are the
    same numbers.
    """
    same_day = dt.date(2026, 2, 1)
    seed([
        fact("total_assets", 200.0, filed=same_day, source="sec"),
        fact("total_assets", 999.0, filed=same_day, source="yahoo"),
        fact("total_liabilities", 120.0, filed=same_day, source="sec"),
        fact("total_equity", 80.0, filed=same_day, source="sec"),
    ])
    from src.company.balancesheet import get_balance_sheet
    from src.dataset import iter_csv

    body = [ln.split(",") for ln in "".join(iter_csv()).strip().splitlines()[1:]]
    assets = [r for r in body if r[1] == "total_assets"]

    assert len(assets) == 1, f"a tie must still collapse to one row: {assets}"
    assert assets[0][6] == "sec", "the preferred source must be the one written"
    assert float(assets[0][2]) == 200.0

    sheet = get_balance_sheet("ACME")
    assert float(assets[0][2]) == sheet.assets["total_assets"].value, (
        "the CSV and the API must agree on a same-day tie, not just on a "
        "restatement"
    )


def test_the_download_carries_its_provenance_on_the_response(client):
    """A file sold on its accuracy should say what was done to it. On the
    HEADERS, not as a `#` line inside the CSV -- a leading comment row makes
    `pd.read_csv(path)` take it as the header, and this is sold to people
    whose first move is exactly that."""
    from src.dataset import PROVENANCE

    assert "A = L + E" in PROVENANCE["X-Dataset-Reconciliation"]
    assert "99.9%" in PROVENANCE["X-Dataset-Reconciliation"]
    assert "latest filing wins" in PROVENANCE["X-Dataset-Grain"]


# ---------------------------------------------------------------------------
# 4. The drawing that did not balance and did not say so
# ---------------------------------------------------------------------------

def test_a_filing_that_does_not_balance_says_so_on_the_drawing(client):
    """Assets 1000 against claims of 1800 rendered happily, claims column at
    180% of the assets column, `notes: []`. On a site whose entire argument is
    that the two columns are the same money counted twice, that is the one
    picture that must never be drawn silently."""
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_liabilities", 900.0, filed=dt.date(2026, 2, 1)),
        fact("total_equity", 900.0, filed=dt.date(2026, 2, 1)),
    ])
    from src.company.view1 import build_view1

    view = build_view1("ACME")
    assert view is not None
    d = view.as_dict()

    assert d["balances"] is False
    assert d["imbalance_pct"] == pytest.approx(80.0, abs=0.1)
    assert d["claims_span_pct"] <= 100.0, "claims drawn past the assets column"
    assert any("does not balance" in n for n in d["notes"])
    assert any("as reported" in n for n in d["notes"])


def test_a_balanced_filing_carries_no_warning(client):
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_liabilities", 600.0, filed=dt.date(2026, 2, 1)),
        fact("total_equity", 400.0, filed=dt.date(2026, 2, 1)),
    ])
    from src.company.view1 import build_view1

    d = build_view1("ACME").as_dict()
    assert d["balances"] is True
    assert not any("does not balance" in n for n in d["notes"])


def test_negative_equity_is_not_called_an_imbalance(client):
    """A real shape this site draws deliberately below the baseline. It
    balances; it just does not look like it."""
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_liabilities", 1200.0, filed=dt.date(2026, 2, 1)),
        fact("total_equity", -200.0, filed=dt.date(2026, 2, 1)),
    ])
    from src.company.view1 import build_view1

    d = build_view1("ACME").as_dict()
    assert d["balances"] is True
    assert d["negative_equity"] is True


def test_the_warning_reaches_the_rendered_page(client):
    seed([
        fact("total_assets", 1000.0, filed=dt.date(2026, 2, 1)),
        fact("total_liabilities", 900.0, filed=dt.date(2026, 2, 1)),
        fact("total_equity", 900.0, filed=dt.date(2026, 2, 1)),
    ])
    html = client.get("/company/ACME").text

    assert "does not balance" in html
    assert 'class="note warn"' in html, "the warning renders as a note, not a warning"
