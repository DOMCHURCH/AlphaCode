"""The auto-updater: the data keeps itself current, without anyone tapping a
button.

Two ideas do all the work here.

**Due is decided from the DATA, not from a clock.** Nothing fires "on the 15th".
Every job asks the database a question -- "is the newest bar older than the last
completed trading session?", "is the newest published SEC quarter actually in
the fundamentals table?" -- and runs when the answer says it is behind. That is
what makes late work catch itself up: a container that was down for a week, a
deploy that happened mid-quarter, a restored database, an SEC dataset that
published three weeks later than the last one. In every case the first tick
after the service is up sees the gap and closes it. A cron line would have
fired into the void and waited for the next one.

**Waiting is persisted.** Every attempt writes `job_state.next_earliest_at`
(see `src.storage.models.JobState`). Without that, a restart-happy platform
would turn a quarterly job into a per-deploy job, and a failure backoff into no
backoff at all.

SEC filings arrive by two routes, and both are here for a reason. The
`filings` job sweeps SEC's XBRL frames every few hours, which carries a 10-Q
within a day of it being filed. The `fundamentals` and `earnings` jobs load the
bulk Financial Statement Data Sets, which publish once, weeks after a quarter
closes, and are the authoritative and more complete record. Fast first,
thorough behind it -- and both write `source="sec"`, so a fact loaded by either
route lands on one row rather than two.

The bulk datasets publish on no announced date, so those jobs do not guess:
they look for the quarter, and if it is not up yet they say so and look again
in a few hours. That is a normal state, not an error, and it is reported as
"waiting" rather than left sitting in `last_error`.

One job runs per tick. These are whole-market downloads; overlapping them buys
nothing and doubles the load on the same upstreams.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import func, select

from src.config.settings import get_settings
from src.locks import BACKFILL
from src.storage.db import session_scope
from src.storage.models import DailyBar, EarningsEvent, Fundamental, JobState

log = structlog.get_logger(__name__)

# A quarter counts as "in the table" once this many rows carry a date inside it.
# Not 1: one stray row from another source should not convince the updater that
# a whole quarterly dataset has been loaded.
_QUARTER_LOADED_MIN_ROWS = 25


def _utcnow() -> dt.datetime:
    """Naive UTC, matching what every DateTime column in this schema stores."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Due checks -- pure where they can be, DB reads where they must be
# ---------------------------------------------------------------------------
def last_completed_session(today: dt.date) -> dt.date:
    """The most recent weekday strictly before `today`.

    Deliberately ignores market holidays. Treating a holiday as a trading day
    only means the bar job stays "due" across that day, and its minimum
    interval already bounds how often it may look. The opposite mistake --
    shipping a holiday calendar that silently drifts out of date -- would
    declare stale data fresh, which is the failure nobody notices.
    """
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d -= dt.timedelta(days=1)
    return d


def quarter_bounds(year: int, quarter: int) -> tuple[dt.date, dt.date]:
    """First and last day of a calendar quarter."""
    start = dt.date(year, 3 * (quarter - 1) + 1, 1)
    after = (
        dt.date(year + 1, 1, 1) if quarter == 4 else dt.date(year, 3 * quarter + 1, 1)
    )
    return start, after - dt.timedelta(days=1)


def target_quarter(today: dt.date) -> tuple[int, int]:
    """The newest SEC dataset quarter that should exist by `today`.

    `recent_quarters` already steps back off the in-progress quarter; the
    newest entry it returns is the one to chase. Whether SEC has actually
    published it is never assumed -- the job finds out by looking.
    """
    from src.ingest import sec_datasets

    return sec_datasets.recent_quarters(today, 1)[0]


def _quarter_row_count(
    session: Any, entity: Any, column: Any, year: int, q: int
) -> int:
    start, end = quarter_bounds(year, q)
    return session.execute(
        select(func.count())
        .select_from(entity)
        .where(column >= start, column <= end)
    ).scalar_one()


def bars_status(session: Any, today: dt.date) -> dict[str, Any]:
    """Whether price bars are behind, and by how much."""
    latest = session.execute(select(func.max(DailyBar.date))).scalar_one()
    expected = last_completed_session(today)
    behind = latest is None or latest < expected
    if latest is None:
        detail = "no bars loaded"
    elif behind:
        detail = (
            f"newest bar {latest.isoformat()}, "
            f"expected {expected.isoformat()}"
        )
    else:
        detail = f"current through {latest.isoformat()}"
    return {
        "due": behind,
        "latest": latest,
        "expected": expected,
        "gap_days": (expected - latest).days if latest else None,
        "detail": detail,
    }


