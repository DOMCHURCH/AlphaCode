"""Schema migration / startup init.

Both the api and the worker call init_db() at startup, so it -- not a build-time
step -- is what actually migrates the real database on deploy. These tests lock
in that init_db creates the tables AND the performance indexes, idempotently.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect

from src.storage.db import EXTRA_INDEXES, init_db
from src.storage.models import ALL_TABLES


def test_init_db_creates_all_tables_and_indexes(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", future=True)
    init_db(engine)

    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert "report_artifacts" in tables, "the report artifact table was not created"
    for model in ALL_TABLES:
        assert model.__tablename__ in tables, f"missing table {model.__tablename__}"

    index_names: set[str] = set()
    for t in ("daily_bars", "fundamentals", "daily_scores"):
        index_names |= {i["name"] for i in insp.get_indexes(t)}
    for name, _ddl in EXTRA_INDEXES:
        assert name in index_names, f"startup migration did not create index {name}"


def test_init_db_is_idempotent(tmp_path):
    """A redeploy re-runs init_db; it must not error on the second pass."""
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", future=True)
    init_db(engine)
    init_db(engine)  # must not raise
    assert "report_artifacts" in inspect(engine).get_table_names()
