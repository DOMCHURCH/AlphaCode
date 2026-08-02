"""The out-of-process run launcher.

The pipeline runs in a child process so a crash (OOM/segfault) can't take the
API down. The one thing the launcher must get right beyond spawning is
reconciliation: a child that is SIGKILL'd never writes its own failed RunLog, so
the parent has to, or /status is left polling a ghost "running" row forever.

No real subprocess is spawned here -- `create_subprocess_exec` is stubbed so we
can drive the exit code deterministically.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select

AS_OF = dt.date(2025, 6, 2)


@pytest.fixture
def run_db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runner.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def test_describe_exit_names_the_usual_killers():
    from src import runner

    assert runner._describe_exit(0) == "ok"
    assert "SIGKILL" in runner._describe_exit(-9)
    assert "OOM" in runner._describe_exit(-9)
    assert "SIGSEGV" in runner._describe_exit(-11)
    assert "code 1" in runner._describe_exit(1)


def _stub_exec(monkeypatch, *, returncode: int, on_wait=None):
    """Replace create_subprocess_exec with a fake that returns `returncode`.

    Captures the --run-id the runner generated so the test can assert against
    the exact RunLog. `on_wait` runs inside wait() to simulate what the child
    did before exiting (e.g. start its run row, then get killed).
    """
    captured: dict[str, str] = {}

    class _FakeProc:
        def __init__(self, run_id: str) -> None:
            self._run_id = run_id

        async def wait(self) -> int:
            if on_wait is not None:
                on_wait(self._run_id)
            return returncode

    async def _fake(*cmd, **kwargs):
        run_id = cmd[cmd.index("--run-id") + 1]
        captured["run_id"] = run_id
        return _FakeProc(run_id)

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake)
    return captured


def test_killed_child_is_reconciled_to_failed(run_db, monkeypatch):
    """A child that starts its run then gets OOM-killed leaves a `running` row.
    The runner must fail it so /status stops chasing a ghost."""
    from src import runner
    from src.storage import repository
    from src.storage.db import session_scope
    from src.storage.models import RunLog

    def _child_started_then_died(run_id: str) -> None:
        with session_scope() as s:
            repository.start_run(s, run_id, AS_OF)

    captured = _stub_exec(monkeypatch, returncode=-9, on_wait=_child_started_then_died)

    rc = asyncio.run(runner.run_pipeline_subprocess(AS_OF))
    assert rc == -9

    with session_scope() as s:
        row = s.execute(
            select(RunLog).where(RunLog.run_id == captured["run_id"])
        ).scalar_one()
        assert row.status == "failed"
        assert "SIGKILL" in row.error
        assert row.finished_at is not None


def test_child_that_recorded_its_own_failure_is_left_alone(run_db, monkeypatch):
    """On a clean Python failure the child writes its own failed RunLog with the
    real reason. The runner must not clobber it with a generic message."""
    from src import runner
    from src.storage import repository
    from src.storage.db import session_scope
    from src.storage.models import RunLog

    def _child_failed_cleanly(run_id: str) -> None:
        with session_scope() as s:
            repository.start_run(s, run_id, AS_OF)
            repository.finish_run(
                s, run_id, status="failed",
                error="insufficient history: 12/252 trading days.",
            )

    captured = _stub_exec(monkeypatch, returncode=1, on_wait=_child_failed_cleanly)

    rc = asyncio.run(runner.run_pipeline_subprocess(AS_OF))
    assert rc == 1

    with session_scope() as s:
        row = s.execute(
            select(RunLog).where(RunLog.run_id == captured["run_id"])
        ).scalar_one()
        assert row.status == "failed"
        # The child's specific reason survives; not overwritten by the runner.
        assert "insufficient history" in row.error


def test_successful_child_is_not_touched(run_db, monkeypatch):
    from src import runner
    from src.storage import repository
    from src.storage.db import session_scope
    from src.storage.models import RunLog

    def _child_succeeded(run_id: str) -> None:
        with session_scope() as s:
            repository.start_run(s, run_id, AS_OF)
            repository.finish_run(s, run_id, status="ok")

    captured = _stub_exec(monkeypatch, returncode=0, on_wait=_child_succeeded)

    rc = asyncio.run(runner.run_pipeline_subprocess(AS_OF))
    assert rc == 0

    with session_scope() as s:
        row = s.execute(
            select(RunLog).where(RunLog.run_id == captured["run_id"])
        ).scalar_one()
        assert row.status == "ok"
