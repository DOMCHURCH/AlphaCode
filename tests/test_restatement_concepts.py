"""A restatement is the SAME XBRL concept reported again at a different value.

Regression for the restatements tracker comparing `LongTermDebtNoncurrent` in one
filing with `LongTermDebt` in another and calling the difference a revision.
`LongTermDebt` includes the current portion and `LongTermDebtNoncurrent` does not,
so the gap is a change of definition, not of the company's books. The two
real-world shapes below come from SEC companyfacts (checked 2026-10-06):

  Conagra (CIK 23217), period 2011-05-29 long-term debt
      10-K  filed 2011-07-19  LongTermDebtNoncurrent  2,870.3M  (also LongTermDebt 3,200M)
      10-Q  filed 2011-09-30  LongTermDebt            3,200.0M  (comparative; no Noncurrent)
  Nike (CIK 320187), period 2013-05-31 long-term debt
      10-K  filed 2013-07-23  LongTermDebtNoncurrent  1,210M    (also LongTermDebt 1,267M)
      10-K  filed 2014-07-25  LongTermDebt            1,267M    chosen only if Noncurrent absent

and the rule is general: every multi-tag metric (revenue included) is compared
within one tag.
"""

from __future__ import annotations

import datetime as dt

import pytest

PERIOD = dt.date.today() - dt.timedelta(days=200)
D1 = dt.date.today() - dt.timedelta(days=150)
D2 = D1 + dt.timedelta(days=40)
D3 = D2 + dt.timedelta(days=40)

NONCURRENT = "LongTermDebtNoncurrent"
TOTAL = "LongTermDebt"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'rc.db'}")
    from src.company import exceptions
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    exceptions.reset_cache()
    init_db()
    yield
    exceptions.reset_cache()
    get_settings.cache_clear()
    reset_engine_cache()


def _add(ticker, metric, value, filed, tag="<unset>", period=PERIOD):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    kw = {} if tag == "<unset>" else {"source_tag": tag}
    with session_scope() as s:
        s.add(Fundamental(ticker=ticker, metric=metric, value=value, period_end=period,
                          fiscal_period="Q2", filing_date=filed, source="sec", **kw))


def _feed(ticker):
    from src.company import exceptions

    exceptions.reset_cache()
    return [e for e in exceptions.all_events()
            if e["type"] == "restatement" and e["ticker"] == ticker]


def _history(ticker):
    from src.company.history import restatements

    return [e for e in restatements(ticker) if e["metric"] != "total_assets"]


# ---------------------------------------------------------------- known false positives
def test_conagra_noncurrent_then_total_is_not_a_revision(db):
    _add("CAG", "long_term_debt", 2_870_300_000, D1, NONCURRENT)
    _add("CAG", "long_term_debt", 3_200_000_000, D2, TOTAL)
    assert _feed("CAG") == []
    assert _history("CAG") == []


def test_nike_noncurrent_then_total_is_not_a_revision(db):
    _add("NKE", "long_term_debt", 1_210_000_000, D1, NONCURRENT)
    _add("NKE", "long_term_debt", 1_267_000_000, D2, TOTAL)
    assert _feed("NKE") == []
    assert _history("NKE") == []


def test_revenue_total_versus_contract_revenue_is_not_a_revision(db):
    """Berkshire-style: `Revenues` (total) vs the narrower contract-revenue tag."""
    _add("BRK", "revenue", 64_972e6, D1, "Revenues")
    _add("BRK", "revenue", 45_169e6, D2, "RevenueFromContractWithCustomerExcludingAssessedTax")
    assert _feed("BRK") == []
    assert _history("BRK") == []


# ------------------------------------------------------- true revisions must still show
def test_same_tag_long_term_debt_revision_is_reported(db):
    _add("REV", "long_term_debt", 100e6, D1, NONCURRENT)
    _add("REV", "long_term_debt", 110e6, D2, NONCURRENT)
    (e,) = _feed("REV")
    assert (e["metric"], e["previous"], e["revised"]) == ("long_term_debt", 100e6, 110e6)
    assert e["change_pct"] == pytest.approx(10.0)
    (h,) = _history("REV")
    assert (h["metric"], h["previous"], h["current"]) == ("long_term_debt", 100e6, 110e6)


def test_same_tag_total_debt_revision_is_reported(db):
    _add("REV", "long_term_debt", 3_200e6, D1, TOTAL)
    _add("REV", "long_term_debt", 3_000e6, D2, TOTAL)
    (e,) = _feed("REV")
    assert (e["previous"], e["revised"]) == (3_200e6, 3_000e6)


def test_same_tag_revenue_recast_is_reported(db):
    """Abbott/GE-style recast after a discontinued operation: same tag, new value."""
    tag = "Revenues"
    _add("ABT", "revenue", 9_807.1e6, D1, tag)
    _add("ABT", "revenue", 5_313.3e6, D2, tag)
    (e,) = _feed("ABT")
    assert e["change_pct"] == pytest.approx(-45.8, abs=0.1)


def test_single_tag_metric_without_a_stored_tag_still_compares(db):
    """Tesla Q1 2024 net income, loaded before `source_tag` existed (NULL)."""
    _add("TSLA", "net_income", 1_129e6, D1)
    _add("TSLA", "net_income", 1_390e6, D2)
    (e,) = _feed("TSLA")
    assert (e["previous"], e["revised"]) == (1_129e6, 1_390e6)
    assert e["original"] == 1_129e6


