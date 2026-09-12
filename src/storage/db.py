"""Engine/session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

import structlog
from sqlalchemy import Engine, create_engine, inspect, text
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


def _sync_added_columns(engine: Engine) -> None:
    """Add columns present in the models but missing from an existing table.

    `create_all` creates missing TABLES but never adds a new COLUMN to a table
    that already exists -- so a column added to a model after the table was first
    created (e.g. `sector_source`) is silently absent on a long-lived Postgres,
    and every query that selects it fails with UndefinedColumn. This closes that
    gap: it diffs each model against the live table and issues an additive-only
    `ALTER TABLE ADD COLUMN` for anything missing. Additive and idempotent -- it
    never drops or retypes a column, and skips a not-null column with no default
    (that needs a real migration, not a silent one). Works on Postgres + SQLite.
    """
    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue  # create_all just made it, with every column
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            if not col.nullable and col.server_default is None and col.default is None:
                log.warning(
                    "column_needs_manual_migration",
                    table=table.name, column=col.name,
                    reason="not-null with no default cannot be added in place",
                )
                continue
            coltype = col.type.compile(dialect=engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                log.info("column_added", table=table.name, column=col.name, type=coltype)
            except Exception as exc:  # noqa: BLE001 - one failed add must not stall boot
                log.warning(
                    "column_add_failed", table=table.name, column=col.name,
                    error=str(exc)[:200],
                )


# Columns whose declared length has GROWN since the table was first created.
# `_sync_added_columns` deliberately never retypes anything -- a silent retype
# is how data gets truncated -- so a widening is listed here by hand, one line
# per change, and each one is a decision somebody made rather than a diff.
#
# Widening a varchar is metadata-only on Postgres (no table rewrite) and a
# no-op on SQLite, which does not enforce declared lengths at all.
_WIDENED_COLUMNS: tuple[tuple[str, str, int], ...] = (
    # DEMO_API_KEY is operator-chosen and a long one would not fit in 64.
    ("api_users", "api_key", 128),
)


def _widen_columns(engine: Engine) -> None:
    """Grow the columns in `_WIDENED_COLUMNS` if the live table is narrower.

    Only ever grows. A column already at or above the target is left alone, so
    this is idempotent and cannot shorten anything.
    """
    if engine.dialect.name != "postgresql":
        return  # SQLite ignores varchar lengths entirely
    insp = inspect(engine)
    for table, column, want in _WIDENED_COLUMNS:
        if not insp.has_table(table):
            continue
        try:
            current = next(
                (c for c in insp.get_columns(table) if c["name"] == column), None
            )
            if current is None:
                continue
            have = getattr(current["type"], "length", None)
            if have is None or have >= want:
                continue
            with engine.begin() as conn:
                conn.execute(text(
                    f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                    f"TYPE VARCHAR({want})"
                ))
            log.info("column_widened", table=table, column=column,
                     from_=have, to=want)
        except Exception as exc:  # noqa: BLE001 - a failed widen must not stall boot
            log.warning("column_widen_failed", table=table, column=column,
                        error=str(exc)[:200])


def hash_plaintext_api_keys(engine: Engine) -> int:
    """Replace any key still stored in the clear with its digest. Returns the
    count changed.

    A data migration and not a schema one, so it lives here rather than in
    `_sync_added_columns`, and it runs AFTER that call because it writes the
    `api_key_prefix` column that call adds.

    Idempotent, and the test for "already done" is exact rather than a guess:
    a digest is 64 hex characters, and an issued key is `token_urlsafe(32)` --
    43 characters, from an alphabet that includes `-`, `_` and upper case. No
    live key can be mistaken for a digest, in either direction.

    A key found in the clear is hashed IN PLACE and keeps working. The prefix
    is recoverable here and only here: after this runs, the plaintext is gone
    from the database for good, which is the point.
    """
    from src.accounts import hash_api_key, key_prefix, looks_hashed

    changed = 0
    try:
        insp = inspect(engine)
        if not insp.has_table("api_users"):
            return 0
        columns = {c["name"] for c in insp.get_columns("api_users")}
        if "api_key_prefix" not in columns:
            # The ADD COLUMN above failed. Hashing now would destroy the only
            # copy of the key and leave nothing to display in its place.
            log.warning("api_key_hash_skipped", reason="api_key_prefix missing")
            return 0
        with engine.begin() as conn:
            rows = conn.execute(
                text('SELECT id, api_key FROM api_users')
            ).all()
            for row_id, stored in rows:
                if looks_hashed(stored or ""):
                    continue
                conn.execute(
                    text(
                        'UPDATE api_users SET api_key = :h, api_key_prefix = :p '
                        "WHERE id = :i"
                    ),
                    {"h": hash_api_key(stored or ""), "p": key_prefix(stored or ""),
                     "i": row_id},
                )
                changed += 1
    except Exception as exc:  # noqa: BLE001 - a failed migration must not stall boot
        log.warning("api_key_hash_failed", error=str(exc)[:200])
        return 0
    if changed:
        log.info("api_keys_hashed", rows=changed)
    return changed


def init_db(engine: Engine | None = None) -> None:
    """Create all tables, sync added columns, and build performance indexes.

    Idempotent. Called at service startup (api + worker) as well as by
    `src.migrate` and the tests, so a deploy migrates itself against the real
    database without relying on a build-time step.
    """
    engine = engine or get_engine()
    Base.metadata.create_all(engine)
    _sync_added_columns(engine)
    _widen_columns(engine)
    hash_plaintext_api_keys(engine)
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
