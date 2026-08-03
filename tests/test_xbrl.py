"""XBRL extraction: the consolidated-instant filter and the validation layer.

The fixtures below reproduce the exact shapes that produced wrong numbers in
production, taken from a real num.txt dump for JPM (CIK 0000019617, period
2025-12-31):

  Assets              23 rows, 1 consolidated. Correct: $4,424,900,000,000.
                      Row 1 was segments=Geographical=EMEA -> $641,190,000,000,
                      which is what the old parser stored as the company total.
  StockholdersEquity  14 rows, 1 consolidated. Correct: $362,438,000,000.
                      Row 1 was segments=EquityComponents=Accumulated
                      GainLossNetCashFlowHedgeParent -> -$1,426,000,000, one
                      line of the equity rollforward.

Both cases: first row wins, no dimensional filter. Every test here fails against
the old extractor and passes against the new one.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile

import pandas as pd
import pytest

from src.ingest import sec_datasets as ds
from src.ingest import xbrl

NUM_COLS = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom", "segments", "value"]
SUB_COLS = ["adsh", "cik", "name", "form", "period", "filed", "fp"]

JPM_SUB = [
    {"adsh": "j1", "cik": "0000019617", "name": "JPMORGAN CHASE & CO",
     "form": "10-K", "period": "20251231", "filed": "20260213", "fp": "FY"},
]
CIK_MAP = {"19617": "JPM"}

# Real consolidated figures from the dump.
JPM_ASSETS = 4_424_900_000_000.0
JPM_EQUITY = 362_438_000_000.0
# What the old parser stored instead.
JPM_EMEA_ASSETS = 641_190_000_000.0
JPM_HEDGE_COMPONENT = -1_426_000_000.0


def _num(**kw) -> dict:
    row = {c: "" for c in NUM_COLS}
    row.update(adsh="j1", version="us-gaap/2025", ddate="20251231", uom="USD")
    row.update(kw)
    return row


def _df(rows: list[dict], cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{c: str(r.get(c, "")) for c in cols} for r in rows], columns=cols)


def _zip(sub_rows: list[dict], num_rows: list[dict]) -> bytes:
    def tsv(cols, rows):
        out = ["\t".join(cols)]
        for r in rows:
            out.append("\t".join(str(r.get(c, "")) for c in cols))
        return "\n".join(out)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub.txt", tsv(SUB_COLS, sub_rows))
        z.writestr("num.txt", tsv(NUM_COLS, num_rows))
    return buf.getvalue()


# --------------------------------------------------------------- rule one
def test_segment_row_does_not_become_the_company_total():
    """The JPM Assets bug: EMEA assets read as total assets."""
    num = _df(
        [
            # Row 1 in file order, exactly as in the dump: a geographic segment.
            _num(tag="Assets", qtrs="0", segments="Geographical=EMEA",
                 value=str(JPM_EMEA_ASSETS)),
            _num(tag="Assets", qtrs="0", segments="BusinessSegments=ConsumerBanking",
                 value="1200000000000"),
            _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),  # consolidated
        ],
        NUM_COLS,
    )
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)

    assets = [r for r in rows if r["metric"] == "total_assets"]
    assert len(assets) == 1, "exactly one consolidated Assets fact must survive"
    assert assets[0]["value"] == JPM_ASSETS
    assert report.dropped_dimensional == 2


def test_equity_component_does_not_become_shareholders_equity():
    """The JPM equity bug: a hedge-accounting rollforward line read as equity."""
    num = _df(
        [
            _num(tag="StockholdersEquity", qtrs="0",
                 segments="EquityComponents=AccumulatedGainLossNetCashFlowHedgeParent",
                 value=str(JPM_HEDGE_COMPONENT)),
            _num(tag="StockholdersEquity", qtrs="0",
                 segments="EquityComponents=CommonStockMember", value="4105000000"),
            _num(tag="StockholdersEquity", qtrs="0", value=str(JPM_EQUITY)),
        ],
        NUM_COLS,
    )
    rows, _ = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)

    equity = [r for r in rows if r["metric"] == "total_equity"]
    assert len(equity) == 1
    assert equity[0]["value"] == JPM_EQUITY
    assert equity[0]["value"] > 0


def test_coreg_row_is_dimensional_too():
    """A coregistrant legal entity is not the consolidated filer."""
    num = _df(
        [
            _num(tag="Assets", qtrs="0", coreg="JPMCB", value="3100000000000"),
            _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),
        ],
        NUM_COLS,
    )
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert [r["value"] for r in rows if r["metric"] == "total_assets"] == [JPM_ASSETS]
    assert report.dropped_dimensional == 1


def test_balance_sheet_duration_row_is_rejected():
    """A nonzero qtrs on Assets is a change over a period, not a balance.

    This is the shape that can produce a negative total.
    """
    num = _df(
        [
            _num(tag="Assets", qtrs="4", value="-20400000000"),
            _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),
        ],
        NUM_COLS,
    )
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert [r["value"] for r in rows if r["metric"] == "total_assets"] == [JPM_ASSETS]
    assert report.dropped_wrong_qtrs == 1


def test_income_statement_instant_row_is_rejected():
    """Revenue is a duration; a qtrs=0 revenue fact is not a period's revenue."""
    num = _df(
        [
            _num(tag="Revenues", qtrs="0", value="999"),
            _num(tag="Revenues", qtrs="4", value="177600000000"),
        ],
        NUM_COLS,
    )
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert [r["value"] for r in rows if r["metric"] == "revenue"] == [177_600_000_000.0]
    assert report.dropped_wrong_qtrs == 1