def _dataset_quarter_loaded(session: Any, job: str) -> str | None:
    """The newest quarter whose bulk ZIP this job actually parsed.

    Read from `job_state` rather than inferred from the fundamentals table.
    Counting rows in the target quarter USED to answer this, and stopped being
    able to the moment the frames sweep began writing rows for the same
    quarter from a different endpoint: the table would say "loaded" while the
    authoritative dataset had never been fetched, and the quarterly job would
    retire itself.
    """
    row = session.get(JobState, job)
    return row.last_quarter_loaded if row is not None else None


def _bulk_status(
    session: Any, today: dt.date, job: str, entity: Any, column: Any, noun: str
) -> dict[str, Any]:
    """Shared shape for the two bulk-dataset jobs."""
    year, q = target_quarter(today)
    target = f"{year}q{q}"
    rows = _quarter_row_count(session, entity, column, year, q)
    marked = _dataset_quarter_loaded(session, job)
    # Empty table beats any marker: a wiped or restored database has to reload
    # whatever the bookkeeping claims.
    wiped = rows < _QUARTER_LOADED_MIN_ROWS
    loaded = marked == target and not wiped
    if loaded:
        detail = f"{target} dataset loaded ({rows:,} {noun} in the table)"
    elif wiped:
        detail = f"{target} not loaded yet"
    else:
        detail = (
            f"{target} dataset not loaded yet"
            + (f" (last was {marked})" if marked else "")
        )
    return {
        "due": not loaded,
        "quarter": target,
        "rows_in_quarter": rows,
        "dataset_quarter_loaded": marked,
        "detail": detail,
    }


def fundamentals_status(session: Any, today: dt.date) -> dict[str, Any]:
    """Whether the newest publishable SEC quarter's BULK dataset has loaded."""
    return _bulk_status(
        session, today, "fundamentals", Fundamental, Fundamental.filing_date,
        "filings",
    )


def filings_status(session: Any, today: dt.date) -> dict[str, Any]:
    """Whether anything filed since the last sweep is still missing.

    Measured on the newest `filing_date` we hold, not on a quarter boundary:
    this is the job whose whole purpose is that a 10-Q filed on Tuesday is on
    the site by Wednesday. It is therefore due most of the time by design --
    "due" here means "sweep at the next check", and the job's minimum interval,
    not the due flag, is what decides how often that actually happens.
    """
    newest = session.execute(select(func.max(Fundamental.filing_date))).scalar_one()
    expected = last_completed_session(today)
    behind = newest is None or newest < expected
    if newest is None:
        detail = "no filings loaded"
    elif behind:
        detail = f"newest filing {newest.isoformat()}; sweeping for anything newer"
    else:
        detail = f"current through {newest.isoformat()}"
    return {"due": behind, "newest_filing": newest, "detail": detail}


def earnings_status(session: Any, today: dt.date) -> dict[str, Any]:
    """The same check against `earnings_events`."""
    return _bulk_status(
        session, today, "earnings", EarningsEvent, EarningsEvent.report_date,
        "events",
    )


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Outcome:
    """What one job attempt did.

    `status` is one of:
      ok      -- it ran and the gap it was chasing is closed
      waiting -- it ran and the data still is not there, because the upstream
                 has not published it. Normal. Not an error, and it must not
                 count toward the failure backoff or land in `last_error`.
      error   -- it raised.
    """

    status: str
    rows: int
    detail: str


def _source_error() -> str | None:
    """The backfill's own verdict on the run that just finished.

    The backfill functions do NOT raise when every source fails -- they log it,
    set their state to `phase="error"`, and return 0 rows. Zero rows on its own
    is ambiguous: it is also what a market holiday looks like. So the error is
    read from where the backfill actually reports it, rather than inferred from
    the row count. Without this, a hard 404 from the price source would be
    filed as "waiting", retried on the gentle cadence, and never once show up
    as broken.
    """
    from src.backfill import get_backfill_state

    state = get_backfill_state()
    if state.get("phase") == "error":
        return str(state.get("last_error") or "the backfill reported an error")
    return None


