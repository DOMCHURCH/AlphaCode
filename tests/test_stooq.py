"""Stooq keyless bulk source — parser and ZIP-iteration tests.

Offline by construction: the parser and the zip loader are pure/streaming, so
the whole keyless wide-end is verified without touching the network. No row is
ever fabricated to pass — malformed input is dropped.
"""

from __future__ import annotations

import datetime as dt
import zipfile

import pytest

from src.ingest import stooq

BUNDLE = (
    "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"
    "AAPL.US,D,20240102,000000,185.5,186.2,183.9,185.10,50000000,0\n"
    "AAPL.US,D,20240103,000000,185.0,187.0,184.5,186.30,48000000,0\n"
)
PLAIN = "Date,Open,High,Low,Close,Volume\n2024-01-02,10,11,9,10.5,1000\n"


def test_parse_bundle_format():
    rows = stooq.parse_stooq_txt(BUNDLE, "AAPL")
    assert len(rows) == 2
    assert rows[0]["date"] == dt.date(2024, 1, 2)
    assert rows[0]["close"] == 185.10
    assert rows[0]["volume"] == 50_000_000
    assert rows[1]["date"] == dt.date(2024, 1, 3)


def test_parse_plain_format():
    rows = stooq.parse_stooq_txt(PLAIN, "XYZ")
    assert len(rows) == 1
    assert rows[0]["close"] == 10.5 and rows[0]["date"] == dt.date(2024, 1, 2)


def test_parse_respects_since_and_drops_bad_rows():
    text = (
        "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"
        "T.US,D,20240102,0,1,1,1,1.0,10,0\n"       # before since -> dropped
        "T.US,D,20240110,0,1,1,1,,10,0\n"          # no close -> dropped
        "T.US,D,20240115,0,1,1,1,2.5,10,0\n"       # kept
        "garbage line without commas\n"            # unparseable -> dropped
    )
    rows = stooq.parse_stooq_txt(text, "T", since=dt.date(2024, 1, 5))
    assert [r["date"] for r in rows] == [dt.date(2024, 1, 15)]
    assert rows[0]["close"] == 2.5


def test_parse_refuses_headerless_data():
    # No recognizable header -> refuse rather than guess column positions.
    assert stooq.parse_stooq_txt("20240102,1,2,3,4,5\n", "T") == []


def test_ticker_from_filename():
    assert stooq.ticker_from_filename("data/daily/us/nasdaq/aapl.us.txt") == "AAPL"
    assert stooq.ticker_from_filename("brk-b.us.txt") == "BRK-B"  # class share
    assert stooq.ticker_from_filename("readme.txt") is None
    assert stooq.ticker_from_filename("foo.uk.txt") is None


def _make_zip(tmp_path):
    p = tmp_path / "d_us_txt.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("data/daily/us/1/aapl.us.txt", BUNDLE)
        zf.writestr("data/daily/us/1/xyz.us.txt", PLAIN)
        zf.writestr("data/daily/us/1/readme.md", "not a ticker")
    return p


def test_iter_and_load_from_zip(tmp_path):
    p = _make_zip(tmp_path)

    saved: list[dict] = []
    progress: list[tuple[int, int]] = []
    total = stooq.load_bulk_from_zip(
        p, lambda batch: (saved.extend(batch), len(batch))[1],
        on_progress=lambda td, rows: progress.append((td, rows)),
    )
    assert total == 3  # 2 AAPL + 1 XYZ
    tickers = {r["ticker"] for r in saved}
    assert tickers == {"AAPL", "XYZ"}
    assert progress and progress[-1][1] == 3


def test_load_respects_max_tickers(tmp_path):
    p = _make_zip(tmp_path)
    saved: list[dict] = []
    stooq.load_bulk_from_zip(p, lambda b: (saved.extend(b), len(b))[1], max_tickers=1)
    assert len({r["ticker"] for r in saved}) == 1


def test_download_rejects_non_zip(tmp_path, monkeypatch):
    """A throttled HTML response (not a ZIP) must fail loudly, never as empty."""
    import asyncio

    class _Resp:
        status_code = 200

        async def aiter_bytes(self, _n):
            yield b"<html>rate limited</html>"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(stooq.httpx, "AsyncClient", _Client)
    with pytest.raises(RuntimeError, match="not a valid ZIP"):
        asyncio.run(stooq.download_bulk("https://example.test/x.zip"))
