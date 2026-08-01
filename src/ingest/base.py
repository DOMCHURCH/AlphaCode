"""Shared async HTTP client: rate limiting, retries, caching, call accounting."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config.rate_limits import CACHE_TTL
from src.config.settings import get_settings
from src.ingest.rate_limiter import get_rate_limiter
from src.storage.cache import cache_get_json, cache_set_json

log = structlog.get_logger(__name__)


class TransientAPIError(RuntimeError):
    """Retryable: 429, 5xx, timeouts, connection resets."""


class PermanentAPIError(RuntimeError):
    """Not retryable: 4xx that will never succeed (bad key, bad symbol)."""


RETRYABLE = (TransientAPIError, httpx.TimeoutException, httpx.TransportError)


def cache_key(source: str, url: str, params: Mapping[str, Any] | None) -> str:
    blob = json.dumps(
        {"u": url, "p": dict(sorted((params or {}).items()))}, default=str
    )
    digest = hashlib.sha1(blob.encode()).hexdigest()[:20]
    return f"cache:{source}:{digest}"


class APIClient:
    """One instance per data source. Owns concurrency, retries and caching.

    Usage:
        async with APIClient("polygon", base_url="https://api.polygon.io") as c:
            data = await c.get_json("/v2/aggs/...", params={...})
    """

    def __init__(
        self,
        source: str,
        base_url: str = "",
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        concurrency: int | None = None,
        cache_ttl: int | None = None,
    ) -> None:
        self.source = source
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.cache_ttl = cache_ttl if cache_ttl is not None else CACHE_TTL.get(source, 0)
        limit = concurrency or get_settings().http_concurrency
        self._sem = asyncio.Semaphore(limit)
        self._client: httpx.AsyncClient | None = None
        self.calls_made = 0
        self.cache_hits = 0

    async def __aenter__(self) -> APIClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=self.timeout,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    async def get_json(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        expect_json: bool = True,
    ) -> Any:
        ttl = self.cache_ttl if cache_ttl is None else cache_ttl
        key = cache_key(self.source, url, params)
        if use_cache and ttl:
            hit = cache_get_json(key)
            if hit is not None:
                self.cache_hits += 1
                return hit

        data = await self._request_with_retry(url, params, expect_json)

        if use_cache and ttl and data is not None:
            cache_set_json(key, data, ttl)
        return data

    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        reraise=True,
    )
    async def _request_with_retry(
        self, url: str, params: Mapping[str, Any] | None, expect_json: bool
    ) -> Any:
        if self._client is None:
            raise RuntimeError("APIClient used outside an async context manager")
        await get_rate_limiter().acquire(self.source)
        async with self._sem:
            self.calls_made += 1
            try:
                resp = await self._client.get(url, params=dict(params or {}))
            except httpx.HTTPError as exc:
                raise TransientAPIError(str(exc)) from exc

        if resp.status_code == 429:
            raise TransientAPIError(f"{self.source} 429 rate limited")
        if resp.status_code >= 500:
            raise TransientAPIError(f"{self.source} {resp.status_code}")
        if resp.status_code == 403:
            # SEC returns 403 when the User-Agent header is missing or bad.
            raise PermanentAPIError(
                f"{self.source} 403 forbidden -- check API key / User-Agent"
            )
        if resp.status_code >= 400:
            raise PermanentAPIError(
                f"{self.source} {resp.status_code}: {resp.text[:200]}"
            )
        if not expect_json:
            return resp.text
        try:
            return resp.json()
        except ValueError as exc:
            raise PermanentAPIError(
                f"{self.source} returned non-JSON: {resp.text[:200]}"
            ) from exc

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """POST with the same rate limiting and retry behaviour. Never cached."""
        return await self._post_with_retry(url, payload, headers, timeout)

    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        reraise=True,
    )
    async def _post_with_retry(
        self,
        url: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None,
        timeout: float | None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError("APIClient used outside an async context manager")
        await get_rate_limiter().acquire(self.source)
        async with self._sem:
            self.calls_made += 1
            try:
                resp = await self._client.post(
                    url,
                    json=dict(payload),
                    headers=dict(headers or {}),
                    timeout=timeout or self.timeout,
                )
            except httpx.HTTPError as exc:
                raise TransientAPIError(str(exc)) from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientAPIError(f"{self.source} {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentAPIError(
                f"{self.source} {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()


async def gather_bounded(coros: list, limit: int = 10) -> list:
    """Run coroutines with a semaphore, returning results positionally.

    Exceptions are returned in place rather than cancelling the batch -- one bad
    ticker must never take down a stage.
    """
    sem = asyncio.Semaphore(limit)

    async def _run(c):
        async with sem:
            try:
                return await c
            except Exception as exc:  # noqa: BLE001 - deliberate per-item isolation
                log.warning("task_failed", error=str(exc), error_type=type(exc).__name__)
                return exc

    return await asyncio.gather(*[_run(c) for c in coros])
