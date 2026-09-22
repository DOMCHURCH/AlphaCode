"""The two definitions of "checkable" have to be one definition.

The home page publishes a count of the companies whose balance sheets were
tested against A = L + E. It used to derive that count two ways, a few inches
apart on the same page: the stat strip summed `identity_breakdown()`'s
Reconciled and Flagged, which required the liabilities and equity COMPONENTS,
while "The check" ran off `universe_check.run_universe_check`, which also
accepts the filer's own stated `liabilities_and_equity` total. They disagreed
by ~686 companies, on the page whose entire argument is that numbers reconcile.

A filer who publishes total assets and their own stated right-hand-side total,
but no separate liabilities or equity line, IS testable -- that is the better
test, in fact, because the stated total carries none of the ambiguity of
choosing which equity tag to add. `_compute_breakdown` now admits it, and what
these tests bind is that the two functions agree about who can be checked.
"""

from __future__ import annotations

import datetime as dt

import pytest

Q = dt.date(2025, 12, 31)
FILED = dt.date(2026, 2, 13)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'identity.db'}")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    yield
    get_settings.cache_clear()
    reset_engine_cache()


def _add(ticker: str, metrics: dict[str, float], sector: str = "Financials") -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap

    with session_scope() as s:
        s.add(SectorMap(ticker=ticker, sector=sector, sector_source="sic"))
        for metric, value in metrics.items():
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=Q,
                fiscal_period="FY", filing_date=FILED, source="sec",
            ))


def _breakdown() -> dict:
    from src.company.stats import identity_breakdown, reset_breakdown_cache

    reset_breakdown_cache()
    b = identity_breakdown()
    assert b is not None, "the breakdown must compute on a seeded database"
    return b


def test_a_filer_with_only_a_stated_total_is_checked_not_skipped(db):
    """Assets and the filer's own right-hand-side total, no components.

    This is the shape the old gate dropped: it demanded total_liabilities AND
    an equity tag, so a filer who published neither but DID publish
    `liabilities_and_equity` was counted as having "no identity to test" when
    the identity was sitting right there.
    """
    _add("AAA", {"total_assets": 1_000e9, "liabilities_and_equity": 1_000e9})

    b = _breakdown()

    assert b["checked"] == 1, "the stated total is an identity to test"
    assert b["not_testable"] == 0
    assert b["reconciled"] == 1
    assert b["flagged"] == 0
    assert b["counts"]["balanced"] == 1


def test_a_stated_total_that_disagrees_with_assets_is_a_broken_filing(db):
    """Their arithmetic, not ours -- which is what "broken" means here.

    It cannot be `missing_tag`: that category means the filer's own totals
    agree and a credit-side line did not reach us. With no components there is
    nothing of ours that could have fallen short.
    """
    _add("BBB", {"total_assets": 1_000e9, "liabilities_and_equity": 1_200e9})

    b = _breakdown()

    assert b["checked"] == 1
    assert b["flagged"] == 1
    assert b["counts"]["broken"] == 1
    assert b["counts"]["missing_tag"] == 0
    assert b["counts"]["unexplained"] == 0
    assert "BBB" in (b["examples"].get("broken") or [])


def test_a_stated_total_inside_presentation_slack_is_rounding(db):
    """Under 1% of total assets is the same slack the component path allows."""
    _add("CCC", {"total_assets": 1_000e9, "liabilities_and_equity": 1_006e9})

    b = _breakdown()

    assert b["checked"] == 1
    assert b["counts"]["rounding"] == 1, b["counts"]


def test_assets_alone_is_still_not_testable(db):
    """The fallback must not turn "no right-hand side at all" into a pass."""
    _add("DDD", {"total_assets": 1_000e9})

    b = _breakdown()

    assert b["checked"] == 0
    assert b["not_testable"] == 1
    assert b["reconciled"] == 0
    assert b["flagged"] == 0


def test_components_still_win_where_the_filer_published_them(db):
    """The fallback is a fallback. A filer with components takes the old path,
    where the NCI and mezzanine explanations are available -- the stated-total
    comparison cannot see those."""
    _add("EEE", {
        "total_assets": 1_000e9,
        "total_liabilities": 600e9,
        "total_equity": 400e9,
        "liabilities_and_equity": 1_000e9,
    })

    b = _breakdown()

    assert b["checked"] == 1
    assert b["counts"]["balanced"] == 1


def test_the_two_definitions_of_checkable_now_agree(db):
    """THE POINT. `_compute_breakdown`'s checked and `run_universe_check`'s
    checkable are two implementations of one question, and the home page
    publishes both. On a universe carrying every shape at once they must
    return the same number."""
    _add("AAA", {"total_assets": 1_000e9, "liabilities_and_equity": 1_000e9})
    _add("BBB", {"total_assets": 1_000e9, "liabilities_and_equity": 1_200e9})
    _add("CCC", {
        "total_assets": 1_000e9, "total_liabilities": 600e9,
        "total_equity": 400e9,
    })
    _add("DDD", {"total_assets": 1_000e9})
    _add("EEE", {"total_liabilities": 600e9, "total_equity": 400e9})

    from src.company.universe_check import run_universe_check

    b = _breakdown()
    u = run_universe_check()["identity"]

    assert b["checked"] == u["checkable"], (
        f"identity_breakdown checked {b['checked']} companies and "
        f"universe_check checked {u['checkable']}; the home page publishes "
        "both and they must be one number"
    )
    assert b["reconciled"] + b["flagged"] == b["checked"], (
        "every checked company lands in exactly one of the two buckets the "
        "stat strip adds up"
    )


def test_the_sector_split_counts_the_fallback_too(db):
    """`by_sector` is built alongside the global counts so the sector hubs and
    the site-wide split cannot disagree. A new path that bumped only the
    global counter would break that silently."""
    _add("AAA", {"total_assets": 1_000e9, "liabilities_and_equity": 1_000e9},
         sector="Financials")

    b = _breakdown()
    split = b["by_sector"]["Financials"]

    assert split["balanced"] == 1, b["by_sector"]
