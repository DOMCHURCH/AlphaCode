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
    # Paid tier. Generous, but grouped-daily is one call anyway. The free tier is
    # far tighter (5 calls/min) -- see bucket_for(), which overrides this at
    # runtime from POLYGON_TIER so the limiter actually throttles a free plan
    # instead of letting it 429.
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

def bucket_for(source: str) -> Bucket | None:
    """The rate-limit bucket for `source`, tier-adjusted where it matters.

    Polygon's bucket depends on the plan: a free key is 5 calls/min, not the
    paid 50/sec. Reading POLYGON_TIER here (rather than baking one number into
    BUCKETS) is what makes the limiter pace a free plan under its real limit
    instead of firing at paid speed and collecting 429s. Returns None for an
    unknown source so the caller can raise a descriptive error.
    """
    base = BUCKETS.get(source)
    if source == "polygon":
        # Imported lazily to avoid a settings <-> config import cycle at module load.
        from src.config.settings import get_settings

        s = get_settings()
        if s.polygon_tier == "free":
            per_min = max(1, s.polygon_free_rate_per_min)
            mw = base.max_wait_s if base else 30.0
            # burst == the per-minute allowance so a short flurry is allowed, then
            # refill paces at per_min/60 tokens/sec.
            return Bucket("polygon", rps=per_min / 60.0, burst=per_min, max_wait_s=mw)
    return base


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
