"""Stage 0 universe construction tests."""

from __future__ import annotations

import datetime as dt

import pytest

from src.universe.builder import build_universe_from_frames, is_common_stock


@pytest.mark.parametrize(
    "ticker,sec_type,expected",
    [
        ("AAPL", "CS", True),
        ("BRK.B", "CS", True),
        ("SPY", "ETF", False),
        ("TQQQ", "ETF", False),
        ("ABCDW", "CS", False),  # 5-char warrant
        ("ABCDU", "CS", False),  # 5-char unit
        ("ABCDR", "CS", False),  # 5-char right
        ("ACME.WS", "CS", False),
        ("ACME.U", "CS", False),
        ("ACME.PA", "CS", False),  # preferred
        ("BOND", "BOND", False),
        ("", "CS", False),
    ],
)
def test_common_stock_filter(ticker, sec_type, expected):
    assert is_common_stock(ticker, sec_type) is expected


def _frames(n=50, **overrides):
    bars, ref, scr = [], [], []
    for i in range(n):
        t = f"T{i:03d}"
        bars.append(
            {
                "ticker": t, "date": dt.date(2025, 6, 2), "open": 50.0, "high": 51.0,
                "low": 49.0, "close": overrides.get("close", 50.0),
                "volume": overrides.get("volume", 500_000),
            }
        )
        ref.append(
            {
                "ticker": t, "name": f"Co {i}", "security_type": "CS",
                "exchange": "XNAS", "cik": f"{1000 + i}", "active": True,
            }
        )
        scr.append(
            {
                "ticker": t, "market_cap": overrides.get("market_cap", 5e8),
                "sector": "Technology", "industry": "Software",
            }
        )
    return bars, ref, scr


def test_universe_applies_all_filters(session):
    bars, ref, scr = _frames(50)
    # One name below the price floor, one below the market-cap floor, one ETF.
    bars[0]["close"] = 1.50
    scr[1]["market_cap"] = 10_000_000
    ref[2]["security_type"] = "ETF"
    ref[3]["exchange"] = "OTC"
    ref[4]["active"] = False

    uni, diag = build_universe_from_frames(
        dt.date(2025, 6, 2), session, bars, ref, scr
    )
    survivors = set(uni["ticker"])
    assert "T000" not in survivors
    assert "T001" not in survivors
    assert "T002" not in survivors
    assert "T003" not in survivors
    assert "T004" not in survivors
    assert len(survivors) == 45
    assert diag["rejects"]["price_below_min"] == 1
    assert diag["rejects"]["market_cap_below_min"] == 1
    assert diag["rejects"]["not_common_stock"] == 1


def test_free_mode_universe_relaxes_market_cap(session):
    """With no screener (free-data mode) there is no market cap, so that filter
    must relax -- names still qualify on price + dollar volume alone."""
    bars, ref, _ = _frames(30)
    uni, diag = build_universe_from_frames(
        dt.date(2025, 6, 2), session, bars, ref, []  # empty screener
    )
    assert len(uni) == 30  # nobody dropped for a missing cap
    assert diag["rejects"].get("market_cap_below_min", 0) == 0
    # Price/liquidity gates still bite.
    bars[0]["close"] = 1.0
    uni2, diag2 = build_universe_from_frames(
        dt.date(2025, 6, 3), session, [{**b, "date": dt.date(2025, 6, 3)} for b in bars], ref, []
    )
    assert diag2["rejects"]["price_below_min"] == 1


def test_sector_source_provenance_is_recorded(session):
    """Every survivor gets a sector_source (fmp | sic | unknown) so IC can later
    separate clean vendor sectors from the approximate SIC-derived ones and drop
    the guessed ones."""
    bars, ref, scr = _frames(20)
    # 15 names have an FMP sector; 5 have none (unknown).
    for i in range(15, 20):
        scr[i]["sector"] = None
    uni, diag = build_universe_from_frames(dt.date(2025, 6, 2), session, bars, ref, scr)
    by = uni.set_index("ticker")
    assert (by.loc[[f"T{i:03d}" for i in range(15)], "sector_source"] == "fmp").all()
    assert (by.loc[[f"T{i:03d}" for i in range(15, 20)], "sector_source"] == "unknown").all()
    # Unknown names carry no sector (not a catch-all bucket).
    assert by.loc[[f"T{i:03d}" for i in range(15, 20)], "sector"].isna().all()
    assert diag["sector_source_counts"].get("fmp") == 15

    # And it round-trips through the persisted snapshot (get_universe), which the
    # resume path reads -- else sector_source is lost before Stage 2.
    from src.storage.pit import get_universe

    reloaded = get_universe(session, dt.date(2025, 6, 2)).set_index("ticker")
    assert reloaded.loc["T000", "sector_source"] == "fmp"
    assert reloaded.loc["T019", "sector_source"] == "unknown"


