"""Run the funnel in a child process.

The pipeline is memory-heavy and calls into native code (numpy, and kaleido /
headless Chromium at the report stage) that can segfault or be OOM-killed --
neither of which is a catchable Python exception. If it runs inside the uvicorn
worker, any such death takes the web server down with it, and with it /status,
the one thing that is supposed to survive so the UI can report the failure.

So the API never hosts the compute. It spawns ``python -m src.pipeline`` as a
child, which writes its RunLog and per-stage checkpoints to the shared Postgres;
/status polls those rows. A child that dies without finishing (SIGKILL/SIGSEGV)
is reconciled here -- its RunLog is marked failed -- instead of being left as a
ghost "running" row that the site would poll forever.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
import sys
import uuid
from typing import Any

import structlog

from src.storage import repository
from src.storage.db import session_scope

log = structlog.get_logger(__name__)


def _describe_exit(rc: int) -> str:
    """Human-readable cause for a child exit code, naming the usual killers."""
    if rc == 0:
        return "ok"
    if rc < 0:  # killed by signal -N
        sig = -rc
        if sig == signal.SIGKILL:
            return "SIGKILL — almost always the OOM killer (raise memory or reduce the load)"
        if sig == signal.SIGSEGV:
            return "SIGSEGV — a native segfault (e.g. kaleido/Chromium in a constrained container)"
        try:
            return f"killed by {signal.Signals(sig).name}"
        except ValueError:
            return f"killed by signal {sig}"
    return f"exited with code {rc}"


async def run_pipeline_subprocess(
    as_of: dt.date | None, skip_llm: bool = False
) -> int:
    """Spawn the funnel as a child process; return its exit code.

    The run_id is generated here (not in the child) so that if the child dies
    before writing its own failure, we can still find and fail the exact RunLog
    it started. The child inherits our stdout/stderr, so its structured logs land
    in the same place as everything else (Railway captures the pipe).
    """
    resolved = as_of or _resolve_last_trading_day()
    run_id = f"{resolved.isoformat()}-{uuid.uuid4().hex[:8]}"

    cmd = [
        sys.executable, "-m", "src.pipeline",
        "--run-id", run_id,
        "--date", resolved.isoformat(),
    ]
    if skip_llm:
        cmd.append("--skip-llm")

    log.info("run_subprocess_spawn", run_id=run_id, as_of=str(resolved), skip_llm=skip_llm)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        # Pipe the child's stdout so we can BOTH echo it to our own stdout (Railway
        # still captures every line) AND tee it into the /diagnostics log ring, so
        # the pipeline's stage logs show up there even though it runs out-of-process.
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    await _tee_child_output(proc)
    rc = await proc.wait()

    if rc == 0:
        log.info("run_subprocess_done", run_id=run_id, returncode=rc)
        return rc

    # Non-zero exit. On a clean Python failure the child already wrote its own
    # failed RunLog (pipeline's except block); on a SIGKILL/segfault it did not,
    # so reconcile the ghost here. fail_run_if_running is a no-op if the child
    # already recorded the outcome.
    reason = _describe_exit(rc)
    log.error("run_subprocess_failed", run_id=run_id, returncode=rc, cause=reason)
    try:
        with session_scope() as session:
            reconciled = repository.fail_run_if_running(
                session, run_id, f"run process died: {reason}"
            )
        if reconciled:
            log.warning("run_subprocess_reconciled_ghost", run_id=run_id, cause=reason)
    except Exception as exc:  # noqa: BLE001 - reconcile is best-effort
        log.error("run_subprocess_reconcile_failed", run_id=run_id, error=str(exc)[:300])
    return rc


async def _tee_child_output(proc: Any) -> None:
    """Drain the child's stdout: echo each line to our stdout and ring-buffer it.

    Draining continuously also prevents the child blocking on a full pipe. Any
    failure here is non-fatal -- the run itself is what matters, not the tee.
    """
    from src.logging_config import record_log_line

    if proc.stdout is None:
        return
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace")
            sys.stdout.write(line)  # keep Railway's stream complete
            sys.stdout.flush()
            record_log_line(line, src="run")
    except Exception as exc:  # noqa: BLE001 - never let log plumbing kill the run
        log.warning("run_subprocess_tee_failed", error=str(exc)[:200])


def _resolve_last_trading_day() -> dt.date:
    # Imported lazily so this module stays cheap to import from the API process.
    from src.pipeline import _last_trading_day

    return _last_trading_day()
