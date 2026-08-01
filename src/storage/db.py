"""Engine/session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config.settings import get_settings
from src.storage.models import Base


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    s = get_settings()
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if s.is_sqlite:
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
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
    """Create all tables. Idempotent. Used by migrations and by tests."""
    Base.metadata.create_all(engine or get_engine())


def reset_engine_cache() -> None:
    """Test helper: drop memoised engine/session so a new DATABASE_URL applies."""
    get_engine.cache_clear()
    get_session_factory.cache_clear()
