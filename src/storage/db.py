"""Engine/session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import structlog
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.config.settings import get_settings
from src.storage.models import Base

log = structlog.get_logger(__name__)

# Indexes that materially change query plans at 6000 names x 400 days but that
# SQLAlchemy will not create from the model definitions alone. Applied at
# startup (see init_db) so they land on the real database rather than depending
# on a build-phase step where DATABASE_URL may not yet be resolved. Portable:
# `IF NOT EXISTS` and `DESC` index columns work on both SQLite and Postgres.
EXTRA_INDEXES: list[tuple[str, str]] = [
    (
        "ix_bars_ticker_date_desc",
        "CREATE INDEX IF NOT EXISTS ix_bars_ticker_date_desc "
        "ON daily_bars (ticker, date DESC)",
    ),
    (
        "ix_fund_lookup",
        "CREATE INDEX IF NOT EXISTS ix_fund_lookup "
        "ON fundamentals (ticker, metric, filing_date)",
    ),
    (
        "ix_scores_date_rank",
        "CREATE INDEX IF NOT EXISTS ix_scores_date_rank "
        "ON daily_scores (as_of_date, final_rank)",
    ),
]


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    s = get_settings()
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if s.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        # Fail fast on an unreachable/slow Postgres instead of hanging the
        # startup migration (and with it the healthcheck) for the driver's
        # default multi-minute timeout.
        kwargs["connect_args"] = {"connect_timeout": 10}
    return create_engine(s.database_url, **kwargs)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on clean exit, rolls back on exception."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Create all tables and performance indexes. Idempotent.

    Called at service startup (api + worker) as well as by `src.migrate` and the
    tests, so a deploy migrates itself against the real database without relying
    on a build-time step.
    """
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for name, ddl in EXTRA_INDEXES:
            try:
                conn.execute(text(ddl))
            except Exception as exc:  # noqa: BLE001 - a missing index is survivable
                log.warning("index_create_failed", index=name, error=str(exc)[:200])


def reset_engine_cache() -> None:
    """Test helper: drop memoised engine/session so a new DATABASE_URL applies."""
    get_engine.cache_clear()
    get_session_factory.cache_clear()
