"""Redis cache with a graceful in-process fallback.

Redis is a hard requirement in production (rate limiting depends on it) but the
cache layer degrades to a local dict so that tests, backfills, and local runs
work without a server.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog

from src.config.settings import get_settings

log = structlog.get_logger(__name__)

_client: Any | None = None
_client_checked = False


class _LocalCache:
    """Tiny TTL dict. Not shared across processes -- fallback only."""

    def __init__(self) -> None:
        self._d: dict[str, tuple[float, str]] = {}

    def get(self, key: str) -> str | None:
        item = self._d.get(key)
        if item is None:
            return None
        expires, value = item
        if expires and expires < time.time():
            self._d.pop(key, None)
            return None
        return value

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._d[key] = (time.time() + ttl if ttl else 0.0, value)

    def set(self, key: str, value: str) -> None:
        self._d[key] = (0.0, value)

    def delete(self, *keys: str) -> None:
        for k in keys:
            self._d.pop(k, None)

    def ping(self) -> bool:
        return True

    def eval(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


_LOCAL = _LocalCache()


def get_redis() -> Any:
    """Return a live Redis client, or the local in-process fallback.

    Redis is optional. Set REDIS_URL empty (or omit the plugin) to run
    Postgres-only -- with a single service process the in-process cache and
    rate-limiter are equivalent, so this is a clean, supported mode rather than a
    degraded one.
    """
    global _client, _client_checked
    if _client is not None:
        return _client
    if _client_checked:
        return _LOCAL
    _client_checked = True

    url = get_settings().redis_url
    if not url:
        # Explicitly disabled -- go straight to the local cache, no connect
        # attempt, no warning.
        log.info("redis_disabled_using_local_cache")
        return _LOCAL
    try:
        import redis  # imported lazily so the package stays optional in tests

        client = redis.Redis.from_url(
            url, decode_responses=True, socket_timeout=2.0
        )
        client.ping()
        _client = client
        return client
    except Exception as exc:  # pragma: no cover - environment dependent
        log.warning("redis_unavailable_using_local_cache", error=str(exc))
        return _LOCAL


def reset_redis_cache() -> None:
    """Test helper."""
    global _client, _client_checked
    _client = None
    _client_checked = False


def cache_get_json(key: str) -> Any | None:
    raw = get_redis().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def cache_set_json(key: str, value: Any, ttl: int) -> None:
    try:
        get_redis().setex(key, ttl, json.dumps(value, default=str))
    except Exception as exc:  # pragma: no cover
        log.warning("cache_write_failed", key=key, error=str(exc))