async def _run_bars(status: dict[str, Any]) -> Outcome:
    from src.backfill import backfill_bars

    gap = status.get("gap_days")
    # A little more than the gap: enough to re-fetch the days around the hole
    # (a source that was mid-publish last time we looked), never the whole
    # history. With no bars at all, fall back to a real backfill window.
    days = 600 if gap is None else max(10, min(2000, gap + 5))
    rows = await backfill_bars(days)
    failure = _source_error()
    if failure:
        return Outcome("error", rows, failure)
    with session_scope() as session:
        after = bars_status(session, dt.date.today())
    if after["due"]:
        # Every source worked and the newest session still is not there, which
        # is what a market holiday and a not-yet-published EOD file both look
        # like. Say so plainly instead of calling it a failure.
        return Outcome("waiting", rows, f"{rows:,} rows; {after['detail']}")
    return Outcome("ok", rows, f"{rows:,} rows; {after['detail']}")


async def _run_sec(kind: str) -> Outcome:
    """fundamentals | earnings -- both come from the same quarterly SEC ZIPs."""
    from src.backfill import backfill_earnings, backfill_fundamentals

    s = get_settings()
    fn = backfill_fundamentals if kind == "fundamentals" else backfill_earnings
    rows = await fn(quarters=s.auto_update_quarters)

    # Record which quarter's ZIP actually parsed BEFORE judging the outcome:
    # that marker is what the due-check reads next time, and it must reflect
    # the download that happened, not the verdict we reach about it.
    from src.backfill import get_backfill_state

    target = "{}q{}".format(*target_quarter(dt.date.today()))
    if target in (get_backfill_state().get("quarters_loaded") or []):
        _write_state(kind, last_quarter_loaded=target)

    failure = _source_error()
    if failure:
        # "No quarter could be downloaded" is reported the same way: state, not
        # an exception. A throttle or an unreachable sec.gov belongs in the
        # backoff, not on the once-every-twelve-hours polling cadence.
        return Outcome("error", rows, failure)
    with session_scope() as session:
        after = (
            fundamentals_status(session, dt.date.today())
            if kind == "fundamentals"
            else earnings_status(session, dt.date.today())
        )
    if after["due"]:
        return Outcome(
            "waiting",
            rows,
            f"{after['quarter']} not published by SEC yet; will look again",
        )
    return Outcome("ok", rows, f"{rows:,} rows; {after['detail']}")


async def _run_filings(_status: dict[str, Any]) -> Outcome:
    from src.backfill import backfill_filings

    s = get_settings()
    rows = await backfill_filings(quarters=s.sec_frames_quarters)
    failure = _source_error()
    if failure:
        return Outcome("error", rows, failure)
    with session_scope() as session:
        after = filings_status(session, dt.date.today())
    if after["due"]:
        # Swept cleanly and still nothing newer than the last session, which is
        # simply what a day with no periodic filings looks like.
        return Outcome("waiting", rows, f"{rows:,} rows; {after['detail']}")
    return Outcome("ok", rows, f"{rows:,} rows; {after['detail']}")


async def _run_fundamentals(_status: dict[str, Any]) -> Outcome:
    return await _run_sec("fundamentals")


async def _run_earnings(_status: dict[str, Any]) -> Outcome:
    return await _run_sec("earnings")


@dataclass(frozen=True)
class Job:
    name: str
    # Reads the DB, returns {"due": bool, "detail": str, ...}
    status: Callable[[Any, dt.date], dict[str, Any]]
    run: Callable[[dict[str, Any]], Awaitable[Outcome]]
    # Floor between attempts, whatever the outcome.
    min_hours: Callable[[Any], float]
    # How long to wait after "the upstream does not have it yet".
    wait_hours: Callable[[Any], float]
    description: str


