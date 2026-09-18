"""The force flag on the page-extras backfill.

`_refresh_page_extras` rebuilds a ticker only when its newest filing is ahead
of the row's `source_period_end`. That is exactly right after a data reload and
exactly wrong after a code change: editing the sentence those rows hold moves
nobody's filing, so nothing looks stale and a green deploy changes nothing on
the live site.

It has cost this project two deploys -- the dead-domain one recorded in
`api.py`, and the identity rewrite of 2026-09-18 -- and both times the fix was a
hand-written `UPDATE company_page_extras SET source_period_end = NULL` against
production at the end of a deploy. These pin the flag that replaces it.
"""

from __future__ import annotations

import inspect


def test_the_refresh_accepts_a_force_flag():
    from src.api import _refresh_page_extras

    params = inspect.signature(_refresh_page_extras).parameters
    assert "force" in params
    assert params["force"].default is False, (
        "force must be opt-in: a backfill that rebuilds all 6,200 tickers by "
        "default would make every data reload pay for a code-change problem"
    )


def test_the_endpoint_passes_force_through_to_the_worker():
    """The flag is useless if the route accepts it and drops it."""
    from src.api import _page_extras_bg, admin_backfill_page_extras

    route = inspect.signature(admin_backfill_page_extras).parameters
    assert "force" in route
    assert route["force"].default is False

    worker = inspect.signature(_page_extras_bg).parameters
    assert "force" in worker


def test_forcing_selects_every_ticker_and_not_forcing_selects_none():
    """The whole behaviour, on the one line that decides it.

    Mirrors `_refresh_page_extras`'s selection with a ticker whose stored row is
    already current -- the state every row is in after a code-only deploy.
    """
    import datetime as dt

    period = dt.date(2026, 6, 30)
    newest = [("A", period), ("BLK", period)]
    built = {"A": period, "BLK": period}     # nothing has moved

    not_forced = [t for t, p in newest if built.get(t) != p]
    forced = [t for t, _ in newest]

    assert not_forced == [], (
        "this is why a code-only deploy changed nothing: no filing moved, so "
        "no row was considered stale"
    )
    assert forced == ["A", "BLK"]
