"""The boot warm-ups must not compete with the first visitors after a deploy.

`refresh_drawable` puts ~6,200 tickers through `build_view1` -- about twenty
queries each -- and `warm_identity` walks the universe again. Started together
against a five-connection pool, they saturate it, and every page request queues
behind them until they drain.

That is what "the first load takes forever and then it is fast" actually was:
not caching and not a cold start, but a deploy's own warm-up competing with the
people who arrived because of that deploy.
"""

from __future__ import annotations

import asyncio
import inspect


def test_the_walks_are_delayed_past_the_first_visitors():
    from src.api import _WARM_DELAY_S, _WARM_GAP_S

    assert _WARM_DELAY_S >= 30, (
        "the delay exists so a deploy's first readers are served from an idle "
        "pool; shortening it puts them back behind 124,000 queries"
    )
    assert _WARM_GAP_S > 0


def test_the_walks_run_one_at_a_time():
    """Sequential awaits, not three concurrent create_task calls."""
    from src.api import _warm

    src = inspect.getsource(_warm)
    assert "for name, fn in" in src, "the walks must share one loop"
    assert "create_task" not in src, (
        "a create_task here would restore the stampede this function replaced"
    )
    assert src.count("await asyncio.to_thread") == 1


def test_boot_starts_the_warm_up_without_awaiting_it():
    """Boot must not block on six thousand tickers."""
    from src.api import _boot

    src = inspect.getsource(_boot)
    assert "_warm(app)" in src
    assert "await _warm" not in src, "boot would stall behind the walks"


def test_the_cheapest_walk_goes_first():
    """The home page's five drawings are what an early visitor looks at, and
    they cost five tickers rather than six thousand."""
    from src.api import _warm

    src = inspect.getsource(_warm)
    order = [s for s in ("panels", "identity", "sitemap_drawable") if s in src]
    assert order == ["panels", "identity", "sitemap_drawable"]
    assert src.index('"panels"') < src.index('"sitemap_drawable"')


def test_a_failing_warm_up_never_stops_the_others():
    """Each step is optional: the sitemap falls back, the home page omits its
    identity line, the panels render on demand. One failure is a log line."""
    from src.api import _warm

    src = inspect.getsource(_warm)
    assert "except Exception" in src
    assert "warm_skipped" in src
    # The loop continues past a failure rather than returning out of it.
    assert "return" not in src.split("for name, fn in")[1]


def test_warm_is_a_coroutine():
    from src.api import _warm

    assert asyncio.iscoroutinefunction(_warm)
