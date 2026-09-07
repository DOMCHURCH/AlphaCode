"""Auto-updater tests.

The behaviour that matters here is not "does a timer fire" -- it is: does the
updater work out that it is BEHIND, from the data, and does it then leave a
persisted record that stops it running again five minutes later. Both halves
are tested against a real (file-backed SQLite) database, because both halves
are database behaviour.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

TODAY = dt.date(2026, 9, 1)  # a Tuesday


@pytest.fixture
def db(tmp_path, monkeypatch):
    """File-backed DB shared across the worker threads the scheduler uses."""
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'sched.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


# ---------------------------------------------------------------------------
# Pure date arithmetic
# ---------------------------------------------------------------------------
def test_last_completed_session_skips_the_weekend():
    from src.scheduler import last_completed_session

    assert last_completed_session(dt.date(2026, 9, 1)) == dt.date(2026, 8, 31)  # Tue
    assert last_completed_session(dt.date(2026, 8, 31)) == dt.date(2026, 8, 28)  # Mon
    assert last_completed_session(dt.date(2026, 8, 30)) == dt.date(2026, 8, 28)  # Sun
    assert last_completed_session(dt.date(2026, 8, 29)) == dt.date(2026, 8, 28)  # Sat


def test_quarter_bounds_cover_the_whole_quarter():
    from src.scheduler import quarter_bounds

    assert quarter_bounds(2026, 1) == (dt.date(2026, 1, 1), dt.date(2026, 3, 31))
    assert quarter_bounds(2026, 2) == (dt.date(2026, 4, 1), dt.date(2026, 6, 30))
    assert quarter_bounds(2026, 4) == (dt.date(2026, 10, 1), dt.date(2026, 12, 31))


def test_target_quarter_is_the_last_completed_one():
    from src.scheduler import target_quarter

    assert target_quarter(dt.date(2026, 9, 1)) == (2026, 2)
    assert target_quarter(dt.date(2026, 1, 15)) == (2025, 4)


def test_backoff_doubles_and_caps():
    from src.scheduler import backoff_hours

    assert [backoff_hours(n, 12) for n in (1, 2, 3, 4, 5)] == [0.5, 1, 2, 4, 8]
    assert backoff_hours(10, 12) == 12  # capped, not 256 hours


# ---------------------------------------------------------------------------
# Due checks read the data
# ---------------------------------------------------------------------------
def _add_bar(session, day: dt.date) -> None:
    from src.storage.models import DailyBar

    session.add(
        DailyBar(ticker="AAA", date=day, open=1, high=1, low=1, close=1, volume=1)
    )


def test_bars_are_due_when_the_table_is_empty(session):
    from src.scheduler import bars_status

    st = bars_status(session, TODAY)
    assert st["due"] is True
    assert st["detail"] == "no bars loaded"
    assert st["gap_days"] is None


def test_bars_are_not_due_when_current_through_the_last_session(session):
    from src.scheduler import bars_status

    _add_bar(session, dt.date(2026, 8, 31))
    session.flush()
    st = bars_status(session, TODAY)
    assert st["due"] is False
    assert "current through 2026-08-31" in st["detail"]


def test_bars_are_due_and_report_the_gap_when_stale(session):
    from src.scheduler import bars_status

    _add_bar(session, dt.date(2026, 8, 21))
    session.flush()
    st = bars_status(session, TODAY)
    assert st["due"] is True
    assert st["gap_days"] == 10
    assert "expected 2026-08-31" in st["detail"]


def _seed_fundamentals(session, filing_date: dt.date, n: int) -> None:
    from src.storage.models import Fundamental

    for i in range(n):
        session.add(
            Fundamental(
                ticker=f"T{i:03d}",
                metric="Assets",
                period_end=filing_date - dt.timedelta(days=30),
                filing_date=filing_date,
                value=1.0,
            )
        )


def _mark_dataset_loaded(session, job: str, quarter: str) -> None:
    from src.storage.models import JobState

    session.add(JobState(name=job, last_quarter_loaded=quarter))
    session.flush()


def test_fundamentals_due_until_the_target_quarters_dataset_has_loaded(session):
    from src.scheduler import fundamentals_status

    st = fundamentals_status(session, TODAY)
    assert st["due"] is True
    assert st["quarter"] == "2026q2"
    assert st["detail"] == "2026q2 not loaded yet"

    # Rows alone are NOT enough any more: the frames sweep writes rows for the
    # same quarter from a different endpoint, so rows prove nothing about
    # whether the authoritative ZIP was ever fetched.
    _seed_fundamentals(session, dt.date(2026, 5, 12), 40)
    session.flush()
    assert fundamentals_status(session, TODAY)["due"] is True

    _mark_dataset_loaded(session, "fundamentals", "2026q2")
    st = fundamentals_status(session, TODAY)
    assert st["due"] is False
    assert "2026q2 dataset loaded" in st["detail"]


def test_the_frames_sweep_cannot_retire_the_quarterly_dataset_job(session):
    """The regression this marker exists to prevent.

    A live run loaded 175,000 rows via frames, which filled the target quarter
    in the fundamentals table. Under a row-count-only check that reads as "the
    quarterly dataset is loaded", and the authoritative job -- the one that
    carries non-calendar filers and the fuller history -- would never run
    again.
    """
    from src.scheduler import fundamentals_status

    # Exactly what the frames sweep produces: plenty of rows, no dataset load.
    _seed_fundamentals(session, dt.date(2026, 5, 12), 500)
    session.flush()

    st = fundamentals_status(session, TODAY)
    assert st["due"] is True, "rows from another source must not mark it done"
    assert st["rows_in_quarter"] == 500
    assert st["dataset_quarter_loaded"] is None


def test_a_stale_marker_does_not_survive_a_wiped_table(session):
    """Bookkeeping never beats an empty table: a restored or wiped database
    must reload whatever the marker claims."""
    from src.scheduler import fundamentals_status

    _mark_dataset_loaded(session, "fundamentals", "2026q2")
    st = fundamentals_status(session, TODAY)
    assert st["due"] is True
    assert st["detail"] == "2026q2 not loaded yet"


def test_an_older_marker_leaves_the_job_due(session):
    from src.scheduler import fundamentals_status

    _seed_fundamentals(session, dt.date(2026, 5, 12), 40)
    _mark_dataset_loaded(session, "fundamentals", "2026q1")
    st = fundamentals_status(session, TODAY)
    assert st["due"] is True
    assert "last was 2026q1" in st["detail"]


def test_earnings_track_the_same_quarter(session):
    from src.scheduler import earnings_status
    from src.storage.models import EarningsEvent

    assert earnings_status(session, TODAY)["due"] is True
    for i in range(30):
        session.add(
            EarningsEvent(
                ticker=f"T{i:03d}",
                report_date=dt.date(2026, 5, 12),
                period_end=dt.date(2026, 3, 31),
            )
        )
    session.flush()
    assert earnings_status(session, TODAY)["due"] is True  # rows are not proof

    _mark_dataset_loaded(session, "earnings", "2026q2")
    st = earnings_status(session, TODAY)
    assert st["due"] is False
    assert "2026q2 dataset loaded" in st["detail"]


# ---------------------------------------------------------------------------
# enabled(): on in prod, off in dev, overridable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "env", "expected"),
    [
        ("auto", "prod", True),
        ("auto", "dev", False),
        ("on", "dev", True),
        ("off", "prod", False),
    ],
)
def test_enabled_modes(monkeypatch, mode, env, expected):
    from src.config.settings import get_settings
    from src.scheduler import enabled

    monkeypatch.setenv("AUTO_UPDATE", mode)
    monkeypatch.setenv("ENV", env)
    get_settings.cache_clear()
    try:
        assert enabled() is expected
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# tick(): runs what is due, then remembers that it did
# ---------------------------------------------------------------------------
def _only_job(monkeypatch, name, status_fn, run_fn):
    """Replace JOBS with a single job, so a tick tests one thing."""
    from src import scheduler

    job = scheduler.Job(
        name=name,
        status=status_fn,
        run=run_fn,
        min_hours=lambda s: 6.0,
        wait_hours=lambda s: 12.0,
        description="test job",
    )
    monkeypatch.setattr(scheduler, "JOBS", (job,))
    return job


def test_tick_runs_a_due_job_and_then_waits(db, monkeypatch):
    from src import scheduler

    calls = []

    async def run(_status):
        calls.append(1)
        return scheduler.Outcome("ok", 42, "loaded")

    _only_job(monkeypatch, "bars", lambda s, d: {"due": True, "detail": "behind"}, run)

    now = dt.datetime(2026, 9, 1, 12, 0)
    result = asyncio.run(scheduler.tick(now))
    assert result["ran"]["status"] == "ok"
    assert result["ran"]["rows"] == 42
    assert calls == [1]

    state = scheduler._read_state("bars")
    assert state["last_success_at"] == now
    assert state["next_earliest_at"] == now + dt.timedelta(hours=6)
    assert state["last_error"] is None

    # Ten minutes later it must NOT run again, even though the (stubbed) due
    # check still says due. This is the part a restart would otherwise lose.
    again = asyncio.run(scheduler.tick(now + dt.timedelta(minutes=10)))
    assert again["ran"] is None
    assert again["considered"][0]["skipped"] == "waiting"
    assert calls == [1]

    # Past the wait, it runs again.
    asyncio.run(scheduler.tick(now + dt.timedelta(hours=7)))
    assert calls == [1, 1]


def test_tick_skips_a_job_that_is_not_due(db, monkeypatch):
    from src import scheduler

    async def run(_status):  # pragma: no cover - must never be reached
        raise AssertionError("ran a job that was not due")

    _only_job(monkeypatch, "bars", lambda s, d: {"due": False, "detail": "fresh"}, run)
    result = asyncio.run(scheduler.tick(dt.datetime(2026, 9, 1, 12, 0)))
    assert result["ran"] is None
    assert result["considered"][0]["skipped"] == "current"


def test_a_failure_backs_off_and_the_loop_survives(db, monkeypatch):
    from src import scheduler

    async def run(_status):
        raise RuntimeError("sec.gov said no")

    _only_job(monkeypatch, "bars", lambda s, d: {"due": True, "detail": "behind"}, run)

    now = dt.datetime(2026, 9, 1, 12, 0)
    result = asyncio.run(scheduler.tick(now))
    assert result["ran"]["status"] == "error"

    state = scheduler._read_state("bars")
    assert state["consecutive_failures"] == 1
    assert "sec.gov said no" in state["last_error"]
    assert state["last_success_at"] is None
    assert state["next_earliest_at"] == now + dt.timedelta(hours=0.5)

    # Second consecutive failure doubles the wait rather than retrying at once.
    asyncio.run(scheduler.tick(now + dt.timedelta(hours=1)))
    state = scheduler._read_state("bars")
    assert state["consecutive_failures"] == 2
    assert state["next_earliest_at"] == now + dt.timedelta(hours=2)


def test_not_published_yet_is_not_a_failure(db, monkeypatch):
    from src import scheduler

    async def run(_status):
        return scheduler.Outcome("waiting", 0, "2026q2 not published by SEC yet")

    _only_job(
        monkeypatch, "fundamentals", lambda s, d: {"due": True, "detail": "behind"}, run
    )

    now = dt.datetime(2026, 9, 1, 12, 0)
    result = asyncio.run(scheduler.tick(now))
    assert result["ran"]["status"] == "waiting"

    state = scheduler._read_state("fundamentals")
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None
    assert state["last_success_at"] is None  # nothing landed, so nothing succeeded
    # Looks again on the waiting cadence, not the success one.
    assert state["next_earliest_at"] == now + dt.timedelta(hours=12)


def test_tick_yields_to_a_running_backfill(db, monkeypatch):
    from src import scheduler
    from src.locks import BACKFILL

    async def run(_status):  # pragma: no cover - must never be reached
        raise AssertionError("ran while a backfill held the lock")

    _only_job(monkeypatch, "bars", lambda s, d: {"due": True, "detail": "behind"}, run)

    async def scenario():
        async with BACKFILL:
            return await scheduler.tick(dt.datetime(2026, 9, 1, 12, 0))

    result = asyncio.run(scenario())
    assert result["ran"] is None
    assert result["skipped"] == "a backfill is already running"


def test_a_broken_probe_does_not_stop_the_loop(db, monkeypatch):
    from src import scheduler

    def boom(session, today):
        raise RuntimeError("no such column")

    async def run(_status):  # pragma: no cover - must never be reached
        raise AssertionError("ran off a failed probe")

    _only_job(monkeypatch, "bars", boom, run)
    result = asyncio.run(scheduler.tick(dt.datetime(2026, 9, 1, 12, 0)))
    assert result["ran"] is None
    assert "probe failed" in result["considered"][0]["skipped"]


def test_report_describes_every_job_before_anything_has_run(db):
    from src import scheduler

    rep = scheduler.report()
    assert [j["name"] for j in rep["jobs"]] == [
        "bars", "filings", "fundamentals", "earnings",
        # Company names, which is what makes searching by name rather than
        # by ticker work. Due on an empty database like every other data
        # job, because zero names IS name search being off.
        "names",
        # The renewal warning is reported like any other job, so
        # /admin shows whether it is due, off, or failing.
        "subscriptions",
    ]
    bars = rep["jobs"][0]
    assert bars["due"] is True  # empty database
    assert bars["last_success_at"] is None
    assert bars["next_earliest_at"] is None
    assert "does" in bars


def test_a_source_failure_that_returns_zero_rows_is_still_a_failure(db, monkeypatch):
    """The backfills report "every source failed" as state and a 0-row return,
    not as an exception. Zero rows also happens on a market holiday, so the
    updater must read the backfill's own verdict rather than infer one from the
    count -- otherwise a hard 404 is filed as "waiting" and never looks broken.
    """
    from src import backfill, scheduler

    async def fake_backfill_bars(days, end=None):
        backfill._update_state(
            phase="error",
            last_error="all wide-end sources failed: stooq: 404",
        )
        return 0

    monkeypatch.setattr(backfill, "backfill_bars", fake_backfill_bars)
    monkeypatch.setattr(
        scheduler, "JOBS",
        tuple(j for j in scheduler.JOBS if j.name == "bars"),
    )

    now = dt.datetime(2026, 9, 1, 12, 0)
    result = asyncio.run(scheduler.tick(now))
    assert result["ran"]["status"] == "error"
    assert "404" in result["ran"]["detail"]

    state = scheduler._read_state("bars")
    assert state["consecutive_failures"] == 1
    assert "404" in state["last_error"]
    assert state["next_earliest_at"] == now + dt.timedelta(hours=0.5)


def test_zero_rows_with_a_clean_backfill_is_only_waiting(db, monkeypatch):
    """The other side of the same coin: a holiday must NOT count as a failure."""
    from src import backfill, scheduler
    from src.storage.db import session_scope
    from src.storage.models import DailyBar

    async def fake_backfill_bars(days, end=None):
        backfill._update_state(phase="done", last_error=None)
        return 0

    monkeypatch.setattr(backfill, "backfill_bars", fake_backfill_bars)
    monkeypatch.setattr(
        scheduler, "JOBS",
        tuple(j for j in scheduler.JOBS if j.name == "bars"),
    )
    # One stale bar, so the job is due but the table is not empty.
    with session_scope() as s:
        s.add(DailyBar(ticker="AAA", date=dt.date(2026, 8, 20),
                       open=1, high=1, low=1, close=1, volume=1))

    now = dt.datetime(2026, 9, 1, 12, 0)
    result = asyncio.run(scheduler.tick(now))
    assert result["ran"]["status"] == "waiting"
    state = scheduler._read_state("bars")
    assert state["consecutive_failures"] == 0
    assert state["last_error"] is None


# ---------------------------------------------------------------------------
# The filings job -- the one that keeps the site current within a day
# ---------------------------------------------------------------------------
def test_filings_are_due_until_a_filing_from_the_last_session_is_held(session):
    from src.scheduler import filings_status

    st = filings_status(session, TODAY)
    assert st["due"] is True
    assert st["detail"] == "no filings loaded"

    # A filing from ten days ago: still sweeping for anything newer.
    _seed_fundamentals(session, dt.date(2026, 8, 21), 1)
    session.flush()
    st = filings_status(session, TODAY)
    assert st["due"] is True
    assert "sweeping for anything newer" in st["detail"]

    # A filing from the last completed session: current.
    _seed_fundamentals(session, dt.date(2026, 8, 31), 1)
    session.flush()
    st = filings_status(session, TODAY)
    assert st["due"] is False
    assert "current through 2026-08-31" in st["detail"]


def test_filings_run_before_the_quarterly_jobs(db):
    """Order matters: one job runs per tick, and the fresh source should be the
    one that gets the slot when several are behind."""
    from src import scheduler

    names = [j.name for j in scheduler.JOBS]
    assert names.index("filings") < names.index("fundamentals")
    assert names.index("filings") < names.index("earnings")


def test_report_covers_the_filings_job(db):
    from src import scheduler

    rep = scheduler.report()
    job = next(j for j in rep["jobs"] if j["name"] == "filings")
    assert job["due"] is True
    assert "frames" in job["does"]


# --------------------------------------------------------------- company names
def test_names_job_is_due_when_no_company_names_are_stored(db):
    """Zero names is not "no match" -- it is name search being switched off,
    and it looks identical to a reader typing something that does not exist.
    The job exists so that state cannot persist unnoticed."""
    from src import scheduler
    from src.storage.db import session_scope

    with session_scope() as s:
        status = scheduler.names_status(s, dt.date(2026, 9, 7))

    assert status["due"] is True
    assert "name search" in status["detail"]


def test_names_job_is_not_due_once_names_are_fresh(db):
    from src import scheduler
    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    today = dt.date(2026, 9, 7)
    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=today, ticker="WMT", name="Walmart Inc."))

    with session_scope() as s:
        assert scheduler.names_status(s, today)["due"] is False
        # A week later the listing set has moved on and it is worth re-reading.
        assert scheduler.names_status(s, today + dt.timedelta(days=8))["due"] is True