def test_non_usd_fact_is_dropped():
    num = _df(
        [
            _num(tag="Assets", qtrs="0", uom="EUR", value="1"),
            _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),
        ],
        NUM_COLS,
    )
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert [r["value"] for r in rows if r["metric"] == "total_assets"] == [JPM_ASSETS]
    assert report.dropped_non_usd == 1


def test_alias_preference_is_deterministic():
    """Two aliases of one metric in one filing collapse to the preferred tag."""
    num = _df(
        [
            _num(tag="IntangibleAssetsNet", qtrs="0", value="200"),
            _num(tag="IntangibleAssetsNetExcludingGoodwill", qtrs="0", value="100"),
            _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),
        ],
        NUM_COLS,
    )
    rows, _ = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    intangibles = [r for r in rows if r["metric"] == "intangibles"]
    assert len(intangibles) == 1
    # IntangibleAssetsNetExcludingGoodwill is listed first, so it wins regardless
    # of which appeared first in the file.
    assert intangibles[0]["value"] == 100.0


def test_pit_pair_is_period_end_and_filing_date():
    num = _df([_num(tag="Assets", qtrs="0", value=str(JPM_ASSETS))], NUM_COLS)
    rows, _ = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert rows[0]["period_end"] == dt.date(2025, 12, 31)
    assert rows[0]["filing_date"] == dt.date(2026, 2, 13)
    assert rows[0]["fiscal_period"] == "FY"
    assert rows[0]["source"] == "sec"


def test_missing_dimension_columns_is_a_hard_error():
    """Without coreg/segments we cannot filter, so refuse rather than guess."""
    bare = ["adsh", "tag", "ddate", "qtrs", "uom", "value"]
    num = _df([{"adsh": "j1", "tag": "Assets", "ddate": "20251231",
                "qtrs": "0", "uom": "USD", "value": "1"}], bare)
    with pytest.raises(xbrl.ExtractionError, match="coreg"):
        xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)


# --------------------------------------------------------------- type coercion
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "", "abc", None])
def test_coerce_float_rejects_non_finite(bad):
    assert xbrl.coerce_float(bad) is None


def test_coerce_float_accepts_real_numbers():
    assert xbrl.coerce_float("4424900000000") == JPM_ASSETS
    assert xbrl.coerce_float(-1.5) == -1.5
    assert xbrl.coerce_float(0) == 0.0


def test_nan_value_never_reaches_a_row():
    num = _df([_num(tag="Assets", qtrs="0", value="nan")], NUM_COLS)
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert rows == []
    assert report.dropped_unparseable == 1