JOBS: tuple[Job, ...] = (
    Job(
        name="bars",
        status=bars_status,
        run=_run_bars,
        min_hours=lambda s: s.auto_update_bars_min_hours,
        wait_hours=lambda s: s.auto_update_bars_min_hours,
        description="daily price bars, through the last completed session",
    ),
    Job(
        name="filings",
        status=filings_status,
        run=_run_filings,
        min_hours=lambda s: s.auto_update_filings_min_hours,
        wait_hours=lambda s: s.auto_update_filings_min_hours,
        description=(
            "newly-filed numbers from SEC's XBRL frames, months ahead of the "
            "bulk datasets"
        ),
    ),
    Job(
        name="fundamentals",
        status=fundamentals_status,
        run=_run_fundamentals,
        min_hours=lambda s: s.auto_update_sec_recheck_hours,
        wait_hours=lambda s: s.auto_update_sec_recheck_hours,
        description="SEC as-reported filings, newest published quarter",
    ),
    Job(
        name="earnings",
        status=earnings_status,
        run=_run_earnings,
        min_hours=lambda s: s.auto_update_sec_recheck_hours,
        wait_hours=lambda s: s.auto_update_sec_recheck_hours,
        description="filing events, newest published quarter",
    ),
)


# ---------------------------------------------------------------------------
# Persisted state
# ---------------------------------------------------------------------------
def _read_state(name: str) -> dict[str, Any]:
    with session_scope() as session:
        row = session.get(JobState, name)
        if row is None:
            return {}
        return {
            "last_attempt_at": row.last_attempt_at,
            "last_success_at": row.last_success_at,
            "next_earliest_at": row.next_earliest_at,
            "last_rows": row.last_rows,
            "last_error": row.last_error,
            "last_detail": row.last_detail,
            "consecutive_failures": row.consecutive_failures,
        }


def _write_state(name: str, **fields: Any) -> None:
    with session_scope() as session:
        row = session.get(JobState, name)
        if row is None:
            row = JobState(name=name)
            session.add(row)
        for key, value in fields.items():
            setattr(row, key, value)


def backoff_hours(consecutive_failures: int, cap_hours: float) -> float:
    """30 minutes, doubling per consecutive failure, capped.

    Real errors only. A quarter SEC has not published yet is not a failure and
    never reaches this.
    """
    return min(cap_hours, 0.5 * (2 ** max(0, consecutive_failures - 1)))


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def enabled() -> bool:
    """AUTO_UPDATE: on | off | auto (= on in prod, off in dev).

    "auto" is the default so a deploy needs no extra variable to start keeping
    itself current, and a local test run never spawns a loop that talks to
    sec.gov.
    """
    s = get_settings()
    if s.auto_update == "on":
        return True
    if s.auto_update == "off":
        return False
    return s.env == "prod"


async def _record_failure(job: Job, message: str, now: dt.datetime) -> dict[str, Any]:
    """Count the failure, back off, and say when it will try again."""
    s = get_settings()
    state = await asyncio.to_thread(_read_state, job.name)
    fails = int(state.get("consecutive_failures") or 0) + 1
    wait = backoff_hours(fails, s.auto_update_backoff_max_hours)
    await asyncio.to_thread(
        _write_state,
        job.name,
        last_error=message[:300],
        last_detail=f"failed; retrying in ~{wait:g}h",
        consecutive_failures=fails,
        next_earliest_at=now + dt.timedelta(hours=wait),
    )
    log.error(
        "auto_update_failed", job=job.name, failures=fails,
        retry_in_hours=wait, error=message[:300],
    )
    return {
        "job": job.name, "status": "error",
        "detail": message[:200], "retry_in_hours": wait,
    }


async def _attempt(
    job: Job, status: dict[str, Any], now: dt.datetime
) -> dict[str, Any]:
    s = get_settings()
    await asyncio.to_thread(_write_state, job.name, last_attempt_at=now)
    log.info("auto_update_start", job=job.name, reason=status.get("detail"))
    try:
        outcome = await job.run(status)
    except Exception as exc:  # noqa: BLE001 - a failed job must not kill the loop
        log.exception("auto_update_raised", job=job.name, error=str(exc)[:300])
        return await _record_failure(job, f"{type(exc).__name__}: {exc}", now)

    # A job can fail without raising: the backfills report "every source
    # failed" as state and a 0-row return. Treated identically to an exception,
    # because to anyone reading /admin it IS the same thing.
    if outcome.status == "error":
        return await _record_failure(job, outcome.detail, now)

    hours = job.min_hours(s) if outcome.status == "ok" else job.wait_hours(s)
    fields: dict[str, Any] = {
        "last_rows": outcome.rows,
        "last_detail": outcome.detail[:200],
        "last_error": None,
        "consecutive_failures": 0,
        "next_earliest_at": now + dt.timedelta(hours=hours),
    }
    if outcome.status == "ok":
        fields["last_success_at"] = now
    await asyncio.to_thread(_write_state, job.name, **fields)
    log.info(
        "auto_update_done", job=job.name, status=outcome.status,
        rows=outcome.rows, detail=outcome.detail[:200],
    )
    return {
        "job": job.name, "status": outcome.status,
        "rows": outcome.rows, "detail": outcome.detail,
    }