def test_single_tag_metric_compares_across_legacy_and_tagged_rows(db):
    _add("MIX", "current_assets", 100e6, D1)                # legacy, NULL tag
    _add("MIX", "current_assets", 90e6, D2, "AssetsCurrent")  # reloaded, tagged
    (e,) = _feed("MIX")
    assert (e["previous"], e["revised"]) == (100e6, 90e6)


# ------------------------------------------------------------------------- the rule itself
def test_comparison_is_per_tag_not_per_adjacent_filing(db):
    """A, B, A: the A series changed (100 -> 105); B is a different concept."""
    _add("ABA", "long_term_debt", 100e6, D1, NONCURRENT)
    _add("ABA", "long_term_debt", 120e6, D2, TOTAL)
    _add("ABA", "long_term_debt", 105e6, D3, NONCURRENT)
    (e,) = _feed("ABA")
    assert (e["previous"], e["revised"]) == (100e6, 105e6)
    assert e["original"] == 100e6
    (h,) = _history("ABA")
    assert (h["previous"], h["current"]) == (100e6, 105e6)


def test_multi_tag_metric_with_unknown_concept_is_not_compared(db):
    """Rows loaded before `source_tag` existed: concept unknown, so no claim is made."""
    _add("OLD", "long_term_debt", 2_870.3e6, D1)
    _add("OLD", "long_term_debt", 3_200e6, D2)
    assert _feed("OLD") == []
    assert _history("OLD") == []


def test_equal_rereport_is_still_not_a_revision(db):
    _add("EQ", "long_term_debt", 100e6, D1, NONCURRENT)
    _add("EQ", "long_term_debt", 100e6, D2, NONCURRENT)
    assert _feed("EQ") == []


def test_comparison_tag_rule():
    from src.ingest.xbrl import comparison_tag

    assert comparison_tag("total_assets", None) == "Assets"          # single tag: known
    assert comparison_tag("total_assets", "Anything") == "Assets"
    assert comparison_tag("long_term_debt", TOTAL) == TOTAL          # multi-tag: stored tag
    assert comparison_tag("long_term_debt", NONCURRENT) == NONCURRENT
    assert comparison_tag("long_term_debt", None) is None            # multi-tag: unknown
    assert comparison_tag("revenue", "") is None


# --------------------------------------------------------------------- the tag is stored
def test_extractor_stores_the_tag_that_supplied_the_value():
    import pandas as pd

    from src.ingest import xbrl

    sub = pd.DataFrame([{"adsh": "a1", "cik": "0000023217", "name": "CONAGRA", "form": "10-Q",
                         "period": "20110828", "filed": "20110930", "fp": "Q1"}])
    num_cols = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom", "segments", "value"]
    base = dict.fromkeys(num_cols, "")
    base.update(adsh="a1", version="us-gaap/2011", ddate="20110529", qtrs="0", uom="USD")
    only_total = pd.DataFrame([{**base, "tag": TOTAL, "value": "3200000000"}], columns=num_cols)
    rows, _ = xbrl.extract_facts(sub, only_total, {"23217": "CAG"})
    (r,) = [r for r in rows if r["metric"] == "long_term_debt"]
    assert (r["value"], r["source_tag"]) == (3_200_000_000.0, TOTAL)

    both = pd.DataFrame([{**base, "tag": TOTAL, "value": "3200000000"},
                         {**base, "tag": NONCURRENT, "value": "2870300000"}], columns=num_cols)
    rows, _ = xbrl.extract_facts(sub, both, {"23217": "CAG"})
    (r,) = [r for r in rows if r["metric"] == "long_term_debt"]
    assert (r["value"], r["source_tag"]) == (2_870_300_000.0, NONCURRENT)


def test_frames_loader_carries_the_tag_through_to_the_row():
    from src.ingest.sec_frames import (
        collapse_alias_rows,
        join_filing_dates,
        rows_from_frame,
    )

    frame = {"data": [{"accn": "0000023217-11-000057", "cik": 23217, "entityName": "CONAGRA",
                       "end": "2011-05-29", "val": 3_200_000_000}]}
    raw = rows_from_frame(frame["data"], "long_term_debt", 1, TOTAL)
    assert raw[0]["source_tag"] == TOTAL
    idx = {"0000023217-11-000057": {"cik": 23217, "filed": dt.date(2011, 9, 30), "form": "10-Q"}}
    kept, _ = join_filing_dates(raw, idx, {"23217": "CAG"})
    (row,) = collapse_alias_rows(kept)
    assert row["source_tag"] == TOTAL and "tag_rank" not in row


def test_every_concept_tag_fits_the_source_tag_column():
    """SQLite ignores varchar lengths, so this is the only place a too-narrow column
    is caught: the production reload failed on a 130-character tag in a 128 column."""
    from src.ingest.xbrl import CONCEPTS
    from src.storage.models import Fundamental

    width = Fundamental.__table__.c.source_tag.type.length
    longest = max((len(t), t) for c in CONCEPTS for t in c.tags)
    assert longest[0] <= width, f"{longest[1]} is {longest[0]} chars, column is {width}"
    # and the live column is widened to match on a database created at the old size
    from src.storage.db import _WIDENED_COLUMNS

    assert ("fundamentals", "source_tag", width) in _WIDENED_COLUMNS
