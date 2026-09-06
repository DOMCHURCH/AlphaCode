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
    llm_triage_timeout_s: int = Field(default=600, alias="LLM_TRIAGE_TIMEOUT_S")
    llm_deep_dive_timeout_s: int = Field(default=900, alias="LLM_DEEP_DIVE_TIMEOUT_S")
    llm_verify_models_on_startup: bool = Field(
        default=True, alias="LLM_VERIFY_MODELS_ON_STARTUP"
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )

    # ---------------- Ask box on /company/{ticker} ----------------
    # A stable, paid, low-cost model. NEVER a ":free" variant -- those are
    # delisted without notice and capped around 200 requests a day, so the
    # question box would break silently at an unpredictable moment. The string
    # is resolved against /api/v1/models at startup (src/llm/client.py), which
    # is also where the per-token price comes from, so cost is never a
    # hardcoded number that quietly goes stale.
    llm_ask_model: str = Field(default="deepseek/deepseek-chat", alias="LLM_ASK_MODEL")
    llm_ask_timeout_s: float = Field(default=25.0, alias="LLM_ASK_TIMEOUT_S")
    llm_ask_max_tokens: int = Field(default=220, alias="LLM_ASK_MAX_TOKENS")
    llm_ask_max_question_chars: int = Field(
        default=300, alias="LLM_ASK_MAX_QUESTION_CHARS"
    )
    # The endpoint is public and the API key sits behind it, so every cap is
    # enforced against the persisted usage table, not an in-process counter.
    llm_ask_per_ip_per_hour: int = Field(default=10, alias="LLM_ASK_PER_IP_PER_HOUR")
    llm_ask_per_day: int = Field(default=300, alias="LLM_ASK_PER_DAY")
    llm_ask_daily_cost_usd: float = Field(default=1.0, alias="LLM_ASK_DAILY_COST_USD")

    # ---------------- Funnel widths ----------------
    stage1_target: int = Field(default=1200, alias="STAGE1_TARGET")
    # Stage 2 output = Stage 3 input. Stage 3 is the only expensive stage (per-
    # ticker SEC/GDELT/options), so 200 halves it; the ~400 band was always
    # approximate and Stage 3 still narrows to ~100.
    stage2_take: int = Field(default=200, alias="STAGE2_TAKE")
    stage3_take: int = Field(default=100, alias="STAGE3_TAKE")
    stage3_timeout_s: int = Field(default=300, alias="STAGE3_TIMEOUT_S")
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

    # SEC Financial Statement Data Sets: one ZIP per quarter (num.txt + sub.txt)
    # replaces the per-CIK XBRL crawl. {year}/{q} are filled in. Override only if
    # SEC moves the path. `sec_dataset_quarters` = how many recent quarters to
    # load (8 = two years, enough for the trailing-quarter factors).
    sec_dataset_url: str = Field(
        default="https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip",
        alias="SEC_DATASET_URL",
    )
    sec_dataset_quarters: int = Field(default=8, alias="SEC_DATASET_QUARTERS")

    # Rate limits for the open, unauthenticated POST endpoints. /run spends LLM
    # credits, so cap how often it (and /backfill) can be triggered per hour to
    # stop anyone with the URL from burning the OpenRouter key. Generous enough
    # for a human; low enough to defeat a script. 0 disables the limit.
    run_rate_per_hour: int = Field(default=20, alias="RUN_RATE_PER_HOUR")
    backfill_rate_per_hour: int = Field(default=8, alias="BACKFILL_RATE_PER_HOUR")
    # /reconcile makes a handful of network calls (SEC + Yahoo per sampled name),
    # so cap it low -- it's a diagnostic, not a hot path.
    reconcile_rate_per_hour: int = Field(default=6, alias="RECONCILE_RATE_PER_HOUR")
    # /api/auth/register is open and hands out a free allowance, which makes the
    # per-key limit worth exactly as much as the cost of a new key. Without a cap
    # here, a script registering a hundred throwaway addresses has a thousand
    # free calls a month and the tier means nothing. Capped globally rather than
    # per-IP for the same reason the others are: one personal service, and the
    # thing being stopped is a loop, not a person signing up twice.
    register_rate_per_hour: int = Field(
        default=20, ge=0, alias="REGISTER_RATE_PER_HOUR"
    )

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

    # ---------------- Auto-update ----------------
    # The data keeps itself current. "auto" means: on in prod, off in dev, so a
    # deploy needs no extra variable to start updating and a local test run
    # never spawns a loop that talks to sec.gov. "on"/"off" force it either way.
    auto_update: Literal["auto", "on", "off"] = Field(
        default="auto", alias="AUTO_UPDATE"
    )
    # How often the loop wakes and asks "is anything stale?". This is NOT how
    # often work runs -- each job carries its own minimum interval and its own
    # backoff. A short tick just means a job that came due is picked up
    # promptly, including right after a deploy.
    auto_update_tick_minutes: int = Field(
        default=15, ge=1, alias="AUTO_UPDATE_TICK_MINUTES"
    )
    # Bars are due when the newest bar is older than the last completed trading
    # day. Market holidays mean "stale" can be permanently true for a day, so a
    # floor on retries stops the loop re-downloading the market every tick.
    auto_update_bars_min_hours: float = Field(
        default=6.0, alias="AUTO_UPDATE_BARS_MIN_HOURS"
    )
    # Quarters to reload on a SCHEDULED fundamentals/earnings run. Small on
    # purpose: the scheduled job exists to pick up the newest quarter (plus one
    # for late filers and restatements), not to rebuild history. A full rebuild
    # is still `POST /backfill?kind=fundamentals` or the reload button.
    auto_update_quarters: int = Field(default=2, ge=1, alias="AUTO_UPDATE_QUARTERS")
    # How many calendar quarters of XBRL frames to sweep for newly-filed
    # numbers. Two: the newest closed quarter is still filling up while the one
    # before it still gains late filers and amendments.
    sec_frames_quarters: int = Field(default=2, ge=1, alias="SEC_FRAMES_QUARTERS")
    # Floor between filing sweeps. This is the job that makes the site current
    # within a day of a company filing, so it runs several times a day rather
    # than quarterly -- but it is a whole-market sweep, so not every tick.
    auto_update_filings_min_hours: float = Field(
        default=6.0, alias="AUTO_UPDATE_FILINGS_MIN_HOURS"
    )
    # SEC publishes a quarter's dataset several weeks after the quarter ends,
    # on no announced date. When the quarter we want is not up yet, that is not
    # a failure -- we simply look again this often until it appears.
    auto_update_sec_recheck_hours: float = Field(
        default=12.0, alias="AUTO_UPDATE_SEC_RECHECK_HOURS"
    )
    # Failure backoff: 30 min, doubling, capped here. Applies to real errors
    # only (a network blip, a throttle), never to "not published yet".
    auto_update_backoff_max_hours: float = Field(
        default=12.0, alias="AUTO_UPDATE_BACKOFF_MAX_HOURS"
    )
    report_dir: str = Field(default="./reports", alias="REPORT_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    env: Literal["dev", "prod"] = Field(default="dev", alias="ENV")

    # Deprecated / unused: the /run and /backfill endpoints are open by design so
    # the one-button site needs no token. Kept only so an existing API_KEY env var
    # doesn't fail settings validation. Safe to leave unset.
    api_key: str = Field(default="", alias="API_KEY")

    # ---------------- Paid access (manual, no payment processor) ----------------
    # The shared secret for POST /admin/grant-access. UNSET MEANS THE ENDPOINT IS
    # DEAD, not open: an empty secret that compared equal to an empty header
    # would hand the grant switch to the whole internet the moment the variable
    # was forgotten on a redeploy. Failing closed makes that mistake obvious
    # (nothing can be granted) instead of silent (anything can).
    admin_secret: str = Field(default="", alias="ADMIN_SECRET")
    # Shown to users as the address to send payment notice to. Empty renders as
    # a plain "contact the site owner" rather than a broken mailto.
    admin_email: str = Field(default="", alias="ADMIN_EMAIL")
    # Calls per calendar month, by tier. Pro is a large ceiling rather than
    # literally unlimited: an unbounded key is an unbounded database bill if one
    # gets loose, and 10k/month is far past any honest use of this API.
    free_tier_monthly_calls: int = Field(
        default=10, ge=0, alias="FREE_TIER_MONTHLY_CALLS"
    )
    pro_tier_monthly_calls: int = Field(
        default=10_000, ge=0, alias="PRO_TIER_MONTHLY_CALLS"
    )
    # Prices, in whole dollars, quoted on the dashboard. Settings rather than
    # literals in the copy so the page and the payment instructions can never
    # drift apart.
    dataset_price_usd: int = Field(default=29, ge=0, alias="DATASET_PRICE_USD")
    pro_price_usd: int = Field(default=49, ge=0, alias="PRO_PRICE_USD")

    # ---------------- Outgoing mail (key recovery only) ----------------
    # AgentMail rather than a raw SMTP relay: one HTTP call, no relay
    # credentials to hold, and no STARTTLS negotiation to debug at 2am.
    # Unset AGENTMAIL_API_KEY disables sending entirely and the dashboard falls
    # back to "contact the owner" -- a recovery flow that silently drops the
    # mail is worse than one that admits it cannot send.
    agentmail_api_key: str = Field(default="", alias="AGENTMAIL_API_KEY")
    # Optional. Pins the mailbox to send from; with this unset the service
    # creates one under a stable client_id and reuses it across restarts.
    agentmail_inbox_id: str = Field(default="", alias="AGENTMAIL_INBOX_ID")
    # A registered address is a target: without a ceiling, "resend my key"
    # is a free email cannon pointed at whoever signed up.
    resend_rate_per_hour: int = Field(
        default=10, ge=0, alias="RESEND_RATE_PER_HOUR"
    )
    resend_cooldown_s: int = Field(default=600, ge=0, alias="RESEND_COOLDOWN_S")

    # ---------------- Public demo ----------------
    # The key the home page's live demo runs on, server-side. It is a REAL key
    # and is never sent to the browser (see src/demo.py); unset simply disables
    # the demo rather than breaking the page.
    demo_api_key: str = Field(default="", alias="DEMO_API_KEY")
    demo_calls_per_ip_per_day: int = Field(
        default=5, ge=0, alias="DEMO_CALLS_PER_IP_PER_DAY"
    )

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
