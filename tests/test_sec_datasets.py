"""SEC bulk Financial Statement Data Sets parser.

Built against the documented num.txt/sub.txt schema (confirmed by reading
edgartools). The live download is verified on the deploy; here we exercise the
parser + extractors against a synthetic ZIP in the real tab-separated layout."""

from __future__ import annotations

import datetime as dt
import io
import zipfile

from src.ingest import sec_datasets as ds


def _zip(sub_rows: list[dict], num_rows: list[dict]) -> bytes:
    sub_cols = ["adsh", "cik", "name", "form", "period", "filed", "fp"]
    num_cols = ["adsh", "tag", "version", "ddate", "qtrs", "uom", "value"]

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


def test_extract_fundamentals_maps_tags_and_dates():
    sub, num = ds.parse_dataset(_zip(SUB, NUM))
    rows = ds.extract_fundamentals(sub, num, CIK_MAP)
    by = {(r["ticker"], r["metric"]): r for r in rows}
    assert ("AAPL", "revenue") in by
    aapl_rev = by[("AAPL", "revenue")]
    assert aapl_rev["value"] == 85_000_000_000.0
    assert aapl_rev["period_end"] == dt.date(2024, 6, 30)
    assert aapl_rev["filing_date"] == dt.date(2024, 8, 1)  # the PIT filing date
    assert aapl_rev["source"] == "sec"
    assert ("NVDA", "net_income") in by
    # The unmapped tag produced no row.
    assert all(r["metric"] != "SomeUnmappedTag" for r in rows)


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
    rows = ds.extract_fundamentals(sub, num, {"320193": "AAPL"})  # NVDA missing
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
