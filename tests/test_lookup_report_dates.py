"""Tests for lookup_report_dates script."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import pytest
from sqlalchemy import create_engine, event, func, inspect, select, text

D = dt.date

# Import the script module
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from scripts.lookup_report_dates import (
    find_candidates,
    lookup_candidates,
    write_csv,
    summarise,
    main,
)
from src.storage.db import init_db
from src.storage.models import Fundamental


@pytest.fixture()
def tmp_sqlite_db():
    """Create a temporary SQLite database for testing."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        db_url = f"sqlite:///{db_path}"
        engine = create_engine(db_url, future=True, connect_args={"check_same_thread": False})

        # Create tables
        from src.storage.models import Base
        Base.metadata.create_all(engine)

        yield engine, db_path, db_url

        engine.dispose()


def put_fundamental(engine, ticker, metric, value, period_end, filing_date, **kwargs):
    """Helper to add a Fundamental row."""
    from sqlalchemy.orm import sessionmaker
    SessionFactory = sessionmaker(bind=engine, future=True)
    with SessionFactory() as s:
        s.add(Fundamental(
            ticker=ticker,
            metric=metric,
            value=value,
            period_end=period_end,
            filing_date=filing_date,
            **kwargs
        ))
        s.commit()


class TestCandidateDerivation:
    """Tests for find_candidates logic."""

    def test_52_week_filer_with_month_end_row(self, tmp_sqlite_db):
        """A 52/53-week filer with a month-end-only old filing should be a candidate."""
        engine, _, _ = tmp_sqlite_db

        # AAPL: non-month-end date exists (52-week style)
        # First filing has a real date
        put_fundamental(engine, "AAPL", "total_assets", 100.0, D(2026, 3, 28), D(2026, 5, 1))
        # Second filing has a month-end date with no twin in the same filing
        put_fundamental(engine, "AAPL", "total_assets", 100.0, D(2026, 6, 30), D(2026, 8, 5))

        candidates = find_candidates(engine)

        # Should find the month-end row (6-30) since it has no same-filing twin
        cand_strs = [(t, fd.isoformat(), pe.isoformat()) for t, fd, pe in candidates]
        assert ("AAPL", "2026-08-05", "2026-06-30") in cand_strs

    def test_calendar_aligned_filer_excluded(self, tmp_sqlite_db):
        """A calendar-aligned filer (all month ends) should NOT be a candidate."""
        engine, _, _ = tmp_sqlite_db

        # MSFT: only month-end dates
        put_fundamental(engine, "MSFT", "total_assets", 100.0, D(2026, 3, 31), D(2026, 5, 1))
        put_fundamental(engine, "MSFT", "total_assets", 100.0, D(2026, 6, 30), D(2026, 8, 1))

        candidates = find_candidates(engine)

        # No candidates for MSFT
        assert not any(c[0] == "MSFT" for c in candidates)

    def test_month_end_with_same_filing_twin_excluded(self, tmp_sqlite_db):
        """A month-end with a same-filing non-month-end twin should be excluded."""
        engine, _, _ = tmp_sqlite_db

        # DE (Deere): 52/53-week filer
        # Real date + rounded twin in same filing
        put_fundamental(engine, "DE", "total_assets", 100.0, D(2026, 5, 3), D(2026, 5, 28))
        put_fundamental(engine, "DE", "total_assets", 100.0, D(2026, 4, 30), D(2026, 5, 28))

        # Another filing, month-end only
        put_fundamental(engine, "DE", "total_assets", 100.0, D(2026, 7, 31), D(2026, 8, 28))

        candidates = find_candidates(engine)

        cand_strs = [(t, fd.isoformat(), pe.isoformat()) for t, fd, pe in candidates]
        # (5-3, 5-28) should NOT be a candidate (has 4-30 twin in same filing)
        # But (7-31, 8-28) should be a candidate (no twin)
        assert ("DE", "2026-08-28", "2026-07-31") in cand_strs
        assert ("DE", "2026-05-28", "2026-04-30") not in cand_strs


