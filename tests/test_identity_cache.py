"""The identity figure's cache: when it recomputes, and when it serves stale.

`_identity` is module state that outlives any one database, and it is written
by a background thread the API starts at boot. Those two facts together caused
a home-page test to read a figure counted from a different test's data. The
tests below pin the contract that resolves it.
"""

from __future__ import annotations

import threading
import time

import pytest

STALE = {"checkable": 999, "pass_rate_pct": 0.0, "drift": []}
FRESH = {"checkable": 1, "pass_rate_pct": 100.0, "drift": []}


@pytest.fixture
def cache():
    """Seed the module cache with a stale value and restore it afterwards."""
    import src.company.stats as stats

    stats._identity, stats._identity_at = dict(STALE), time.monotonic()
    try:
        yield stats
    finally:
        stats.reset_identity_cache()


class _Hog:
    """Holds the identity lock so the call under test meets real contention."""

    def __init__(self, stats):
        self._stats = stats
        self._holding = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        with self._stats._identity_lock:
            self._holding.set()
            self._release.wait(timeout=5)

    def __enter__(self):
        self._thread.start()
        assert self._holding.wait(timeout=5), "lock hog never started"
        return self

    def release(self):
        self._release.set()

    def __exit__(self, *_exc):
        self._release.set()
        self._thread.join(timeout=5)


def test_a_forced_recompute_waits_rather_than_serving_the_stale_value(
    cache, monkeypatch
):
    """`identity(max_age_s=0)` means "from the data as it is NOW".

    It used to answer with the PREVIOUS value whenever another thread held the
    lock -- silently, with no way for the caller to tell. That is how a startup
    warm could publish a count derived from data that is no longer there, and
    how one test's figure reached another test's assertion.
    """
    calls: list[int] = []
    monkeypatch.setattr(
        cache, "_compute_identity", lambda: (calls.append(1), dict(FRESH))[1]
    )

    result: dict = {}

    with _Hog(cache) as hog:
        caller = threading.Thread(
            target=lambda: result.update(value=cache.identity(max_age_s=0.0)),
            daemon=True,
        )
        caller.start()
        # While the lock is held the forced caller must still be waiting --
        # not off returning the stale value.
        caller.join(timeout=0.5)
        assert caller.is_alive(), "a forced recompute did not wait for the lock"
        assert calls == [], "it computed while another thread held the lock"

        hog.release()
        caller.join(timeout=5)

    assert result["value"] == FRESH
    assert calls == [1], "it must recompute exactly once"


def test_a_page_render_still_prefers_stale_over_waiting(cache, monkeypatch):
    """The other half of the trade, which must NOT change.

    An ordinary render passes the TTL. Queueing it behind a whole-universe walk
    would make one slow computation slow every reader with it, so a contended
    render answers immediately with whatever it has.
    """
    monkeypatch.setattr(
        cache, "_compute_identity", lambda: pytest.fail("render must not compute")
    )
    cache._identity_at = 0.0  # long expired, so the TTL cannot short-circuit

    with _Hog(cache):
        started = time.monotonic()
        assert cache.identity(max_age_s=1800.0) == STALE
        assert time.monotonic() - started < 1.0, "a render must not block"


def test_a_failed_recompute_keeps_the_last_known_good_figure(cache, monkeypatch):
    """A transient database error must not blank the home page's statistic."""
    monkeypatch.setattr(cache, "_compute_identity", lambda: None)

    assert cache.identity(max_age_s=0.0) == STALE


def test_resetting_the_cache_forgets_the_previous_database(cache):
    cache.reset_identity_cache()

    assert cache._identity is None
    assert cache._identity_at == 0.0
