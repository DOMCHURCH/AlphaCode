"""Schema migration entrypoint. Idempotent, safe to run on every deploy.

    python -m src.migrate

For a fresh database this creates every table. For an existing one it adds
anything missing and leaves existing data alone. It never drops or alters a
column -- destructive changes go through a hand-written Alembic revision in
/migrations so that a bad deploy cannot delete point-in-time history.
"""

from __future__ import annotations

import sys

import structlog
from sqlalchemy import inspect

from src.config.settings import get_settings
from src.logging_config import configure_logging
from src.storage.db import get_engine, init_db
from src.storage.models import Base

log = structlog.get_logger(__name__)


def main() -> int:
    configure_logging()
    s = get_settings()
    engine = get_engine()

    log.info("migrate_start", database=_redact(s.database_url))

    before = set(inspect(engine).get_table_names())
    # init_db creates tables AND the performance indexes; this is the same call
    # the services make at startup, so the CLI and the deploy path agree.
    init_db(engine)
    after = set(inspect(engine).get_table_names())
    created = sorted(after - before)

    log.info(
        "migrate_complete",
        created=created,
        total_tables=len(after),
        expected=len(Base.metadata.tables),
    )
    missing = set(Base.metadata.tables) - after
    if missing:
        log.error("migrate_incomplete", missing=sorted(missing))
        return 1
    return 0


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


if __name__ == "__main__":
    sys.exit(main())
