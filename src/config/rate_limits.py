"""Per-source rate limit configuration.

Every ingest module goes through the Redis token bucket using these numbers.
No time.sleep() anywhere in the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bucket:
    """Token bucket parameters for one data source."""

    name: str
    rps: float  # sustained refill rate, tokens/second
    burst: int  # bucket capacity
    max_wait_s: float = 30.0  # give up rather than block a stage forever


BUCKETS: dict[str, Bucket] = {
    # Paid tier. Generous, but grouped-daily is one call anyway.
    "polygon": Bucket("polygon", rps=50.0, burst=100),
    # Bulk endpoints only; we never loop per-ticker on FMP.
    "fmp": Bucket("fmp", rps=10.0, burst=20),
    # Free tier is 60 req/min. Stay under it.
    "finnhub": Bucket("finnhub", rps=0.95, burst=10),
    # SEC hard-caps at 10 req/sec and will block you for exceeding it.
    "sec": Bucket("sec", rps=8.0, burst=10),
    # Free but slow. Be polite.
    "gdelt": Bucket("gdelt", rps=2.0, burst=4, max_wait_s=60.0),
    "fred": Bucket("fred", rps=5.0, burst=10),
    "openrouter": Bucket("openrouter", rps=5.0, burst=10, max_wait_s=120.0),
    "yahoo": Bucket("yahoo", rps=1.0, burst=2),
}

# Redis cache TTLs in seconds.
CACHE_TTL: dict[str, int] = {
    "gdelt": 6 * 3600,
    "fred": 24 * 3600,
    "sec_submissions": 12 * 3600,
    "finnhub": 6 * 3600,
    "fmp_bulk": 12 * 3600,
    "polygon_options": 3600,
    "openrouter_models": 3600,
}