async def tick(now: dt.datetime | None = None) -> dict[str, Any]:
    """One pass: run the first job that is both due and out of its wait.

    Returns what it did, for the log and for /admin. Never raises.
    """
    now = now or _utcnow()
    if BACKFILL.locked():
        return {"ran": None, "skipped": "a backfill is already running"}

    considered: list[dict[str, Any]] = []
    for job in JOBS:
        try:
            state = await asyncio.to_thread(_read_state, job.name)
            gate = state.get("next_earliest_at")
            if gate is not None and now < gate:
                considered.append(
                    {"job": job.name, "skipped": "waiting", "until": gate.isoformat()}
                )
                continue
            status = await asyncio.to_thread(_status_of, job, now.date())
        except Exception as exc:  # noqa: BLE001 - a broken probe must not stop the loop
            log.warning("auto_update_probe_failed", job=job.name, error=str(exc)[:200])
            considered.append(
                {"job": job.name, "skipped": f"probe failed: {exc}"[:120]}
            )
            continue

        if not status.get("due"):
            considered.append({"job": job.name, "skipped": "current"})
            continue

        async with BACKFILL:
            result = await _attempt(job, status, now)
        return {"ran": result, "considered": considered}

    return {"ran": None, "considered": considered}


def _status_of(job: Job, today: dt.date) -> dict[str, Any]:
    with session_scope() as session:
        return job.status(session, today)


async def run_forever() -> None:
    """Tick until cancelled. Started from the API's boot when enabled.

    The first pass happens shortly after boot rather than a full interval
    later: a deploy is exactly when the data is most likely to be behind, and
    waiting a quarter of an hour to notice serves nobody. The short delay keeps
    it off the healthcheck's critical path.
    """
    s = get_settings()
    log.info(
        "auto_update_enabled",
        tick_minutes=s.auto_update_tick_minutes,
        jobs=[j.name for j in JOBS],
    )
    await asyncio.sleep(20)
    while True:
        try:
            result = await tick()
            if result.get("ran"):
                log.info(
                    "auto_update_tick",
                    ran=result["ran"]["job"],
                    status=result["ran"]["status"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop outlives any one tick
            log.exception("auto_update_tick_failed", error=str(exc)[:300])
        # A little jitter so repeated deploys do not line up and hit the same
        # upstream on the same second.
        await asyncio.sleep(s.auto_update_tick_minutes * 60 + random.uniform(0, 30))


def report() -> dict[str, Any]:
    """What the auto-updater has done and what it is waiting on, for /admin.

    Cheap: DB reads only, no network. Each job shows its LIVE due-check next to
    its persisted state, so the page answers both "is it behind?" and "when
    will it look again?" -- and a job that has never run is visibly a job that
    has never run, rather than a blank.
    """
    s = get_settings()
    out: dict[str, Any] = {
        "enabled": enabled(),
        "mode": s.auto_update,
        "tick_minutes": s.auto_update_tick_minutes,
        "quarters_per_run": s.auto_update_quarters,
        "jobs": [],
    }
    today = dt.date.today()
    for job in JOBS:
        entry: dict[str, Any] = {"name": job.name, "does": job.description}
        try:
            state = _read_state(job.name)
            status = _status_of(job, today)
            last_success = state.get("last_success_at")
            last_attempt = state.get("last_attempt_at")
            next_earliest = state.get("next_earliest_at")
            entry.update(
                due=bool(status.get("due")),
                now=status.get("detail"),
                last_success_at=last_success.isoformat() if last_success else None,
                last_attempt_at=last_attempt.isoformat() if last_attempt else None,
                next_earliest_at=next_earliest.isoformat() if next_earliest else None,
                last_rows=state.get("last_rows"),
                last_detail=state.get("last_detail"),
                last_error=state.get("last_error"),
                consecutive_failures=state.get("consecutive_failures") or 0,
            )
        except Exception as exc:  # noqa: BLE001 - /admin must still render
            entry["error"] = str(exc)[:200]
        out["jobs"].append(entry)
    return out