def test_free_mode_universe_marks_sic_and_unknown(session):
    """The SIC-map screener path stamps sic / unknown per the map."""
    bars, ref, _ = _frames(10)
    screener = [
        {"ticker": f"T{i:03d}", "market_cap": float("nan"),
         "sector": ("Energy" if i < 6 else None),
         "sector_source": ("sic" if i < 6 else "unknown"), "industry": None}
        for i in range(10)
    ]
    uni, _ = build_universe_from_frames(dt.date(2025, 6, 2), session, bars, ref, screener)
    by = uni.set_index("ticker")
    assert (by.loc[[f"T{i:03d}" for i in range(6)], "sector_source"] == "sic").all()
    assert (by.loc[[f"T{i:03d}" for i in range(6, 10)], "sector_source"] == "unknown").all()


def test_parse_company_tickers():
    from src.ingest.sec_edgar import parse_company_tickers

    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
        "2": {"cik_str": 0, "ticker": "", "title": "No Ticker"},  # skipped
        "3": {"cik_str": 320193, "ticker": "AAPL", "title": "dup"},  # deduped
    }
    rows = parse_company_tickers(payload)
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["AAPL", "MSFT"]
    assert rows[0]["cik"] == "320193"
    assert rows[0]["security_type"] == "CS"


def test_yahoo_frame_to_rows_multiindex():
    import pandas as pd

    from src.ingest.yahoo import _frame_to_rows

    idx = pd.to_datetime(["2025-06-02", "2025-06-03"])
    cols = pd.MultiIndex.from_product(
        [["AAPL", "MSFT"], ["Open", "High", "Low", "Close", "Volume"]]
    )
    data = [
        [200, 202, 199, 201, 1_000_000, 400, 404, 398, 402, 2_000_000],
        [201, 205, 200, 204, 1_100_000, 402, 406, 401, 405, 2_100_000],
    ]
    df = pd.DataFrame(data, index=idx, columns=cols)
    rows = _frame_to_rows(df, ["AAPL", "MSFT"])
    assert len(rows) == 4
    aapl = [r for r in rows if r["ticker"] == "AAPL"]
    assert aapl[0]["close"] == 201 and aapl[0]["date"] == dt.date(2025, 6, 2)
    assert all(r["vwap"] is None for r in rows)


def test_yahoo_frame_to_rows_skips_nan_close():
    import numpy as np
    import pandas as pd

    from src.ingest.yahoo import _frame_to_rows

    idx = pd.to_datetime(["2025-06-02", "2025-06-03"])
    df = pd.DataFrame(
        {"Open": [10.0, 11.0], "High": [10.5, 11.5], "Low": [9.5, 10.5],
         "Close": [np.nan, 11.2], "Volume": [1000, 1100]},
        index=idx,
    )
    rows = _frame_to_rows(df, ["ABC"])
    assert len(rows) == 1  # the NaN-close row is dropped
    assert rows[0]["date"] == dt.date(2025, 6, 3)


def test_universe_snapshot_is_persisted_per_date(session):
    """The survivorship-bias defence: every day gets its own snapshot."""
    from src.storage.pit import get_universe

    bars, ref, scr = _frames(20)
    build_universe_from_frames(dt.date(2025, 6, 2), session, bars, ref, scr)

    # The next day a company dies -- it must remain in the earlier snapshot.
    bars2 = [b for b in bars if b["ticker"] != "T000"]
    bars2 = [{**b, "date": dt.date(2025, 6, 3)} for b in bars2]
    build_universe_from_frames(dt.date(2025, 6, 3), session, bars2, ref, scr)
    session.flush()

    day1 = get_universe(session, dt.date(2025, 6, 2))
    day2 = get_universe(session, dt.date(2025, 6, 3))
    assert "T000" in set(day1["ticker"]), "the dead name vanished from history"
    assert "T000" not in set(day2["ticker"])
    assert len(day1) == len(day2) + 1


def test_universe_falls_back_to_the_prior_snapshot_not_today(session):
    from src.storage.pit import get_universe

    bars, ref, scr = _frames(10)
    build_universe_from_frames(dt.date(2025, 6, 2), session, bars, ref, scr)
    session.flush()

    # Asking for a later date with no snapshot returns the most recent PRIOR one.
    got = get_universe(session, dt.date(2025, 6, 10))
    assert len(got) == 10
    assert (got["as_of_date"] == dt.date(2025, 6, 2)).all()

    # Asking for an earlier date returns nothing rather than today's listing.
    assert get_universe(session, dt.date(2025, 1, 1)).empty


def test_dollar_volume_filter(session):
    bars, ref, scr = _frames(10, close=5.0, volume=100_000)  # $500k ADV
    uni, diag = build_universe_from_frames(
        dt.date(2025, 6, 2), session, bars, ref, scr
    )
    assert uni.empty
    assert diag["rejects"]["dollar_volume_below_min"] == 10
