"""Schema migration / startup init.

Both the api and the worker call init_db() at startup, so it -- not a build-time
step -- is what actually migrates the real database on deploy. These tests lock
in that init_db creates the tables AND the performance indexes, idempotently.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

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


def test_init_db_adds_a_column_missing_from_an_existing_table(tmp_path):
    """Reproduces the production failure: a table created before a model gained a
    column (sector_source) is missing it, because create_all never alters an
    existing table. init_db must add the column in place so queries stop failing
    with UndefinedColumn."""
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", future=True)
    init_db(engine)

    # Simulate the old schema: drop sector_source from sector_map, as it would be
    # on a Postgres created before the column was added to the model.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sector_map DROP COLUMN sector_source"))
    cols = {c["name"] for c in inspect(engine).get_columns("sector_map")}
    assert "sector_source" not in cols  # precondition: the drift exists

    init_db(engine)  # the heal

    cols = {c["name"] for c in inspect(engine).get_columns("sector_map")}
    assert "sector_source" in cols, "init_db did not add the missing column"
    # And a query that selects it now works (this is what crashed in prod).
    with engine.begin() as conn:
        conn.execute(text("SELECT ticker, sector, sector_source FROM sector_map"))


def test_init_db_syncs_all_drifted_sector_source_columns(tmp_path):
    """sector_source was added to three tables; all must be healed, not just the
    one that happened to be queried first."""
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}", future=True)
    init_db(engine)
    for tbl in ("sector_map", "universe", "daily_scores"):
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {tbl} DROP COLUMN sector_source"))

    init_db(engine)

    insp = inspect(engine)
    for tbl in ("sector_map", "universe", "daily_scores"):
        cols = {c["name"] for c in insp.get_columns(tbl)}
        assert "sector_source" in cols, f"{tbl}.sector_source not restored"
