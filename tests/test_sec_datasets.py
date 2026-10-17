"""SEC bulk Financial Statement Data Sets parser.

Built against the documented num.txt/sub.txt schema (confirmed by reading
edgartools). The live download is verified on the deploy; here we exercise the
parser + extractors against a synthetic ZIP in the real tab-separated layout."""

from __future__ import annotations

import datetime as dt
import io
import zipfile

from src.ingest import sec_datasets as ds
from src.ingest import xbrl


def _zip(sub_rows: list[dict], num_rows: list[dict]) -> bytes:
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


SUB = [
    {"adsh": "a1", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
     "period": "20240630", "filed": "20240801", "fp": "Q3"},
    {"adsh": "a2", "cik": "1045810", "name": "NVIDIA CORP", "form": "10-K",
     "period": "20240131", "filed": "20240221", "fp": "FY"},
]
NUM = [
    {"adsh": "a1", "tag": "Revenues", "version": "us-gaap/2024", "ddate": "20240630",
     "qtrs": "1", "uom": "USD", "value": "85000000000"},
    {"adsh": "a1", "tag": "EarningsPerShareDiluted", "version": "us-gaap/2024",
     "ddate": "20240630", "qtrs": "1", "uom": "USD/shares", "value": "1.40"},
    {"adsh": "a2", "tag": "NetIncomeLoss", "version": "us-gaap/2024", "ddate": "20240131",
     "qtrs": "4", "uom": "USD", "value": "29760000000"},
    # A tag we don't map -> ignored.
    {"adsh": "a1", "tag": "SomeUnmappedTag", "version": "us-gaap/2024", "ddate": "20240630",
     "qtrs": "1", "uom": "USD", "value": "1"},
]
CIK_MAP = {"320193": "AAPL", "1045810": "NVDA"}


def test_parse_dataset_reads_sub_and_num():
    sub, num = ds.parse_dataset(_zip(SUB, NUM))
    assert {"adsh", "cik", "form", "period", "filed"}.issubset(sub.columns)
    assert {"adsh", "tag", "ddate", "value"}.issubset(num.columns)
    assert len(sub) == 2 and len(num) == 4


def test_extract_earnings_from_periodic_filings():
    sub, num = ds.parse_dataset(_zip(SUB, NUM))
    ev = {e["ticker"]: e for e in ds.extract_earnings(sub, num, CIK_MAP)}
    assert set(ev) == {"AAPL", "NVDA"}
    assert ev["AAPL"]["report_date"] == dt.date(2024, 8, 1)
    assert ev["AAPL"]["period_end"] == dt.date(2024, 6, 30)
    assert ev["AAPL"]["actual_eps"] == 1.40
    # Consensus/surprise are never fabricated.
    assert ev["AAPL"]["consensus_eps"] is None and ev["AAPL"]["surprise_pct"] is None


def test_unmapped_ciks_are_dropped_not_guessed():
    sub, num = ds.parse_dataset(_zip(SUB, NUM))
    rows, _ = xbrl.extract_facts(sub, num, {"320193": "AAPL"})  # NVDA missing
    assert {r["ticker"] for r in rows} == {"AAPL"}


def test_bad_zip_raises_loudly():
    import pytest

    with pytest.raises(RuntimeError):
        ds.parse_dataset(b"not a zip")


def test_recent_quarters_are_past_and_ordered():
    qs = ds.recent_quarters(dt.date(2025, 8, 2), 4)
    assert len(qs) == 4
    # newest first, all strictly before the current quarter (2025 Q3)
    assert qs[0] == (2025, 2)  # current is 2025Q3 -> most recent published is 2025Q2
    for (_y, q) in qs:
        assert 1 <= q <= 4


# ---------------------------------------------------------------------------
# Duplicate facts. num.txt repeats the same (ticker, metric, period_end) via
# (a) several XBRL tags mapping to one metric and (b) amended filings. Postgres
# aborts the whole upsert on a duplicate ("cannot affect row a second time"), so
# both must be collapsed before the insert.
# ---------------------------------------------------------------------------
SUB_DUP = [
    # Original 10-Q, then an amendment restating the SAME period, filed later.
    {"adsh": "orig", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q",
     "period": "20240630", "filed": "20240801", "fp": "Q3"},
    {"adsh": "amend", "cik": "0000320193", "name": "APPLE INC", "form": "10-Q/A",
     "period": "20240630", "filed": "20241115", "fp": "Q3"},
]
NUM_DUP = [
    # ONE filing reporting revenue under two different tags -> identical key.
    {"adsh": "orig", "tag": "Revenues", "version": "us-gaap/2024",
     "ddate": "20240630", "qtrs": "1", "uom": "USD", "value": "85000000000"},
    {"adsh": "orig", "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
     "version": "us-gaap/2024", "ddate": "20240630", "qtrs": "1", "uom": "USD",
     "value": "85100000000"},
    # The amendment restates the same period with a different number.
    {"adsh": "amend", "tag": "Revenues", "version": "us-gaap/2024",
     "ddate": "20240630", "qtrs": "1", "uom": "USD", "value": "99900000000"},
]


