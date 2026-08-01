"""APScheduler worker entrypoint.

Runs at 06:00 America/New_York, weekdays only -- after overnight news and
before the 09:30 open. Market holidays are guarded with pandas_market_calendars,
so a cron that fires on Thanksgiving exits without burning API calls.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import signal
import sys

import structlog

from src.config.settings import get_settings
from src.logging_config import configure_logging
from src.pipeline import is_trading_day, run_pipeline
from src.storage.db import init_db
from src.validation.ic import backfill_forward_returns

log = structlog.get_logger(__name__)


async def daily_job() -> None:
    if not is_trading_day():
        log.info("skipping_non_trading_day", date=str(dt.date.today()))
        return
    try:
        # Backfill forward returns first: yesterday's scores can now be measured,
        # and doing it before the run means the report shows current IC.
        from src.storage.db import session_scope

        with session_scope() as session:
            backfill_forward_returns(session, dt.date.today())
    except Exception as exc:  # noqa: BLE001 - never block the run on this
        log.warning("forward_return_backfill_failed", error=str(exc))

    result = await run_pipeline()
    log.info(
        "daily_job_complete", run_id=result.run_id, names=len(result.dives),
        cost_usd=result.cost.get("cost_usd"), warnings=len(result.warnings),
    )


def build_scheduler():
    """Build (but do not start) the daily-funnel scheduler.

    Shared by the standalone worker (`python -m src.scheduler`) and the
    single-service mode where the api process runs it in-loop (see src/api.py).
    """
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    s = get_settings()
    scheduler = AsyncIOScheduler(timezone=s.run_timezone)
    scheduler.add_job(
        daily_job,
        CronTrigger(
            day_of_week="mon-fri", hour=s.run_hour, minute=s.run_minute,
            timezone=s.run_timezone,
        ),
        id="daily_funnel",
        max_instances=1,
        misfire_grace_time=3600,
        coalesce=True,
    )
    return scheduler


def main() -> None:
    configure_logging()
    init_db()
    s = get_settings()

    scheduler = build_scheduler()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    scheduler.start()
    log.info(
        "scheduler_started",
        schedule=f"{s.run_hour:02d}:{s.run_minute:02d} {s.run_timezone} mon-fri",
    )

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass
    try:
        loop.run_until_complete(stop.wait())
    finally:
        scheduler.shutdown(wait=False)
        loop.close()
        log.info("scheduler_stopped")


if __name__ == "__main__":
    if "--once" in sys.argv:
        configure_logging()
        init_db()
        asyncio.run(daily_job())
    else:
        main()
