"""Tests for the frames path -- filings loaded as they are filed.

The risk in this module is not that it errors, it is that it QUIETLY produces
plausible-looking rows with the wrong filing date. A fundamental carrying a
made-up date is worse than a missing one: the point-in-time layer trusts it,
and a backtest reads a number weeks before it was public. So the tests below
lean on the drop paths as hard as the happy path.

The fixtures are built to the shapes confirmed against live SEC responses:
an EDGAR `form.idx` line and a frames `data` entry.
"""

from __future__ import annotations

import datetime as dt

import pytest

# One real-shaped form.idx: header block, then fixed-width rows.
FORM_IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    Aug 31, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/




Form Type   Company Name                                                  CIK       Date Filed  File Name
---------------------------------------------------------------------------------------------------------
10-K        AAR CORP                                                      1750      20260722    edgar/data/1750/0001104659-26-085459.txt
10-Q        ABBOTT LABORATORIES                                           1800      20260728    edgar/data/1800/0001628280-26-050134.txt
10-Q        JPMORGAN CHASE & CO                                           19617     20260806    edgar/data/19617/0001628280-26-054343.txt
10-Q/A      SOME AMENDER INC                                              2222      20260810    edgar/data/2222/0001111111-26-000001.txt
8-K         NOT A PERIODIC REPORT                                         3333      20260811    edgar/data/3333/0002222222-26-000002.txt
4           ALSO NOT PERIODIC                                             4444      20260812    edgar/data/4444/0003333333-26-000003.txt
"""


def _frame(rows):
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# The form index
# ---------------------------------------------------------------------------
def test_form_index_keeps_only_periodic_reports():
    from src.ingest.sec_frames import parse_form_index

    idx = parse_form_index(FORM_IDX)
    assert set(idx) == {
        "0001104659-26-085459",
        "0001628280-26-050134",
        "0001628280-26-054343",
        "0001111111-26-000001",
    }
    # An 8-K and a Form 4 have no balance sheet to date.
    assert "0002222222-26-000002" not in idx
    assert "0003333333-26-000003" not in idx


def test_form_index_reads_the_filing_date_and_cik():
    from src.ingest.sec_frames import parse_form_index

    entry = parse_form_index(FORM_IDX)["0001628280-26-054343"]
    assert entry == {"form": "10-Q", "cik": 19617, "filed": dt.date(2026, 8, 6)}


def test_form_index_survives_a_header_only_file():
    from src.ingest.sec_frames import parse_form_index

    assert parse_form_index(FORM_IDX.split("10-K")[0]) == {}
    assert parse_form_index("") == {}


# ---------------------------------------------------------------------------
# Frame rows
# ---------------------------------------------------------------------------
def test_frame_rows_carry_period_end_and_alias_rank():
    from src.ingest.sec_frames import rows_from_frame

    rows = rows_from_frame(
        _frame([{
            "accn": "0001628280-26-054343", "cik": 19617,
            "entityName": "JPMORGAN CHASE & CO",
            "end": "2026-06-30", "val": 5015069000000,
        }]),
        "total_assets", 2,
    )
    assert rows == [{
        "cik": 19617,
        "accn": "0001628280-26-054343",
        "period_end": dt.date(2026, 6, 30),
        "metric": "total_assets",
        "value": 5015069000000.0,
        "tag_rank": 2,
    }]


@pytest.mark.parametrize(
    "bad",
    [
        {"cik": None, "accn": "a", "end": "2026-06-30", "val": 1},
        {"cik": 1, "accn": "", "end": "2026-06-30", "val": 1},
        {"cik": 1, "accn": "a", "end": "not-a-date", "val": 1},
        {"cik": 1, "accn": "a", "end": "2026-06-30", "val": None},
    ],
)
def test_frame_rows_drop_anything_incomplete(bad):
    from src.ingest.sec_frames import rows_from_frame

    assert rows_from_frame(_frame([bad]), "total_assets") == []


# ---------------------------------------------------------------------------
# The join -- where a wrong answer would be dangerous
# ---------------------------------------------------------------------------
def test_the_filing_date_comes_from_the_index_not_the_period():
    from src.ingest.sec_frames import join_filing_dates, parse_form_index

    idx = parse_form_index(FORM_IDX)
    rows = [{
        "cik": 19617, "accn": "0001628280-26-054343",
        "period_end": dt.date(2026, 6, 30),
        "metric": "total_assets", "value": 5015069000000.0, "tag_rank": 0,
    }]
    kept, dropped = join_filing_dates(rows, idx, {"19617": "JPM"})
    assert len(kept) == 1
    row = kept[0]
    assert row["ticker"] == "JPM"
    assert row["period_end"] == dt.date(2026, 6, 30)
    # Filed five weeks after the period closed. Using period_end here would
    # publish the number weeks before it existed.
    assert row["filing_date"] == dt.date(2026, 8, 6)
    assert row["source"] == "sec"
    assert dropped == {"no_accession_in_index": 0, "no_ticker": 0}


def test_a_row_we_cannot_date_is_dropped_not_guessed():
    from src.ingest.sec_frames import join_filing_dates, parse_form_index

    idx = parse_form_index(FORM_IDX)
    rows = [{
        "cik": 999, "accn": "0009999999-26-000999",  # in no index we hold
        "period_end": dt.date(2026, 6, 30),
        "metric": "total_assets", "value": 1.0, "tag_rank": 0,
    }]
    kept, dropped = join_filing_dates(rows, idx, {"999": "ZZZ"})
    assert kept == []
    assert dropped["no_accession_in_index"] == 1


def test_a_filer_with_no_ticker_is_dropped():
    from src.ingest.sec_frames import join_filing_dates, parse_form_index

    idx = parse_form_index(FORM_IDX)
    rows = [{
        "cik": 1800, "accn": "0001628280-26-050134",
        "period_end": dt.date(2026, 6, 30),
        "metric": "total_assets", "value": 1.0, "tag_rank": 0,
    }]
    kept, dropped = join_filing_dates(rows, idx, {})  # empty ticker map
    assert kept == []
    assert dropped["no_ticker"] == 1


# ---------------------------------------------------------------------------
# Alias collapse -- two tags for one concept must not become two rows
# ---------------------------------------------------------------------------
def test_the_preferred_alias_wins_regardless_of_arrival_order():
    from src.ingest.sec_frames import collapse_alias_rows

    base = {
        "ticker": "AAA", "metric": "minority_interest",
        "period_end": dt.date(2026, 6, 30), "filing_date": dt.date(2026, 8, 6),
        "source": "sec", "restated": False,
    }
    fallback = {**base, "value": 2.0, "tag_rank": 1}
    preferred = {**base, "value": 1.0, "tag_rank": 0}

    # Whichever concurrent fetch returned first, the preferred tag must win.
    for order in ([fallback, preferred], [preferred, fallback]):
        out = collapse_alias_rows(order)
        assert len(out) == 1
        assert out[0]["value"] == 1.0
        assert "tag_rank" not in out[0], "rank is a fetch detail, not a column"


def test_different_periods_are_not_collapsed():
    from src.ingest.sec_frames import collapse_alias_rows

    base = {
        "ticker": "AAA", "metric": "total_assets", "value": 1.0,
        "filing_date": dt.date(2026, 8, 6), "source": "sec",
        "restated": False, "tag_rank": 0,
    }
    out = collapse_alias_rows([
        {**base, "period_end": dt.date(2026, 6, 30)},
        {**base, "period_end": dt.date(2026, 3, 31)},
    ])
    assert len(out) == 2


# ---------------------------------------------------------------------------
# Earnings events
# ---------------------------------------------------------------------------
def test_one_event_per_ticker_and_filing_date_with_eps_attached():
    from src.ingest.sec_frames import earnings_from_rows

    rows = [
        {"ticker": "AAA", "metric": "total_assets", "value": 100.0,
         "period_end": dt.date(2026, 6, 30), "filing_date": dt.date(2026, 8, 6)},
        {"ticker": "AAA", "metric": "eps_diluted", "value": 2.5,
         "period_end": dt.date(2026, 6, 30), "filing_date": dt.date(2026, 8, 6)},
    ]
    events = earnings_from_rows(rows)
    assert events == [{
        "ticker": "AAA",
        "report_date": dt.date(2026, 8, 6),
        "period_end": dt.date(2026, 6, 30),
        "actual_eps": 2.5,
    }]


def test_consensus_is_never_invented():
    from src.ingest.sec_frames import earnings_from_rows

    events = earnings_from_rows([
        {"ticker": "AAA", "metric": "total_assets", "value": 1.0,
         "period_end": dt.date(2026, 6, 30), "filing_date": dt.date(2026, 8, 6)},
    ])
    # No free consensus feed exists. A fabricated 0.0 would read as a company
    # that met expectations exactly.
    assert events[0]["actual_eps"] is None
    assert "consensus_eps" not in events[0]


# ---------------------------------------------------------------------------
# Periods and URLs
# ---------------------------------------------------------------------------
def test_frame_periods_start_at_the_last_closed_quarter():
    from src.ingest.sec_frames import frame_quarters

    assert frame_quarters(dt.date(2026, 9, 1)) == [(2026, 2), (2026, 1)]
    assert frame_quarters(dt.date(2026, 1, 5)) == [(2025, 4), (2025, 3)]
    assert frame_quarters(dt.date(2026, 9, 1), back=1) == [(2026, 2)]


def test_instants_and_durations_ask_for_different_frames():
    from src.ingest.sec_frames import concept_requests

    reqs = concept_requests(2026, 2)
    by_metric = {m: (tag, uom, ccp) for m, tag, uom, ccp, _ in reqs}
    # A balance-sheet total is a point in time: the frame id ends in I.
    assert by_metric["total_assets"][2] == "CY2026Q2I"
    # A flow is measured across the quarter: no I.
    assert by_metric["revenue"][2] == "CY2026Q2"
    # Per-share figures are not USD, and a USD-only sweep would silently miss
    # every EPS in the market.
    assert by_metric["eps_diluted"][1] == "USD/shares"


def test_per_share_units_are_spelled_the_way_the_url_wants():
    from src.ingest.sec_frames import uom_path

    assert uom_path("USD") == "USD"
    assert uom_path("USD/shares") == "USD-per-shares"


def test_every_alias_of_a_concept_is_requested_with_its_rank():
    from src.ingest.sec_frames import concept_requests
    from src.ingest.xbrl import CONCEPTS

    reqs = concept_requests(2026, 2)
    assert len(reqs) == sum(len(c.tags) for c in CONCEPTS)
    ranks = {(m, tag): rank for m, tag, _, _, rank in reqs}
    multi = next(c for c in CONCEPTS if len(c.tags) > 1)
    for i, tag in enumerate(multi.tags):
        assert ranks[(multi.metric, tag)] == i