def test_tag_aliases_collapse_to_one_row_per_filing():
    """Two tags for one metric in ONE filing must not emit two identical-key
    rows; the tag listed first in xbrl.CONCEPTS wins, deterministically."""
    sub, num = ds.parse_dataset(_zip(SUB_DUP, NUM_DUP))
    rows, _ = xbrl.extract_facts(sub, num, CIK_MAP)
    orig = [r for r in rows if r["filing_date"] == dt.date(2024, 8, 1)
            and r["metric"] == "revenue"]
    assert len(orig) == 1, f"tag aliases produced {len(orig)} rows for one filing"
    # RevenueFromContractWithCustomer... is listed first -> preferred.
    assert orig[0]["value"] == 85_100_000_000.0


def test_natural_key_is_unique_after_extract():
    """The invariant the Postgres upsert needs: no two rows share the full
    natural key inside one extraction."""
    sub, num = ds.parse_dataset(_zip(SUB_DUP, NUM_DUP))
    rows, _ = xbrl.extract_facts(sub, num, CIK_MAP)
    keys = [(r["ticker"], r["metric"], r["period_end"], r["source"], r["filing_date"])
            for r in rows]
    assert len(keys) == len(set(keys))


def test_restatement_collapses_to_the_earliest_filing(tmp_path, monkeypatch):
    """End to end: extract -> collapse -> save. The amendment must NOT win; the
    as-first-reported value is what was known at the time."""
    from sqlalchemy import select

    from src.backfill import _collapse_earliest_filing
    from src.config.settings import get_settings
    from src.storage import repository
    from src.storage.db import init_db, reset_engine_cache, session_scope
    from src.storage.models import Fundamental

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'd.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        sub, num = ds.parse_dataset(_zip(SUB_DUP, NUM_DUP))
        facts, _ = xbrl.extract_facts(sub, num, CIK_MAP)
        rows = _collapse_earliest_filing(facts)
        with session_scope() as s:
            repository.save_fundamentals(s, rows)
        with session_scope() as s:
            got = s.execute(
                select(Fundamental.value, Fundamental.filing_date)
                .where(Fundamental.metric == "revenue")
            ).all()
        assert len(got) == 1
        assert got[0][1] == dt.date(2024, 8, 1), "kept the amendment, not the original"
        assert got[0][0] == 85_100_000_000.0
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


# ---------------------------------------------------------------------------
# Hooker Furnishings (HOFT), 10-Q for the quarter ended 2026-08-02. Its income
# statement uses RevenueFromContractWithCustomerIncludingAssessedTax, which was
# not in the revenue tag list: bulk data dropped the line and the frames route
# read a -$522,000 `Revenues` line instead of $63,250,000.
# ---------------------------------------------------------------------------
SUB_HOFT = [
    {"adsh": "h1", "cik": "0000320193", "name": "HOOKER FURNISHINGS", "form": "10-Q",
     "period": "20260731", "filed": "20260911", "fp": "Q2"},
]


def _hoft_num(*tags_and_values):
    return [
        {"adsh": "h1", "tag": tag, "version": "us-gaap/2025", "ddate": "20260731",
         "qtrs": "1", "uom": "USD", "value": value}
        for tag, value in tags_and_values
    ]


def test_a_filer_that_reports_revenue_including_assessed_tax_gets_a_revenue_row():
    num_tag = "RevenueFromContractWithCustomerIncludingAssessedTax"
    sub, num = ds.parse_dataset(_zip(SUB_HOFT, _hoft_num((num_tag, "63250000"))))
    rows, _ = xbrl.extract_facts(sub, num, CIK_MAP)
    revenue = [r for r in rows if r["metric"] == "revenue"]
    assert [(r["value"], r["source_tag"]) for r in revenue] == [(63_250_000.0, num_tag)], (
        "revenue was dropped: the tag is missing from xbrl.CONCEPTS")


def test_the_including_assessed_tax_tag_outranks_the_plain_revenues_line():
    """Both on one filing: the ASC 606 revenue line beats the generic `Revenues`
    line, as the Excluding variant already does (HOFT's `Revenues` is a -$522,000
    adjustment, not its revenue)."""
    sub, num = ds.parse_dataset(_zip(SUB_HOFT, _hoft_num(
        ("Revenues", "-522000"),
        ("RevenueFromContractWithCustomerIncludingAssessedTax", "63250000"),
    )))
    rows, _ = xbrl.extract_facts(sub, num, CIK_MAP)
    revenue = [r for r in rows if r["metric"] == "revenue"]
    assert [r["value"] for r in revenue] == [63_250_000.0]


