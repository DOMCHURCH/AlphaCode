"""Redis-backed token bucket. One bucket per data source.

Atomic via a Lua script so multiple worker processes share one budget. Falls
back to a process-local bucket when Redis is unavailable (dev/test), which is
correct-enough for a single process.

Nothing in this codebase calls time.sleep() to pace requests. Everything goes
through `acquire()`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from src.config.rate_limits import BUCKETS, Bucket
from src.storage.cache import get_redis

log = structlog.get_logger(__name__)

# Returns the number of seconds the caller must wait (0 = token granted).
_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = burst
  ts = now
end

local delta = math.max(0, now - ts)
tokens = math.min(burst, tokens + delta * rate)

local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  wait = (cost - tokens) / rate
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)
return tostring(wait)
"""


class RateLimiter:
    """Shared token bucket across all ingest modules."""

    def __init__(self, prefix: str = "rl") -> None:
        self.prefix = prefix
        self._script: Any | None = None
        self._local: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()
        self._calls: dict[str, int] = {}

    # -- registration -------------------------------------------------
    def _bucket(self, source: str) -> Bucket:
        b = BUCKETS.get(source)
        if b is None:
            raise KeyError(
                f"No rate-limit bucket configured for source {source!r}. "
                f"Add it to src/config/rate_limits.py."
            )
        return b

    def call_counts(self) -> dict[str, int]:
        return dict(self._calls)

    def reset_counts(self) -> None:
        self._calls.clear()

    # -- acquisition --------------------------------------------------
    async def acquire(self, source: str, cost: float = 1.0) -> None:
        """Block (async) until a token is available for `source`."""
        bucket = self._bucket(source)
        deadline = time.monotonic() + bucket.max_wait_s
        while True:
            wait = self._try_consume(bucket, cost)
            if wait <= 0:
                self._calls[source] = self._calls.get(source, 0) + 1
                return
            if time.monotonic() + wait > deadline:
                # Better to log and proceed than to stall a whole stage. The
                # source's own 429 handling (tenacity retry) is the backstop.
                log.warning(
                    "rate_limit_wait_exceeded",
                    source=source,
                    wait_s=round(wait, 2),
                    max_wait_s=bucket.max_wait_s,
                )
                self._calls[source] = self._calls.get(source, 0) + 1
                return
            await asyncio.sleep(min(wait, 1.0))

    def _try_consume(self, bucket: Bucket, cost: float) -> float:
        key = f"{self.prefix}:{bucket.name}"
        now = time.time()
        client = get_redis()
        try:
            if hasattr(client, "register_script"):
                if self._script is None:
                    self._script = client.register_script(_LUA)
                raw = self._script(
                    keys=[key], args=[bucket.rps, bucket.burst, now, cost]
                )
                return float(raw)
        except Exception as exc:  # pragma: no cover - redis optional
            log.debug("rate_limiter_redis_failed", error=str(exc))
        return self._try_consume_local(bucket, cost, now)

    def _try_consume_local(self, bucket: Bucket, cost: float, now: float) -> float:
        tokens, ts = self._local.get(bucket.name, (float(bucket.burst), now))
        tokens = min(bucket.burst, tokens + max(0.0, now - ts) * bucket.rps)
        if tokens >= cost:
            self._local[bucket.name] = (tokens - cost, now)
            return 0.0
        self._local[bucket.name] = (tokens, now)
        return (cost - tokens) / bucket.rps


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