# --------------------------------------------------------------- rule two
def _row(metric: str, value: float, ticker: str = "JPM") -> dict:
    return {
        "ticker": ticker, "metric": metric, "value": value,
        "period_end": dt.date(2025, 12, 31), "fiscal_period": "FY",
        "filing_date": dt.date(2026, 2, 13), "source": "sec", "restated": False,
    }


def test_negative_total_assets_rejects_the_whole_period():
    """FCX's impossible shape: a negative total. The period is untrustworthy."""
    report = xbrl.ExtractionReport()
    kept = xbrl.validate_facts(
        [_row("total_assets", -20_400_000_000.0), _row("cash", 100.0)], report
    )
    assert kept == []
    assert report.rejected_periods == 1
    assert report.rejections[0]["rule"] == "total_assets_not_positive"


def test_zero_total_assets_rejects_the_whole_period():
    report = xbrl.ExtractionReport()
    assert xbrl.validate_facts([_row("total_assets", 0.0)], report) == []
    assert report.rejected_periods == 1


def test_component_larger_than_total_assets_is_dropped():
    report = xbrl.ExtractionReport()
    kept = xbrl.validate_facts(
        [_row("total_assets", 1_000.0), _row("goodwill", 5_000.0), _row("cash", 10.0)],
        report,
    )
    metrics = {r["metric"] for r in kept}
    assert "goodwill" not in metrics
    assert metrics == {"total_assets", "cash"}
    assert report.rejections[0]["rule"] == "component_exceeds_total_assets"
    # A dropped component is not a rejected period -- the rest is still good.
    assert report.rejected_periods == 0


def test_balance_identity_drift_is_flagged_not_dropped():
    report = xbrl.ExtractionReport()
    kept = xbrl.validate_facts(
        [
            _row("total_assets", 1_000.0),
            _row("total_liabilities", 400.0),
            _row("total_equity", 100.0),  # 500 != 1000
        ],
        report,
    )
    assert len(kept) == 3, "a drifting identity is reported, never silently deleted"
    assert report.flags[0]["rule"] == "balance_identity_drift"
    assert report.flags[0]["drift_pct"] == 50.0


def test_balance_identity_within_tolerance_is_not_flagged():
    report = xbrl.ExtractionReport()
    xbrl.validate_facts(
        [
            _row("total_assets", 1_000.0),
            _row("total_liabilities", 700.0),
            _row("total_equity", 305.0),  # 1005, 0.5% off
        ],
        report,
    )
    assert report.flags == []


def test_scale_jump_is_flagged():
    report = xbrl.ExtractionReport()
    a = _row("total_assets", 1_000.0)
    b = _row("total_assets", 500_000.0)
    b["period_end"] = dt.date(2026, 3, 31)
    xbrl.validate_facts([a, b], report)
    jumps = [f for f in report.flags if f["rule"] == "scale_jump"]
    assert len(jumps) == 1
    assert jumps[0]["ratio"] == 500.0


def test_high_rejection_rate_fails_the_load():
    """If most periods are bad the parser is wrong; do not write partial data."""
    report = xbrl.ExtractionReport()
    n = xbrl.MIN_PERIODS_FOR_RATE_CHECK + 10
    rows = [_row("total_assets", -1.0, ticker=f"T{i}") for i in range(n)]
    with pytest.raises(xbrl.ExtractionError, match="refusing to write partial data"):
        xbrl.validate_facts(rows, report)


def test_a_few_bad_periods_do_not_fail_the_load():
    """5% bad over a real-sized sample is survivable; those rows just drop."""
    report = xbrl.ExtractionReport()
    rows = [_row("total_assets", -1.0, ticker=f"BAD{i}") for i in range(5)]
    rows += [_row("total_assets", 1_000.0, ticker=f"OK{i}") for i in range(95)]
    kept = xbrl.validate_facts(rows, report)
    assert report.validated_periods == 100
    assert report.rejected_periods == 5
    assert len({r["ticker"] for r in kept}) == 95


