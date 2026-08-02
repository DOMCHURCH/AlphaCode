"""Central settings. Everything comes from env vars; nothing is hardcoded."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------------- API keys ----------------
    polygon_api_key: str = Field(default="", alias="POLYGON_API_KEY")
    # Polygon plan. On the free tier grouped-daily is capped at 5 calls/min, so a
    # multi-day backfill (one call per session) 429s for hours -- the wide end
    # must come from a whole-market bulk source (Stooq) instead. Selection is by
    # capability, not key presence: "free" caps Polygon to `polygon_free_rate_per_min`
    # and forbids using it for backfill; "paid" allows the grouped-daily backfill
    # loop. Default "free" -- the safe assumption; a key alone does not imply paid.
    polygon_tier: Literal["free", "paid"] = Field(
        default="free", alias="POLYGON_TIER"
    )
    polygon_free_rate_per_min: int = Field(
        default=5, alias="POLYGON_FREE_RATE_PER_MIN"
    )
    fmp_api_key: str = Field(default="", alias="FMP_API_KEY")
    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
    fred_api_key: str = Field(default="", alias="FRED_API_KEY")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    sec_user_agent: str = Field(
        default="AlphaFunnel Research contact@example.com", alias="SEC_USER_AGENT"
    )

    # ---------------- Infrastructure ----------------
    database_url: str = Field(
        default="sqlite:///./alphafunnel.db", alias="DATABASE_URL"
    )
    # Optional. Empty = use the in-process cache/rate-limiter (fine for a single
    # service). Set it (or a ${{Redis.REDIS_URL}} reference) only if you run
    # multiple processes that must share rate-limit budgets.
    redis_url: str = Field(default="", alias="REDIS_URL")

    # ---------------- LLM ----------------
    llm_triage_model: str = Field(
        default="deepseek/deepseek-chat", alias="LLM_TRIAGE_MODEL"
    )
    llm_deep_model: str = Field(
        default="deepseek/deepseek-chat", alias="LLM_DEEP_MODEL"
    )
    llm_temperature: float = Field(default=0.2, alias="LLM_TEMPERATURE")
    llm_verify_models_on_startup: bool = Field(
        default=True, alias="LLM_VERIFY_MODELS_ON_STARTUP"
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )

    # ---------------- Funnel widths ----------------
    stage1_target: int = Field(default=1200, alias="STAGE1_TARGET")
    # Stage 2 output = Stage 3 input. Stage 3 is the only expensive stage (per-
    # ticker SEC/GDELT/options), so 200 halves it; the ~400 band was always
    # approximate and Stage 3 still narrows to ~100.
    stage2_take: int = Field(default=200, alias="STAGE2_TAKE")
    stage3_take: int = Field(default=100, alias="STAGE3_TAKE")
    stage4_take: int = Field(default=25, alias="STAGE4_TAKE")
    stage5_take: int = Field(default=10, alias="STAGE5_TAKE")
    max_per_sector: int = Field(default=3, alias="MAX_PER_SECTOR")

    # ---------------- Universe filters ----------------
    min_price: float = Field(default=3.0, alias="MIN_PRICE")
    min_dollar_volume: float = Field(default=2_000_000.0, alias="MIN_DOLLAR_VOLUME")
    min_market_cap: float = Field(default=150_000_000.0, alias="MIN_MARKET_CAP")
    min_universe_size: int = Field(default=4000, alias="MIN_UNIVERSE_SIZE")

    # Trading days of history required before a run is allowed. Stage 1 evaluates
    # a 52-week high, a 12-month return and a 200-day SMA (and its 21-day slope),
    # so a run on less than a year is degraded, not just "smaller". Below this the
    # run is BLOCKED and the shortfall surfaced -- never run degraded to succeed.
    min_history_days: int = Field(default=252, alias="MIN_HISTORY_DAYS")

    # Stage 2 aborts if mean per-name factor completeness falls below this. A
    # composite scored on mostly-NaN factors (e.g. no estimate revisions without a
    # FINNHUB key, or fundamentals that didn't join) is not a defensible ranking --
    # better to fail loudly and fix the data than ship a score built on ~16%.
    min_mean_completeness: float = Field(
        default=0.4, alias="MIN_MEAN_COMPLETENESS"
    )

    # Free-data mode (no POLYGON_API_KEY): bars come from the Stooq bulk archive
    # (one download, whole market). Cap how many tickers to keep (0 = all; the
    # price/ADV filters trim illiquid ones anyway). Lower it to make the first
    # free backfill faster.
    free_universe_max: int = Field(default=0, alias="FREE_UNIVERSE_MAX")

    # Keyless wide-end source: the Stooq bulk daily archive. Override if Stooq
    # changes the path.
    stooq_bulk_url: str = Field(
        default="https://stooq.com/db/h/d_us_txt.zip", alias="STOOQ_BULK_URL"
    )

    # Rate limits for the open, unauthenticated POST endpoints. /run spends LLM
    # credits, so cap how often it (and /backfill) can be triggered per hour to
    # stop anyone with the URL from burning the OpenRouter key. Generous enough
    # for a human; low enough to defeat a script. 0 disables the limit.
    run_rate_per_hour: int = Field(default=20, alias="RUN_RATE_PER_HOUR")
    backfill_rate_per_hour: int = Field(default=8, alias="BACKFILL_RATE_PER_HOUR")
    # /reconcile makes a handful of network calls (SEC + Yahoo per sampled name),
    # so cap it low -- it's a diagnostic, not a hot path.
    reconcile_rate_per_hour: int = Field(default=6, alias="RECONCILE_RATE_PER_HOUR")

    # Hard per-chunk timeout (seconds) for the Yahoo/yfinance batch download.
    # yfinance does a blocking socket read with no timeout of its own; a stalled
    # Yahoo connection would otherwise hang the whole backfill (and hold the
    # backfill lock) forever. A timed-out chunk is logged and skipped.
    yahoo_download_timeout: float = Field(
        default=120.0, alias="YAHOO_DOWNLOAD_TIMEOUT"
    )
    # Hard per-call timeout (seconds) for a Polygon grouped-daily request. Bounds
    # each day of the backfill loop so a single hung call can't freeze the whole
    # load; the day is logged, recorded in the backfill diagnostics, and skipped.
    polygon_fetch_timeout: float = Field(
        default=60.0, alias="POLYGON_FETCH_TIMEOUT"
    )

    # ---------------- Operational ----------------
    max_run_cost_usd: float = Field(default=2.0, alias="MAX_RUN_COST_USD")
    run_timezone: str = Field(default="America/New_York", alias="RUN_TIMEZONE")
    run_hour: int = Field(default=6, alias="RUN_HOUR")
    run_minute: int = Field(default=0, alias="RUN_MINUTE")

    # Single-service mode: run the daily scheduler inside the api process, so one
    # `uvicorn src.api:app` does both serving and the 06:00 run. Off by default so
    # local dev doesn't spawn a cron; set ENABLE_SCHEDULER=true on the one Railway
    # service.
    enable_scheduler: bool = Field(default=False, alias="ENABLE_SCHEDULER")
    report_dir: str = Field(default="./reports", alias="REPORT_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    env: Literal["dev", "prod"] = Field(default="dev", alias="ENV")

    # Deprecated / unused: the /run and /backfill endpoints are open by design so
    # the one-button site needs no token. Kept only so an existing API_KEY env var
    # doesn't fail settings validation. Safe to leave unset.
    api_key: str = Field(default="", alias="API_KEY")

    # Business-day buffer added on top of filing_date to model ingestion lag.
    pit_lag_business_days: int = Field(default=2, alias="PIT_LAG_BUSINESS_DAYS")

    # Concurrency
    http_concurrency: int = Field(default=10, alias="HTTP_CONCURRENCY")

    @field_validator("database_url")
    @classmethod
    def _normalise_pg_scheme(cls, v: str) -> str:
        # Railway hands out postgres://; SQLAlchemy 2 wants postgresql://
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql://", 1)
        return v

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