class TestLookupCandidates:
    """Tests for lookup_candidates logic and all status codes."""

    def test_status_rounded(self):
        """edgar_report_date is NOT a month end, 1-15 days from period_end."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "AAPL", "cik_str": 320193}
            },
            "https://data.sec.gov/submissions/CIK0000320193.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-26-000001"],
                        "filingDate": ["2026-05-01"],
                        "reportDate": ["2026-03-28"],  # NOT a month end, 3 days before 3-31
                        "form": ["10-Q"],
                        "primaryDocument": ["0000320193-26-000001-index.htm"]
                    }
                }
            }
        })

        candidates = [("AAPL", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "rounded"
        assert results[0]["diff_days"] == 3
        assert results[0]["edgar_report_date"] == "2026-03-28"

    def test_status_same(self):
        """edgar_report_date equals period_end."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "MSFT", "cik_str": 789019}
            },
            "https://data.sec.gov/submissions/CIK0000789019.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000789019-26-000001"],
                        "filingDate": ["2026-05-01"],
                        "reportDate": ["2026-03-31"],  # Exact match
                        "form": ["10-Q"],
                        "primaryDocument": ["index.htm"]
                    }
                }
            }
        })

        candidates = [("MSFT", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "same"
        assert results[0]["diff_days"] == 0

    def test_status_far(self):
        """edgar_report_date differs by more than 15 days."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "NVDA", "cik_str": 1318605}
            },
            "https://data.sec.gov/submissions/CIK0001318605.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001318605-26-000001"],
                        "filingDate": ["2026-05-20"],
                        "reportDate": ["2026-04-01"],  # 29 days before 4-30
                        "form": ["10-Q"],
                        "primaryDocument": ["index.htm"]
                    }
                }
            }
        })

        candidates = [("NVDA", D(2026, 5, 20), D(2026, 4, 30))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "far"
        assert abs(results[0]["diff_days"]) > 15

    def test_status_no_match(self):
        """No 10-Q/10-K found for that filing_date."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "TEST", "cik_str": 1234567}
            },
            "https://data.sec.gov/submissions/CIK0001234567.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001234567-26-000001"],
                        "filingDate": ["2026-06-01"],
                        "reportDate": ["2026-03-31"],
                        "form": ["8-K"],  # Not a 10-Q/10-K
                        "primaryDocument": ["index.htm"]
                    }
                }
            }
        })

        candidates = [("TEST", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "no_match"

    def test_status_multiple(self):
        """Multiple distinct reportDates for the same filing_date."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "MULTI", "cik_str": 9999999}
            },
            "https://data.sec.gov/submissions/CIK0009999999.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0009999999-26-000001", "0009999999-26-000002"],
                        "filingDate": ["2026-05-01", "2026-05-01"],
                        "reportDate": ["2026-03-31", "2026-03-28"],  # Two different dates
                        "form": ["10-Q", "10-Q"],
                        "primaryDocument": ["index.htm", "index.htm"]
                    }
                }
            }
        })

        candidates = [("MULTI", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "multiple"
        assert "2026-03-31" in results[0]["note"]

    def test_status_error(self):
        """HTTP/parse failure after retries (submissions fetch fails)."""
        def fetch_partial_error(url):
            if url == "https://www.sec.gov/files/company_tickers.json":
                return {"0": {"ticker": "BAD", "cik_str": 9999999}}
            # Fail on submissions fetch
            raise Exception("Connection failed")

        candidates = [("BAD", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch_partial_error, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "error"
        # Note: should not contain actual error message, just a generic one
        assert "Failed" in results[0]["note"]

    def test_status_no_cik(self):
        """CIK not found in ticker mapping."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {}
        })

        candidates = [("NOCIK", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "no_cik"
        assert results[0]["cik"] is None

    def test_10k_form_matched(self):
        """10-K forms (incl. variants) are matched."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "K", "cik_str": 1000000}
            },
            "https://data.sec.gov/submissions/CIK0001000000.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001000000-26-000001"],
                        "filingDate": ["2026-03-01"],
                        "reportDate": ["2025-12-31"],
                        "form": ["10-K405"],  # 10-K variant
                        "primaryDocument": ["index.htm"]
                    }
                }
            }
        })

        candidates = [("K", D(2026, 3, 1), D(2025, 12, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "same"
        assert results[0]["form"] == "10-K405"

    def test_amended_form_matched(self):
        """10-Q/A and 10-K/A forms are matched."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "AMEND", "cik_str": 2000000}
            },
            "https://data.sec.gov/submissions/CIK0002000000.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0002000000-26-000001"],
                        "filingDate": ["2026-05-10"],
                        "reportDate": ["2026-03-31"],
                        "form": ["10-Q/A"],  # Amended
                        "primaryDocument": ["index.htm"]
                    }
                }
            }
        })

        candidates = [("AMEND", D(2026, 5, 10), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        assert results[0]["status"] == "same"

    def test_older_filings_fetched(self):
        """Older filings from files array are fetched when needed."""
        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "OLD", "cik_str": 3000000}
            },
            "https://data.sec.gov/submissions/CIK0003000000.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0003000000-26-000001"],
                        "filingDate": ["2026-05-01"],
                        "reportDate": ["2026-03-31"],
                        "form": ["10-Q"],
                        "primaryDocument": ["index.htm"]
                    },
                    "files": [
                        {"name": "CIK0003000000-submissions-001.json"}
                    ]
                }
            },
            "https://data.sec.gov/submissions/CIK0003000000-submissions-001.json": {
                "accessionNumber": ["0003000000-25-000001"],
                "filingDate": ["2025-05-01"],
                "reportDate": ["2025-03-28"],
                "form": ["10-Q"],
                "primaryDocument": ["index.htm"]
            }
        })

        # Candidate with older filing_date
        candidates = [("OLD", D(2025, 5, 1), D(2025, 3, 31))]
        results = lookup_candidates(candidates, fetch, sleep=_no_sleep)

        assert len(results) == 1
        # The old filing has report_date 2025-03-28 which is 3 days before 2025-03-31 month end
        # and 2025-03-28 is NOT a month end, so it should be "rounded"
        assert results[0]["status"] == "rounded"
        assert results[0]["edgar_report_date"] == "2025-03-28"


