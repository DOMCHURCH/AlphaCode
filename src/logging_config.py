"""Structured JSON logging via structlog."""

from __future__ import annotations

import logging
import sys

import structlog

from src.config.settings import get_settings

_configured = False


def configure_logging(json_output: bool | None = None) -> None:
    global _configured
    if _configured:
        return
    s = get_settings()
    use_json = (s.env == "prod") if json_output is None else json_output

    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=s.log_level.upper()
    )
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if use_json
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, s.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True
