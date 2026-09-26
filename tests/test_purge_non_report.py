"""Rows stored from 8-K/S-4/S-1 filings by loads before 2026-09-25 are purged."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import zipfile

import pytest

SUB = "adsh\tcik\tform\tfiled\n" \
      "a1\t0000000001\t10-Q\t20260511\n" \
      "a2\t0000000001\t8-K\t20260626\n" \
      "a3\t0000000002\t8-K\t20260511\n" \
      "a4\t0000000002\t10-Q\t20260511\n"


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub.txt", SUB)
        z.writestr("num.txt", "adsh\ttag\tddate\tqtrs\tuom\tvalue\tcoreg\tsegments\n")
    return buf.getvalue()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'purge.db'}")
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache, session_scope
    from src.storage.models import Fundamental

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    p = dt.date(2025, 12, 31)
    with session_scope() as s:
        for t, filed, v in (("SYK", dt.date(2026, 5, 11), 22.6e9),   # its 10-Q
                            ("SYK", dt.date(2026, 6, 26), 6.4e9),    # the 8-K
                            ("TWO", dt.date(2026, 5, 11), 1e9)):     # 8-K + 10-Q same day
            s.add(Fundamental(ticker=t, metric="revenue", value=v, period_end=p,
                              fiscal_period="FY", filing_date=filed, source="sec"))

    async def fake_fetch(year, q, **kw):
        return _zip()

    async def fake_map():
        return {"1": "SYK", "2": "TWO"}

    import src.backfill as bf
    import src.ingest.sec_cache as sc
    monkeypatch.setattr(sc, "fetch_dataset", fake_fetch)
    monkeypatch.setattr(bf, "_cik_to_ticker", fake_map)
    yield
    get_settings.cache_clear()
    reset_engine_cache()


def _values():
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        return sorted((r.ticker, r.value) for r in s.execute(select(Fundamental)).scalars())


def test_dry_run_counts_and_real_run_deletes_only_8k_only_days(db):
    from src.backfill import purge_non_report_rows

    out = asyncio.run(purge_non_report_rows(quarters=1, dry_run=True))
    assert out["rows_matched"] == 1 and out["rows_deleted"] == 0
    assert len(_values()) == 3
    out = asyncio.run(purge_non_report_rows(quarters=1, dry_run=False))
    assert out["rows_deleted"] == 1
    # The 8-K-only day is gone; the 10-Q and the ambiguous day stay.
    assert _values() == [("SYK", 22.6e9), ("TWO", 1e9)]