class TestCSVHandling:
    """Tests for CSV writing and --resume logic."""

    def test_csv_columns_and_sort(self, tmp_sqlite_db):
        """CSV has correct columns in correct order."""
        _, _, _ = tmp_sqlite_db
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            out_path = Path(f.name)

        try:
            results = [
                {
                    "ticker": "Z", "cik": "1", "filing_date": "2026-05-02",
                    "db_period_end": "2026-03-31", "edgar_report_date": "2026-03-28",
                    "form": "10-Q", "accession": "0000000001-26-000001",
                    "diff_days": 3, "status": "rounded", "note": ""
                },
                {
                    "ticker": "A", "cik": "2", "filing_date": "2026-05-01",
                    "db_period_end": "2026-03-31", "edgar_report_date": "2026-03-31",
                    "form": "10-Q", "accession": "0000000002-26-000001",
                    "diff_days": 0, "status": "same", "note": ""
                },
            ]

            write_csv(results, out_path, resume=False)

            with open(out_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            # Check column order (via header)
            with open(out_path) as f:
                header = f.readline().strip()
            assert header.startswith("ticker")

            # Check sort order: A before Z
            assert rows[0]["ticker"] == "A"
            assert rows[1]["ticker"] == "Z"

            assert len(rows) == 2
        finally:
            out_path.unlink()

    def test_resume_appends_to_existing(self):
        """--resume flag appends rather than overwriting."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
            out_path = Path(f.name)
            f.write("ticker,cik,filing_date,db_period_end,edgar_report_date,form,accession,diff_days,status,note\n")
            f.write("A,1,2026-05-01,2026-03-31,2026-03-31,10-Q,0000000001,0,same,\n")

        try:
            new_results = [
                {
                    "ticker": "B", "cik": "2", "filing_date": "2026-05-02",
                    "db_period_end": "2026-03-31", "edgar_report_date": "2026-03-28",
                    "form": "10-Q", "accession": "0000000002", "diff_days": 3,
                    "status": "rounded", "note": ""
                }
            ]

            write_csv(new_results, out_path, resume=True)

            with open(out_path) as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            assert len(rows) == 2
            assert rows[0]["ticker"] == "A"
            assert rows[1]["ticker"] == "B"
        finally:
            out_path.unlink()

class TestCountOnly:
    """Tests for --count-only flag."""

    def test_count_only_makes_no_network_calls(self, tmp_sqlite_db):
        """--count-only should not call fetch."""
        engine, _, db_url = tmp_sqlite_db

        # Add some test data
        put_fundamental(engine, "A", "total_assets", 100.0, D(2026, 3, 28), D(2026, 5, 1))
        put_fundamental(engine, "A", "total_assets", 100.0, D(2026, 3, 31), D(2026, 5, 1))
        put_fundamental(engine, "B", "total_assets", 100.0, D(2026, 6, 30), D(2026, 8, 1))

        fetch_called = False
        def fetch_that_errors(url):
            nonlocal fetch_called
            fetch_called = True
            raise RuntimeError("fetch should not be called in --count-only mode")

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            out_path = Path(f.name)
        out_path.unlink()  # Delete it so main() can create it fresh

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            url_file = Path(f.name)
            f.write(db_url)

        try:
            if True:
                result = main(["--url-file", str(url_file), "--out", str(out_path), "--count-only"])

            assert not fetch_called
            assert result == 0
        finally:
            out_path.unlink(missing_ok=True)
            url_file.unlink(missing_ok=True)


class TestRateLimiting:
    """Tests for rate limiting and retry behavior."""

    def test_sleep_called_between_requests(self):
        """sleep function is called with minimum spacing."""
        sleep_calls = []
        def mock_sleep(seconds):
            sleep_calls.append(seconds)

        fetch = _mock_fetch({
            "https://www.sec.gov/files/company_tickers.json": {
                "0": {"ticker": "A", "cik_str": 1}
            },
            "https://data.sec.gov/submissions/CIK0000000001.json": {
                "filings": {"recent": {
                    "accessionNumber": ["1"],
                    "filingDate": ["2026-05-01"],
                    "reportDate": ["2026-03-31"],
                    "form": ["10-Q"],
                    "primaryDocument": ["idx"]
                }}
            }
        })

        candidates = [("A", D(2026, 5, 1), D(2026, 3, 31))]
        lookup_candidates(candidates, fetch, sleep=mock_sleep)

        # Should have at least one sleep call of 0.2 seconds
        assert any(s == 0.2 for s in sleep_calls)

    def test_429_on_submissions_handled(self):
        """A 429 error on submissions fetch results in error status."""
        def fetch_with_429_on_submissions(url):
            if url == "https://www.sec.gov/files/company_tickers.json":
                return {"0": {"ticker": "A", "cik_str": 1}}
            if url == "https://data.sec.gov/submissions/CIK0000000001.json":
                raise HTTPError(url, 429, "Too Many Requests", {}, None)
            raise Exception(f"Unexpected URL: {url}")

        candidates = [("A", D(2026, 5, 1), D(2026, 3, 31))]
        results = lookup_candidates(candidates, fetch_with_429_on_submissions, sleep=_no_sleep)

        # Should result in error status since submissions fetch failed
        assert len(results) == 1
        assert results[0]["status"] == "error"


class TestSecurityAndNoSecrets:
    """Tests for secret handling and no-write guarantees."""

    def test_db_url_not_in_stdout_stderr(self, tmp_sqlite_db):
        """Database URL is never printed to stdout/stderr."""
        _, _, db_url = tmp_sqlite_db

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            out_path = Path(f.name)
        out_path.unlink()  # Delete it so main() can create it fresh

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            url_file = Path(f.name)
            f.write(db_url)

        try:
            # Capture stdout/stderr and check they don't contain the URL
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            try:
                result = main(["--url-file", str(url_file), "--out", str(out_path), "--count-only"])
            finally:
                stdout_content = sys.stdout.getvalue()
                stderr_content = sys.stderr.getvalue()
                sys.stdout = old_stdout
                sys.stderr = old_stderr

            # Check that the URL doesn't appear
            assert db_url not in stdout_content
            assert db_url not in stderr_content
            assert result == 0
        finally:
            out_path.unlink(missing_ok=True)
            url_file.unlink(missing_ok=True)

# ============================================================================
# Helper functions
# ============================================================================

def _mock_fetch(responses: dict[str, Any]):
    """Create a mock fetch function that returns pre-canned responses."""
    def fetch(url: str) -> Any:
        if url in responses:
            return responses[url]
        raise Exception(f"Unexpected URL: {url}")
    return fetch


def _no_sleep(seconds: float) -> None:
    """Sleep function that does nothing."""
    pass


# ============================================================================
# main(): the end-to-end behaviour that matters -- read-only, filtered before fetching, resumable, scrubbed
# ============================================================================

CIKS = {"0": {"cik_str": 1, "ticker": "AAA"}, "1": {"cik_str": 2, "ticker": "BBB"}}


def _subs(report_date, filed="2026-05-01", form="10-Q"):
    return {"filings": {"recent": {"accessionNumber": ["0000000001-26-000001"], "filingDate": [filed],
                                   "reportDate": [report_date], "form": [form], "primaryDocument": ["x.htm"]}, "files": []}}


def _seed_two_filers(engine):
    for t in ("AAA", "BBB"):
        put_fundamental(engine, t, "total_assets", 1.0, D(2026, 3, 31), D(2026, 5, 1))     # month end, no twin
        put_fundamental(engine, t, "total_assets", 1.0, D(2025, 6, 28), D(2025, 8, 1))     # a real 52-week date


class _Recorder:
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, url):
        self.calls.append(url)
        return self.table[url]


TABLE = {
    "https://www.sec.gov/files/company_tickers.json": CIKS,
    "https://data.sec.gov/submissions/CIK0000000001.json": _subs("2026-03-28"),
    "https://data.sec.gov/submissions/CIK0000000002.json": _subs("2026-03-28"),
}


def test_main_filters_tickers_before_fetching_and_writes_the_csv(tmp_sqlite_db, tmp_path):
    engine, _, url = tmp_sqlite_db
    _seed_two_filers(engine)
    (tmp_path / "u.txt").write_text(url)
    rec, out = _Recorder(TABLE), tmp_path / "o.csv"
    assert main(["--url-file", str(tmp_path / "u.txt"), "--out", str(out), "--tickers", "AAA"],
                fetch=rec, sleep=_no_sleep) == 0
    assert not any("CIK0000000002" in c for c in rec.calls)          # BBB was never fetched
    rows = list(csv.DictReader(open(out, newline="")))
    assert [(r["ticker"], r["status"], r["diff_days"]) for r in rows] == [("AAA", "rounded", "3")]


def test_main_resume_skips_what_is_done_and_does_not_refetch(tmp_sqlite_db, tmp_path):
    engine, _, url = tmp_sqlite_db
    _seed_two_filers(engine)
    (tmp_path / "u.txt").write_text(url)
    out = tmp_path / "o.csv"
    main(["--url-file", str(tmp_path / "u.txt"), "--out", str(out), "--tickers", "AAA"], fetch=_Recorder(TABLE), sleep=_no_sleep)
    rec = _Recorder(TABLE)
    assert main(["--url-file", str(tmp_path / "u.txt"), "--out", str(out), "--resume"], fetch=rec, sleep=_no_sleep) == 0
    assert not any("CIK0000000001" in c for c in rec.calls)          # AAA was already in the CSV
    assert sorted(r["ticker"] for r in csv.DictReader(open(out, newline=""))) == ["AAA", "BBB"]


def test_main_refuses_to_overwrite_an_existing_output_without_resume(tmp_sqlite_db, tmp_path):
    _, _, url = tmp_sqlite_db
    (tmp_path / "u.txt").write_text(url)
    out = tmp_path / "o.csv"
    out.write_text("keep me")
    assert main(["--url-file", str(tmp_path / "u.txt"), "--out", str(out)], fetch=_Recorder(TABLE), sleep=_no_sleep) == 1
    assert out.read_text() == "keep me"


def test_rows_are_written_as_each_ticker_finishes(tmp_sqlite_db, tmp_path):
    """An interrupted run keeps what it already learned."""
    engine, _, url = tmp_sqlite_db
    _seed_two_filers(engine)
    (tmp_path / "u.txt").write_text(url)
    out = tmp_path / "o.csv"

    def fetch(u):
        if "CIK0000000002" in u:
            assert [r["ticker"] for r in csv.DictReader(open(out, newline=""))] == ["AAA"]   # AAA is already on disk
            raise KeyboardInterrupt
        return TABLE[u]

    with pytest.raises(KeyboardInterrupt):
        main(["--url-file", str(tmp_path / "u.txt"), "--out", str(out)], fetch=fetch, sleep=_no_sleep)
    assert [r["ticker"] for r in csv.DictReader(open(out, newline=""))] == ["AAA"]


def test_the_candidate_check_is_per_filing(tmp_sqlite_db):
    """A month-end that has a twin in ANOTHER filing is still unproven in its own."""
    engine, _, _ = tmp_sqlite_db
    put_fundamental(engine, "AAA", "total_assets", 1.0, D(2026, 3, 31), D(2026, 5, 1))
    put_fundamental(engine, "AAA", "total_assets", 1.0, D(2026, 3, 28), D(2026, 5, 1))   # twin, filing 1
    put_fundamental(engine, "AAA", "total_assets", 1.0, D(2026, 3, 31), D(2026, 9, 1))   # no twin, filing 2
    assert find_candidates(engine) == [("AAA", D(2026, 9, 1), D(2026, 3, 31))]


def test_a_comparative_period_is_explained_not_just_flagged_far(tmp_sqlite_db):
    table = {"https://www.sec.gov/files/company_tickers.json": CIKS,
             "https://data.sec.gov/submissions/CIK0000000001.json": _subs("2026-03-28")}
    [row] = lookup_candidates([("AAA", D(2026, 5, 1), D(2025, 3, 31))], _Recorder(table), _no_sleep)
    assert row["status"] == "far" and "comparative" in row["note"]


def test_the_default_fetch_decompresses_what_it_asked_for(monkeypatch):
    import gzip
    import json

    import scripts.lookup_report_dates as mod

    class Resp:
        headers = {"Content-Encoding": "gzip"}

        def read(self):
            return gzip.compress(json.dumps({"ok": 1}).encode())

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "urlopen", lambda req, timeout=None: Resp())
    monkeypatch.setattr(mod, "get_settings", lambda: type("S", (), {"sec_user_agent": "Test test@example.com"})())
    assert mod._fetch_json_default("https://data.sec.gov/x") == {"ok": 1}


def test_the_database_url_is_scrubbed_from_a_failed_connection(tmp_path, capsys):
    secret = "hunter2-very-secret"
    (tmp_path / "pg.txt").write_text(f"postgresql://someuser:{secret}@127.0.0.1:1/nodb")
    assert main(["--url-file", str(tmp_path / "pg.txt"), "--count-only"]) == 1
    cap = capsys.readouterr()
    assert secret not in cap.out + cap.err and "someuser" not in cap.err


def test_the_candidate_scan_cannot_write_even_if_asked(tmp_sqlite_db):
    from sqlalchemy import delete

    from src.storage.script_db import make_engine

    engine, _, url = tmp_sqlite_db
    put_fundamental(engine, "AAA", "total_assets", 1.0, D(2026, 3, 31), D(2026, 5, 1))
    ro = make_engine(url, readonly=True)
    ro.dispose()
    with pytest.raises(Exception):
        with ro.begin() as conn:
            conn.execute(delete(Fundamental))
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(Fundamental)).scalar_one() == 1
