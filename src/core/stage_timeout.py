"""Per-stage timeout enforcement for long-running pipeline stages.

Wraps async stages with wall-clock limits. If a stage exceeds its timeout,
it raises StageTimeout, which the caller should catch and fail the run.
"""

from __future__ import annotations

import asyncio
import time


class StageTimeout(Exception):
    """Raised when a stage exceeds its wall-clock limit."""

    def __init__(self, stage: int, stage_name: str, elapsed_s: float, timeout_s: int) -> None:
        super().__init__(
            f"Stage {stage} ({stage_name}) exceeded {timeout_s}s timeout "
            f"(elapsed {elapsed_s:.1f}s)"
        )
        self.stage = stage
        self.stage_name = stage_name
        self.elapsed_s = elapsed_s
        self.timeout_s = timeout_s


async def enforce_stage_timeout(
    coro,
    stage: int,
    stage_name: str,
    timeout_s: int = 300,
) -> any:
    """Execute a coroutine with a stage-level timeout.

    If the coroutine takes longer than timeout_s, raises StageTimeout.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        elapsed = timeout_s  # asyncio.wait_for doesn't tell us exact elapsed
        raise StageTimeout(stage, stage_name, elapsed, timeout_s) from exc