def test_a_broken_quarter_at_real_scale_still_fails_the_load():
    """The floor must not let a genuinely broken quarter through.

    A real quarter carries thousands of company-periods, so the 50-period floor
    is never the binding constraint there. 30% bad out of 1000 must abort.
    """
    report = xbrl.ExtractionReport()
    rows = [_row("total_assets", -1.0, ticker=f"BAD{i}") for i in range(300)]
    rows += [_row("total_assets", 1_000.0, ticker=f"OK{i}") for i in range(700)]
    with pytest.raises(xbrl.ExtractionError, match="refusing to write partial data"):
        xbrl.validate_facts(rows, report)


def test_the_floor_binds_only_below_its_threshold():
    """Exactly at the floor, a >20% rate still aborts -- the floor is a minimum
    denominator, not an exemption."""
    n = xbrl.MIN_PERIODS_FOR_RATE_CHECK
    bad = int(n * 0.3)
    rows = [_row("total_assets", -1.0, ticker=f"BAD{i}") for i in range(bad)]
    rows += [_row("total_assets", 1_000.0, ticker=f"OK{i}") for i in range(n - bad)]
    with pytest.raises(xbrl.ExtractionError):
        xbrl.validate_facts(rows, xbrl.ExtractionReport())

    # One period fewer and the rate is not yet trustworthy, so it is not applied.
    fewer = rows[:-1]
    kept = xbrl.validate_facts(fewer, xbrl.ExtractionReport())
    assert kept, "below the floor the load proceeds, dropping only the bad rows"


# --------------------------------------------------------------- qtrs discipline
def test_cumulative_ytd_durations_are_dropped_and_counted():
    """A Q3 10-Q reports both the 3-month figure (qtrs=1) and the 9-month
    cumulative one (qtrs=3). Reading the latter as quarterly would triple the
    quarter. Dropped -- and counted separately, because it is a judgement call.
    """
    num = _df(
        [
            _num(tag="Revenues", qtrs="1", value="1000"),   # the real quarter
            _num(tag="Revenues", qtrs="2", value="2000"),   # 6-month cumulative
            _num(tag="Revenues", qtrs="3", value="3000"),   # 9-month cumulative
        ],
        NUM_COLS,
    )
    rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)

    assert [r["value"] for r in rows if r["metric"] == "revenue"] == [1000.0]
    assert report.dropped_ytd_cumulative == 2
    assert report.dropped_wrong_qtrs == 2


def test_duration_qtrs_are_histogrammed_for_auditing():
    """The load reports what filers actually use, so the {1,4} choice is
    checkable against the real file instead of taken on faith."""
    num = _df(
        [
            _num(tag="Revenues", qtrs="1", value="1"),
            _num(tag="Revenues", qtrs="1", value="2"),
            _num(tag="Revenues", qtrs="3", value="3"),
            _num(tag="Revenues", qtrs="4", value="4"),
            # Instants must not pollute the duration histogram.
            _num(tag="Assets", qtrs="0", value="5"),
        ],
        NUM_COLS,
    )
    _rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert report.duration_qtrs_seen == {"1": 2, "3": 1, "4": 1}


def test_dimensional_rows_do_not_enter_the_qtrs_histogram():
    """The histogram describes consolidated facts, which is what we filter on."""
    num = _df(
        [
            _num(tag="Revenues", qtrs="2", segments="Geographical=EMEA", value="1"),
            _num(tag="Revenues", qtrs="1", value="2"),
        ],
        NUM_COLS,
    )
    _rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert report.duration_qtrs_seen == {"1": 1}
    assert report.dropped_ytd_cumulative == 0


def test_rate_check_needs_a_real_denominator():
    """One bad filer in a tiny load must not abort it -- the rate is meaningless.

    A five-company verification load hitting one bad filer is not evidence that
    the parser is broken, and aborting there would make targeted checks
    impossible.
    """
    report = xbrl.ExtractionReport()
    kept = xbrl.validate_facts(
        [_row("total_assets", -1.0, ticker="BAD"),
         _row("total_assets", 1_000.0, ticker="GOOD")],
        report,
    )
    assert report.rejected_periods == 1
    assert [r["ticker"] for r in kept] == ["GOOD"]


