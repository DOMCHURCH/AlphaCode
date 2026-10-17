"""The dataset's row count is expensive and every page quotes it.

`file_row_count` is a few seconds of database work. It must never be paid twice
by readers: shared between simultaneous cold requests, served stale while one
background thread refreshes, kept when a refresh fails, and warmed at boot.
These tests drive `_Memo` with fake computations so they are fast and exact.
"""

from __future__ import annotations

import threading
import time

import pytest

from src.dataset import CountUnavailable, _Memo


def _wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Counter:
    def __init__(self, value=1, delay=0.0):
        self.calls = 0
        self.value = value
        self.delay = delay
        self.fail = False
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("database away")
        return self.value


def test_a_fresh_value_is_served_without_computing_again():
    m, c = _Memo(60), Counter(7)
    assert m.get(c) == 7 and m.get(c) == 7 and m.get(c) == 7
    assert c.calls == 1


def test_simultaneous_cold_requests_share_one_computation():
    """Six cold requests used to run six full queries (5.7 to 10.8 s each against
    the live table). Now one computes and the rest wait for that result."""
    m, c = _Memo(60, cold_wait_s=5), Counter(42, delay=0.3)
    out: list[int] = []
    ths = [threading.Thread(target=lambda: out.append(m.get(c))) for _ in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert out == [42] * 6
    assert c.calls == 1


def test_a_cold_request_that_waits_too_long_gets_the_fallback_not_a_hang():
    m, c = _Memo(60, cold_wait_s=0.1), Counter(5)
    c.gate = threading.Event()
    first = threading.Thread(target=lambda: m.get(c))
    first.start()
    assert _wait_until(lambda: c.calls == 1)
    with pytest.raises(CountUnavailable):
        m.get(c)                      # arrives while the first is still working
    c.gate.set()
    first.join()
    assert m.get(c) == 5              # and the first computation was kept
    assert c.calls == 1


def test_a_stale_value_is_served_at_once_while_one_background_refresh_runs():
    m, c = _Memo(60), Counter(1)
    assert m.get(c) == 1
    m.age(1000)                       # past its TTL
    c.value = 2
    c.gate = threading.Event()        # hold the refresh open
    t0 = time.monotonic()
    assert m.get(c) == 1              # the old value, immediately
    assert m.get(c) == 1              # and again: still just one refresh
    assert time.monotonic() - t0 < 0.5
    assert _wait_until(lambda: c.calls == 2) and m.refreshing
    c.gate.set()
    assert _wait_until(lambda: not m.refreshing)
    assert m.get(c) == 2 and c.calls == 2


def test_a_failed_refresh_keeps_the_old_value_and_retries_later():
    m, c = _Memo(60, retry_s=30), Counter(1)
    assert m.get(c) == 1
    m.age(1000)
    c.fail = True
    assert m.get(c) == 1
    assert _wait_until(lambda: not m.refreshing)
    assert c.calls == 2
    assert m.get(c) == 1 and c.calls == 2   # not retried straight away
    m.age(31)                               # retry_s later
    c.fail, c.value = False, 3
    assert m.get(c) == 1                    # still the old value at once
    assert _wait_until(lambda: not m.refreshing)
    assert m.get(c) == 3


def test_a_reset_discards_a_computation_that_was_already_running():
    """A stale result must not land after somebody asked for a fresh one."""
    m, c = _Memo(60, cold_wait_s=5), Counter(1)
    c.gate = threading.Event()
    got: list[int] = []
    t = threading.Thread(target=lambda: got.append(m.get(c)))
    t.start()
    assert _wait_until(lambda: c.calls == 1)
    m.reset()
    c.gate.set()
    t.join()
    assert got == [1]                 # the caller still gets its answer...
    c2 = Counter(9)
    assert m.get(c2) == 9             # ...but the cache did not keep it


def test_facts_label_falls_back_to_blank_when_the_count_is_unavailable(monkeypatch):
    from src import dataset

    def boom():
        raise CountUnavailable("still working")

    monkeypatch.setattr(dataset, "row_count", boom)
    assert dataset.facts_label() == ""


def test_warm_fills_both_caches_so_the_first_reader_computes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'warm.db'}")
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    from src import dataset

    dataset.reset_count_cache()
    calls = []
    real = dataset.file_row_count
    monkeypatch.setattr(dataset, "file_row_count", lambda: calls.append(1) or real())
    dataset.warm()
    assert len(calls) == 1
    dataset.row_count()
    dataset.shape()
    dataset.facts_label()
    assert len(calls) == 1
    get_settings.cache_clear()
    reset_engine_cache()


def test_the_boot_warm_up_skips_sqlite_and_runs_on_postgres(monkeypatch):
    """SQLite is dev and tests: counting there is instant, and a boot-time count
    would race whatever the test seeds next."""
    import asyncio

    from src import api, dataset

    warmed = []
    monkeypatch.setattr(dataset, "warm", lambda: warmed.append(1))

    class S:
        is_sqlite = True

    monkeypatch.setattr(api, "get_settings", lambda: S())
    asyncio.run(api._warm_dataset_count())
    assert warmed == []

    S.is_sqlite = False
    asyncio.run(api._warm_dataset_count())
    assert warmed == [1]


def test_the_boot_warm_up_never_raises(monkeypatch):
    import asyncio

    from src import api, dataset

    def boom():
        raise RuntimeError("database away")

    class S:
        is_sqlite = False

    monkeypatch.setattr(dataset, "warm", boom)
    monkeypatch.setattr(api, "get_settings", lambda: S())
    asyncio.run(api._warm_dataset_count())   # a warm-up is never critical
