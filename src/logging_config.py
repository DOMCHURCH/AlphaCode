"""Structured logging via structlog.

Two things this module is careful about, both learned the hard way in a
constrained Railway container:

1. Rendering under uvicorn. `logging.basicConfig` is a no-op once uvicorn has
   installed its own root handlers, so routing structlog through the stdlib
   root logger silently drops our lines. We instead write straight to stdout
   with `PrintLoggerFactory`, independent of whatever uvicorn did to stdlib.

2. Surviving an OOM kill. stdout to a pipe (Railway captures a pipe, not a tty)
   is block-buffered, so on a SIGKILL the buffered lines are lost -- which is
   exactly why the logs looked "blank" right before a crash. `PrintLogger`
   flushes every line, so the last thing the process logged is on the wire
   before the kill lands. That is what makes an OOM diagnosable at all.
"""

from __future__ import annotations

import json
import logging
import resource
import sys
from collections import deque
from typing import Any

import structlog

from src.config.settings import get_settings

_configured = False

# In-memory ring of the most recent structured log lines, so /diagnostics can
# show "last 200 lines" without shipping a log aggregator or making the user
# open Railway. Two writers feed it: this process's own structlog (via the
# processor below) and the out-of-process pipeline child (its stdout lines are
# forwarded here by src/runner.py). Bounded, so it can never grow unbounded.
_LOG_RING: deque[dict[str, Any]] = deque(maxlen=400)


def _coerce(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _ring_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: copy each record into the ring, then pass it on."""
    try:
        _LOG_RING.append(
            {
                "ts": event_dict.get("timestamp"),
                "level": str(event_dict.get("level", method_name)),
                "event": _coerce(event_dict.get("event")),
                "fields": {
                    k: _coerce(v)
                    for k, v in event_dict.items()
                    if k not in ("timestamp", "level", "event")
                },
                "src": "api",
            }
        )
    except Exception:  # noqa: BLE001 - logging must never raise
        pass
    return event_dict


def record_log_line(raw: str, *, src: str = "run") -> None:
    """Push a log line from another process (the pipeline child) into the ring.

    The child renders JSON to stdout; the runner forwards each line here so the
    diagnostics page shows pipeline stage logs alongside the API's own. A line
    that is not JSON (dev console format) is kept as-is under `event`.
    """
    raw = raw.rstrip("\n")
    if not raw.strip():
        return
    entry: dict[str, Any] = {"ts": None, "level": "info", "event": raw, "fields": {}, "src": src}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            entry["ts"] = obj.get("timestamp")
            entry["level"] = str(obj.get("level", "info"))
            entry["event"] = _coerce(obj.get("event", raw))
            entry["fields"] = {
                k: _coerce(v) for k, v in obj.items()
                if k not in ("timestamp", "level", "event")
            }
    except (ValueError, TypeError):
        pass
    _LOG_RING.append(entry)


def get_recent_logs(level: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Most recent log entries, newest first, optionally filtered by min level."""
    order = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50}
    floor = order.get((level or "").lower(), 0)
    out = [e for e in reversed(_LOG_RING) if order.get(e["level"].lower(), 20) >= floor]
    return out[:limit]


def peak_rss_mb() -> float:
    """Process high-water-mark resident memory, in MB.

    `ru_maxrss` is kilobytes on Linux, bytes on macOS. It only ever rises, so
    logging it after each stage shows where allocation actually spikes -- the
    single most useful number for chasing an OOM.
    """
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return 0.0
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(ru / divisor, 1)


def configure_logging(json_output: bool | None = None) -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    use_json = (s.env == "prod") if json_output is None else json_output
    level = getattr(logging, s.log_level.upper(), logging.INFO)

    # Line-buffer stdout so records leave the process promptly even when it is a
    # pipe (Railway), not a tty. Belt-and-braces alongside PrintLogger's flush.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001 - not all streams support it
        pass

    # Keep stdlib logging working for third-party libraries. `force=True`
    # replaces any handlers uvicorn already installed on the root logger, so
    # their records render too instead of vanishing. Our own logs do NOT go
    # through here -- see the PrintLoggerFactory below.
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=level, force=True
    )
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _ring_processor,  # capture into the /diagnostics ring before rendering
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Write directly to stdout, flushing each line. Independent of the
        # stdlib root logger, so it renders under uvicorn and survives a kill.
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    _configured = True
