"""Cross-process response cache. Without Redis the in-process dict is lost every
subprocess run, so SEC/GDELT re-hit the network every time; the Postgres cache
makes a same-day re-run served from the DB."""

from __future__ import annotations

import datetime as dt

import pytest


@pytest.fixture
def cache_db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.cache import reset_redis_cache
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
    monkeypatch.setenv("REDIS_URL", "")  # force the Postgres cache path
    get_settings.cache_clear()
    reset_engine_cache()
    reset_redis_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()
        reset_redis_cache()


def test_pg_cache_persists_and_reads_back(cache_db):
    from src.storage.cache import cache_get_json, cache_set_json

    assert cache_get_json("cache:sec:abc") is None  # miss
    cache_set_json("cache:sec:abc", {"sic": "3674", "name": "NVDA"}, ttl=24 * 3600)
    # A fresh session_scope per call simulates a new subprocess reading the cache.
    assert cache_get_json("cache:sec:abc") == {"sic": "3674", "name": "NVDA"}


def test_pg_cache_respects_ttl(cache_db):
    from sqlalchemy import update

    from src.storage.cache import cache_get_json, cache_set_json
    from src.storage.db import session_scope
    from src.storage.models import CacheEntry

    cache_set_json("cache:gdelt:xyz", {"tone": 1.0}, ttl=6 * 3600)
    assert cache_get_json("cache:gdelt:xyz") == {"tone": 1.0}
    # Force it stale.
    with session_scope() as s:
        s.execute(
            update(CacheEntry)
            .where(CacheEntry.cache_key == "cache:gdelt:xyz")
            .values(expires_at=dt.datetime(2000, 1, 1))
        )
    assert cache_get_json("cache:gdelt:xyz") is None  # expired -> miss


def test_pg_cache_upserts_on_repeat_key(cache_db):
    from src.storage.cache import cache_get_json, cache_set_json

    cache_set_json("cache:sec:k", {"v": 1}, ttl=3600)
    cache_set_json("cache:sec:k", {"v": 2}, ttl=3600)  # same key -> update, no PK clash
    assert cache_get_json("cache:sec:k") == {"v": 2}
