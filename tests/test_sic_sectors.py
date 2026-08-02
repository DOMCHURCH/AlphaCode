"""SIC -> GICS sector map: mapping correctness, SEC parse, backfill, and that
the free universe path actually attaches sectors (so Stage-2 stays
sector-neutral instead of universe-neutral)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from src.ingest import sic
from src.ingest.sic import (
    COMM_SVCS,
    CONS_DISC,
    CONS_STAPLES,
    ENERGY,
    FINANCIALS,
    HEALTHCARE,
    INDUSTRIALS,
    INFO_TECH,
    MATERIALS,
    REAL_ESTATE,
    UTILITIES,
)


@pytest.mark.parametrize(
    "code,expected",
    [
        (1311, ENERGY),          # crude petroleum & natural gas
        (2834, HEALTHCARE),      # pharmaceutical preparations (override in chemicals)
        (2820, MATERIALS),       # plastics materials
        (3674, INFO_TECH),       # semiconductors
        (3571, INFO_TECH),       # electronic computers
        (3711, CONS_DISC),       # motor vehicles
        (3721, INDUSTRIALS),     # aircraft
        (3841, HEALTHCARE),      # surgical & medical instruments
        (4813, COMM_SVCS),       # telephone communications
        (4911, UTILITIES),       # electric services
        (5411, CONS_STAPLES),    # grocery stores
        (5731, CONS_DISC),       # radio/TV/consumer electronics stores
        (6022, FINANCIALS),      # state commercial banks
        (6798, REAL_ESTATE),     # REITs (override in holding offices)
        (7372, INFO_TECH),       # prepackaged software
        (8000, HEALTHCARE),      # health services
        (100, CONS_STAPLES),     # agricultural production
        (9995, None),            # non-classifiable -> honest None
        (0, None),
        (None, None),
        ("abc", None),
    ],
)
def test_sic_to_gics(code, expected):
    assert sic.sic_to_gics(code) == expected


def test_narrow_overrides_precede_broad_ranges():
    # drugs (2834) must not fall into the chemicals bucket (2800-2899 Materials)
    assert sic.sic_to_gics(2834) == HEALTHCARE
    # REIT (6798) must not fall into holding offices (Financials)
    assert sic.sic_to_gics(6798) == REAL_ESTATE
    # software services (7372) must not fall into business services (Industrials)
    assert sic.sic_to_gics(7372) == INFO_TECH


def test_parse_sic_from_submissions_payload():
    from src.ingest.sec_edgar import parse_sic

    sic_code, desc = parse_sic({"sic": "3674", "sicDescription": "Semiconductors"})
    assert sic_code == "3674" and desc == "Semiconductors"
    assert parse_sic({}) == (None, None)
    assert parse_sic({"sic": "", "sicDescription": None}) == (None, None)


def _db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sic.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()


def test_sector_backfill_caches_map_and_skips_done(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    from src import backfill
    from src.ingest import sec_edgar
    from src.storage import repository
    from src.storage.db import session_scope

    async def fake_tickers():
        return [
            {"ticker": "NVDA", "cik": "1045810", "name": "NVIDIA"},
            {"ticker": "XOM", "cik": "34088", "name": "Exxon"},
            {"ticker": "NOSIC", "cik": "999", "name": "Weird"},
        ]

    sics = {"1045810": ("3674", "Semiconductors"), "34088": ("1311", "Oil & Gas"),
            "999": ("9995", "Non-classifiable")}

    async def fake_fetch_sic(client, cik):
        return sics.get(str(cik), (None, None))

    monkeypatch.setattr(sec_edgar, "fetch_company_tickers", fake_tickers)
    monkeypatch.setattr(sec_edgar, "fetch_sic", fake_fetch_sic)
    monkeypatch.setattr(sec_edgar, "make_client", lambda **k: _NullClient())

    n = asyncio.run(backfill.backfill_sectors())
    assert n == 3
    with session_scope() as s:
        smap = repository.get_sector_map(s)
    assert smap["NVDA"] == INFO_TECH
    assert smap["XOM"] == ENERGY
    assert "NOSIC" not in smap  # 9995 -> None sector, not forced into a bucket

    # A second pass skips already-mapped CIKs -> nothing new.
    assert asyncio.run(backfill.backfill_sectors()) == 0


def test_free_universe_attaches_cached_sectors(tmp_path, monkeypatch):
    _db(tmp_path, monkeypatch)
    monkeypatch.setenv("POLYGON_API_KEY", "")
    monkeypatch.setenv("MIN_UNIVERSE_SIZE", "1")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    from src.ingest import sec_edgar
    from src.storage import repository
    from src.storage.db import session_scope
    from src.universe import builder

    as_of = dt.date(2025, 6, 2)
    with session_scope() as s:
        # A tradeable name with a cached sector + a stored bar for today.
        repository.save_sector_map(
            s, [{"ticker": "NVDA", "cik": "1", "sic": "3674",
                 "sic_description": "Semi", "sector": INFO_TECH}]
        )
        repository.save_bars(
            s, [{"ticker": "NVDA", "date": as_of, "open": 100, "high": 101,
                 "low": 99, "close": 100.0, "volume": 5_000_000}]
        )

    async def fake_tickers():
        return [{"ticker": "NVDA", "cik": "1", "name": "NVIDIA",
                 "security_type": "CS", "exchange": None, "active": True}]

    monkeypatch.setattr(sec_edgar, "fetch_company_tickers", fake_tickers)

    with session_scope() as s:
        uni, _ = asyncio.run(builder.build_universe(as_of, s))
    assert "NVDA" in set(uni["ticker"])
    assert uni.set_index("ticker").loc["NVDA", "sector"] == INFO_TECH


class _NullClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False