def test_the_revenue_tag_order_is_excluding_then_including_then_revenues():
    c = next(c for c in xbrl.CONCEPTS if c.metric == "revenue")
    assert c.tags[:3] == (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
    )
    assert xbrl._TAG_RANK["RevenueFromContractWithCustomerIncludingAssessedTax"] < xbrl._TAG_RANK["Revenues"]


# ---------------------------------------------------------------------------
# EPS unit. In SEC's real bulk files `EarningsPerShareDiluted` is uom `USD`
# (2026q3: 24,767 rows `USD`, none `USD/shares`; the rest are foreign currencies),
# but the concept demanded `USD/shares`, so the bulk route dropped every EPS fact.
# Lines below are shaped like the real ones (Carpenter Technology 10-K, filed
# 2026-08-12, fiscal year ended 2026-06-30; General Mills, fiscal year ended
# 2026-05-31).
# ---------------------------------------------------------------------------
SUB_EPS = [
    {"adsh": "0000017843-26-000034", "cik": "0000320193", "name": "CARPENTER TECHNOLOGY CORP",
     "form": "10-K", "period": "20260630", "filed": "20260812", "fp": "FY"},
    {"adsh": "0001628280-26-046466", "cik": "1045810", "name": "GENERAL MILLS INC",
     "form": "10-K", "period": "20260531", "filed": "20260701", "fp": "FY"},
]


def _eps(adsh, ddate, qtrs, uom, value, segments=""):
    return {"adsh": adsh, "tag": "EarningsPerShareDiluted", "version": "us-gaap/2026",
            "ddate": ddate, "qtrs": qtrs, "uom": uom, "value": value, "segments": segments}


def test_bulk_eps_with_the_real_uom_usd_produces_an_annual_row():
    num = [
        _eps("0000017843-26-000034", "20260630", "4", "USD", "10.5200"),   # the fiscal year
        _eps("0000017843-26-000034", "20260630", "1", "USD", "3.2300"),    # its fourth quarter
        _eps("0001628280-26-046466", "20260531", "4", "USD", "-0.1600"),
        _eps("0001628280-26-046466", "20260531", "1", "USD", "-3.7400"),
    ]
    sub, n = ds.parse_dataset(_zip(SUB_EPS, num))
    rows, report = xbrl.extract_facts(sub, n, CIK_MAP)
    eps = {r["ticker"]: r for r in rows if r["metric"] == "eps_diluted"}
    assert len(eps) == 2, "EPS was dropped as a non-USD unit"
    # The year wins over its fourth quarter (same key, quarter ranks after the year).
    assert sorted(r["value"] for r in eps.values()) == [-0.16, 10.52]
    assert all(r["fiscal_period"] == "FY" for r in eps.values())
    assert report.dropped_non_usd == 0


def test_bulk_eps_labelled_usd_per_shares_is_still_accepted():
    sub, n = ds.parse_dataset(_zip(SUB_EPS[:1], [_eps("0000017843-26-000034", "20260630", "4", "USD/shares", "10.5200")]))
    rows, _ = xbrl.extract_facts(sub, n, CIK_MAP)
    assert [r["value"] for r in rows if r["metric"] == "eps_diluted"] == [10.52]


def test_eps_in_a_foreign_currency_is_still_refused():
    num = [_eps("0000017843-26-000034", "20260630", "4", cur, "9.9000") for cur in ("CNY", "CAD", "HKD")]
    sub, n = ds.parse_dataset(_zip(SUB_EPS[:1], num))
    rows, report = xbrl.extract_facts(sub, n, CIK_MAP)
    assert [r for r in rows if r["metric"] == "eps_diluted"] == []
    assert report.dropped_non_usd == 3


def test_a_dimensional_eps_row_is_still_not_the_companys_eps():
    """TWIN-style restatement scenarios carry the same tag with a segments value."""
    num = [
        _eps("0000017843-26-000034", "20260630", "4", "USD", "9.0000", segments="Restatement=ScenarioPreviouslyReported;"),
        _eps("0000017843-26-000034", "20260630", "4", "USD", "10.5200"),
    ]
    sub, n = ds.parse_dataset(_zip(SUB_EPS[:1], num))
    rows, _ = xbrl.extract_facts(sub, n, CIK_MAP)
    assert [r["value"] for r in rows if r["metric"] == "eps_diluted"] == [10.52]