# --------------------------------------------------- unmapped-tag census
def test_unmapped_consolidated_tags_are_counted():
    """Thin coverage is ambiguous without this.

    A concept at 31% could mean "few filers report it" or "they report it under
    a tag we don't map". Those need opposite fixes, so the tags we discard are
    counted rather than silently dropped.
    """
    num = _df(
        [
            _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),
            _num(tag="LongTermDebtCurrent", qtrs="0", value="500"),
            _num(tag="LongTermDebtCurrent", qtrs="0", value="600"),
            _num(tag="SomethingNobodyReads", qtrs="0", value="1"),
        ],
        NUM_COLS,
    )
    _rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)

    assert report.unmapped_tags["LongTermDebtCurrent"] == 2
    assert report.unmapped_tags["SomethingNobodyReads"] == 1
    # A tag we DO map must never appear in the census.
    assert "Assets" not in report.unmapped_tags

    top = report.top_unmapped()
    assert top[0] == {"tag": "LongTermDebtCurrent", "count": 2}


def test_census_ignores_dimensional_rows():
    """Segment breakdowns would swamp the tally and misrepresent the gap."""
    num = _df(
        [
            _num(tag="UnmappedThing", qtrs="0", segments="Geographical=EMEA", value="1"),
            _num(tag="UnmappedThing", qtrs="0", coreg="SUB", value="2"),
            _num(tag="UnmappedThing", qtrs="0", value="3"),
        ],
        NUM_COLS,
    )
    _rows, report = xbrl.extract_facts(_df(JPM_SUB, SUB_COLS), num, CIK_MAP)
    assert report.unmapped_tags == {"UnmappedThing": 1}


# --------------------------------------------------------------- end to end
def test_extract_and_validate_produces_the_real_jpm_numbers():
    """The whole path against the dump's row shapes."""
    num_rows = [
        _num(tag="Assets", qtrs="0", segments="Geographical=EMEA",
             value=str(JPM_EMEA_ASSETS)),
        _num(tag="Assets", qtrs="0", value=str(JPM_ASSETS)),
        _num(tag="StockholdersEquity", qtrs="0",
             segments="EquityComponents=AccumulatedGainLossNetCashFlowHedgeParent",
             value=str(JPM_HEDGE_COMPONENT)),
        _num(tag="StockholdersEquity", qtrs="0", value=str(JPM_EQUITY)),
        _num(tag="Liabilities", qtrs="0", value=str(JPM_ASSETS - JPM_EQUITY)),
    ]
    sub, num = ds.parse_dataset(_zip(JPM_SUB, num_rows))
    rows, report = xbrl.extract_and_validate(sub, num, CIK_MAP)

    by_metric = {r["metric"]: r["value"] for r in rows}
    assert by_metric["total_assets"] == JPM_ASSETS
    assert by_metric["total_equity"] == JPM_EQUITY
    assert report.rejected_periods == 0
    # Assets == Liabilities + Equity by construction here, so no drift flag.
    assert [f for f in report.flags if f["rule"] == "balance_identity_drift"] == []


def test_parse_dataset_keeps_the_dimensional_columns():
    """The root defect: these columns were never read, so filtering was impossible."""
    _sub, num = ds.parse_dataset(_zip(JPM_SUB, [_num(tag="Assets", qtrs="0", value="1")]))
    for col in xbrl.DIMENSION_COLUMNS:
        assert col in num.columns


def test_parse_dataset_rejects_a_file_with_no_dimension_columns():
    def tsv(cols, rows):
        out = ["\t".join(cols)]
        for r in rows:
            out.append("\t".join(str(r.get(c, "")) for c in cols))
        return "\n".join(out)

    bare = ["adsh", "tag", "ddate", "qtrs", "uom", "value"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub.txt", tsv(SUB_COLS, JPM_SUB))
        z.writestr("num.txt", tsv(bare, [{"adsh": "j1", "tag": "Assets", "ddate": "20251231",
                                          "qtrs": "0", "uom": "USD", "value": "1"}]))
    with pytest.raises(RuntimeError, match="dimensional"):
        ds.parse_dataset(buf.getvalue())
