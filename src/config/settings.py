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
    stage2_take: int = Field(default=400, alias="STAGE2_TAKE")
    stage3_take: int = Field(default=100, alias="STAGE3_TAKE")
    stage4_take: int = Field(default=25, alias="STAGE4_TAKE")
    stage5_take: int = Field(default=10, alias="STAGE5_TAKE")
    max_per_sector: int = Field(default=3, alias="MAX_PER_SECTOR")

    # ---------------- Universe filters ----------------
    min_price: float = Field(default=3.0, alias="MIN_PRICE")
    min_dollar_volume: float = Field(default=2_000_000.0, alias="MIN_DOLLAR_VOLUME")
    min_market_cap: float = Field(default=150_000_000.0, alias="MIN_MARKET_CAP")
    min_universe_size: int = Field(default=4000, alias="MIN_UNIVERSE_SIZE")

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

    # Optional guard for the money-spending POST /run endpoint. Empty = open
    # (local/dev default, no behaviour change). Set it in prod and callers must
    # send it as the X-API-Key header. Read endpoints stay open so report links
    # remain shareable.
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
