"""SQLAlchemy models.

Design notes that matter:

* `universe` is snapshotted every single day. Backtests reconstruct the universe
  as it existed on that date, including names that have since died. Screening
  today's live tickers against 2023 prices produces a fantasy.
* `fundamentals` carries three dates -- `period_end`, `filing_date`, `ingested_at`.
  Reads go through src/storage/pit.py, which enforces `filing_date <= as_of`.
* `daily_scores` exists so the IC tracker can correlate scores against forward
  returns once those returns exist.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---------------------------------------------------------------------------
# Stage 0
# ---------------------------------------------------------------------------
class UniverseSnapshot(Base):
    """One row per (date, ticker). The survivorship-bias defence."""

    __tablename__ = "universe"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(256))
    exchange: Mapped[str | None] = mapped_column(String(16))
    security_type: Mapped[str | None] = mapped_column(String(16))
    sector: Mapped[str | None] = mapped_column(String(64), index=True)
    # Where `sector` came from: fmp | sic | unknown. Lets IC analysis separate
    # the clean vendor sectors from the approximate SIC-derived ones.
    sector_source: Mapped[str | None] = mapped_column(String(8))
    industry: Mapped[str | None] = mapped_column(String(128))
    cik: Mapped[str | None] = mapped_column(String(16))
    market_cap: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    adv_20d: Mapped[float | None] = mapped_column(Float)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("as_of_date", "ticker", name="uq_universe_date_ticker"),
    )


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------
class DailyBar(Base):
    __tablename__ = "daily_bars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    transactions: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("ticker", "date", name="uq_bars_ticker_date"),
        Index("ix_bars_date_ticker", "date", "ticker"),
    )


# ---------------------------------------------------------------------------
# Point-in-time fundamentals
# ---------------------------------------------------------------------------
class Fundamental(Base):
    """One row per (ticker, period_end, metric, source).

    `filing_date` is the SEC `filed` field -- the date the number actually became
    public. Never the fiscal period end. A quarter ending 2025-09-30 may not be
    filed until 2025-11-08; using it on 2025-10-01 is lookahead bias.

    `restated` marks vendor-restated figures. SEC as-reported is the source of
    truth for backtests; FMP/Yahoo serve restated numbers.
    """

    __tablename__ = "fundamentals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    period_end: Mapped[dt.date] = mapped_column(Date, nullable=False)
    fiscal_period: Mapped[str | None] = mapped_column(String(8))  # Q1..Q4, FY
    filing_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="sec")
    restated: Mapped[bool] = mapped_column(Boolean, default=False)
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "ticker",
            "metric",
            "period_end",
            "source",
            "filing_date",
            name="uq_fundamental_point",
        ),
        Index("ix_fund_ticker_filing", "ticker", "filing_date"),
    )


class EstimateSnapshot(Base):
    """Consensus estimates as captured on a given day.

    Revisions are computed by differencing snapshots, so `captured_on` is the
    point-in-time key -- there is no filing date for a consensus number.
    """

    __tablename__ = "estimates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    captured_on: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    horizon: Mapped[str] = mapped_column(String(16), default="fy1")
    eps_consensus: Mapped[float | None] = mapped_column(Float)
    revenue_consensus: Mapped[float | None] = mapped_column(Float)
    n_analysts: Mapped[int | None] = mapped_column(Integer)
    reco_strong_buy: Mapped[int | None] = mapped_column(Integer)
    reco_buy: Mapped[int | None] = mapped_column(Integer)
    reco_hold: Mapped[int | None] = mapped_column(Integer)
    reco_sell: Mapped[int | None] = mapped_column(Integer)
    reco_strong_sell: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "ticker", "captured_on", "horizon", name="uq_estimate_point"
        ),
    )


class EarningsEvent(Base):
    __tablename__ = "earnings_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    report_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[dt.date | None] = mapped_column(Date)
    actual_eps: Mapped[float | None] = mapped_column(Float)
    consensus_eps: Mapped[float | None] = mapped_column(Float)
    surprise_pct: Mapped[float | None] = mapped_column(Float)
    gap_pct: Mapped[float | None] = mapped_column(Float)
    is_future: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("ticker", "report_date", name="uq_earnings_point"),
    )


# ---------------------------------------------------------------------------
# Stage 3 raw inputs
# ---------------------------------------------------------------------------
class FilingEvent(Base):
    __tablename__ = "filing_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    cik: Mapped[str | None] = mapped_column(String(16))
    form: Mapped[str] = mapped_column(String(24), nullable=False)
    items: Mapped[str | None] = mapped_column(String(256))  # 8-K item codes
    filing_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    accession: Mapped[str] = mapped_column(String(32), nullable=False)
    primary_doc: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("accession", "ticker", name="uq_filing_accession"),
    )


class InsiderTransaction(Base):
    __tablename__ = "insider_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    person: Mapped[str | None] = mapped_column(String(256))
    role: Mapped[str | None] = mapped_column(String(64))
    transaction_code: Mapped[str | None] = mapped_column(String(4))  # P, S, A, M...
    shares: Mapped[float | None] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    value_usd: Mapped[float | None] = mapped_column(Float)
    transaction_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    filed_date: Mapped[dt.date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(16), default="sec")

    __table_args__ = (
        Index("ix_insider_ticker_date", "ticker", "transaction_date"),
    )


class SectorMap(Base):
    """SIC -> GICS-bucket sector per ticker. Near-static; pulled once from SEC
    and cached so sector-neutral scoring works without a paid sector feed."""

    __tablename__ = "sector_map"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    cik: Mapped[str | None] = mapped_column(String(16))
    sic: Mapped[str | None] = mapped_column(String(8))
    sic_description: Mapped[str | None] = mapped_column(String(160))
    sector: Mapped[str | None] = mapped_column(String(40), index=True)
    # sic (mapped) | unknown (SIC present but unmappable). Never a guess.
    sector_source: Mapped[str | None] = mapped_column(String(8))
    ingested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class NewsAggregate(Base):
    """GDELT-derived, per ticker per day. Never raw article text."""

    __tablename__ = "news_aggregates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    volume_z: Mapped[float | None] = mapped_column(Float)
    tone_avg: Mapped[float | None] = mapped_column(Float)
    tone_slope_7d: Mapped[float | None] = mapped_column(Float)
    source_diversity: Mapped[float | None] = mapped_column(Float)
    article_count: Mapped[int | None] = mapped_column(Integer)
    themes: Mapped[list[Any] | None] = mapped_column(JSON)
    headlines: Mapped[list[Any] | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("ticker", "as_of_date", name="uq_news_point"),
    )


class MacroSnapshot(Base):
    __tablename__ = "macro_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[dt.date] = mapped_column(
        Date, nullable=False, unique=True, index=True
    )
    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    regime_score: Mapped[float | None] = mapped_column(Float)
    series: Mapped[dict[str, Any] | None] = mapped_column(JSON)


# ---------------------------------------------------------------------------
# Pipeline outputs
# ---------------------------------------------------------------------------
class StageResult(Base):
    """Checkpoint. If Stage 4 dies we resume from the Stage 3 payload."""

    __tablename__ = "stage_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    stage: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    exit_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    rejected: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "stage", name="uq_stage_checkpoint"),
    )


class DailyScore(Base):
    """The IC tracker's input. Forward returns are backfilled once they exist."""

    __tablename__ = "daily_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    sector: Mapped[str | None] = mapped_column(String(64))
    sector_source: Mapped[str | None] = mapped_column(String(8))  # fmp|sic|unknown
    stage_reached: Mapped[int] = mapped_column(Integer, default=1)
    # Which factor set produced factor_composite: "full" (all 19) or
    # "momentum_only" (3 price factors). IC evaluation MUST filter on this --
    # a momentum-only score is not comparable to a full-composite score, and
    # pooling them would measure neither.
    mode: Mapped[str] = mapped_column(String(16), default="full", index=True)
    factor_composite: Mapped[float | None] = mapped_column(Float)
    catalyst_score: Mapped[float | None] = mapped_column(Float)
    llm_triage_score: Mapped[float | None] = mapped_column(Float)
    llm_total_score: Mapped[float | None] = mapped_column(Float)
    final_rank: Mapped[int | None] = mapped_column(Integer)
    factor_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    data_completeness: Mapped[float | None] = mapped_column(Float)
    fwd_ret_1d: Mapped[float | None] = mapped_column(Float)
    fwd_ret_5d: Mapped[float | None] = mapped_column(Float)
    fwd_ret_21d: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("as_of_date", "ticker", name="uq_score_point"),
    )


class Thesis(Base):
    __tablename__ = "theses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    total_score: Mapped[int] = mapped_column(Integer, default=0)
    subscores: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    thesis: Mapped[str | None] = mapped_column(Text)
    bull_case: Mapped[str | None] = mapped_column(Text)
    bear_case: Mapped[str | None] = mapped_column(Text)
    invalidation: Mapped[str | None] = mapped_column(Text)
    time_horizon_days: Mapped[int | None] = mapped_column(Integer)
    conviction: Mapped[str | None] = mapped_column(String(8))
    key_risks: Mapped[list[Any] | None] = mapped_column(JSON)
    catalysts_ahead: Mapped[list[Any] | None] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("as_of_date", "ticker", name="uq_thesis_point"),
    )


class RunLog(Base):
    __tablename__ = "run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")
    # "full" | "momentum_only" -- see DailyScore.mode.
    mode: Mapped[str] = mapped_column(String(16), default="full")
    regime: Mapped[str | None] = mapped_column(String(16))
    funnel_counts: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    api_calls: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    report_path: Mapped[str | None] = mapped_column(String(512))
    error: Mapped[str | None] = mapped_column(Text)


class ReportArtifact(Base):
    """The rendered report, stored in the DB rather than only on disk.

    On Railway the worker (which renders) and the api (which serves) are
    separate services with separate, ephemeral filesystems. A file on the
    worker's disk is unreachable by the api and gone on the next redeploy.
    Persisting the bytes here makes the report durable and cross-service; the
    disk copy remains a convenience for local runs.
    """

    __tablename__ = "report_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[dt.date] = mapped_column(
        Date, nullable=False, unique=True, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(64))
    html: Mapped[str | None] = mapped_column(Text)
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary)
    html_bytes: Mapped[int] = mapped_column(Integer, default=0)
    pdf_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class CacheEntry(Base):
    """Cross-process HTTP response cache with a TTL.

    Redis is optional here; without it the in-process cache is lost every time
    the pipeline runs (it runs in a fresh subprocess), so re-running the same day
    re-hits SEC/GDELT for every ticker. This table persists those responses so a
    same-day re-run is served from Postgres, not the network. Keyed by the same
    hash the API client uses; `expires_at` is naive UTC.
    """

    __tablename__ = "cache_entries"

    cache_key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


class PageView(Base):
    """One row per reader-facing page request. No sampling, no rollups.

    Stored per event rather than as counters because a counter cannot be
    re-cut: "how many people looked at JPM last week" is unanswerable once the
    only thing kept is a running total. Rows are cheap and this is a personal
    site.

    `ip_hash` is a salted digest, never the address. It is enough to tell two
    requests apart, and deliberately not enough to be a log of who read what.
    It is also NOT a person: one office shares an address, and one phone
    switching from wifi to cellular produces two. Anything derived from it is
    labelled as a count of addresses, not of people.

    `is_bot` is recorded rather than dropped. A crawler is real traffic and
    silently discarding it makes a number nobody can reconcile against the
    server log.
    """

    __tablename__ = "page_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False, index=True
    )
    path: Mapped[str] = mapped_column(String(128), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16))
    ip_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    referrer: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[int] = mapped_column(Integer, default=200)

    __table_args__ = (
        Index("ix_pageview_time_bot", "created_at", "is_bot"),
    )


class LlmUsage(Base):
    """One row per question asked on a company page: tokens, cost, outcome.

    Persisted rather than counted in memory because the daily ceiling is a
    SPEND cap on a public endpoint with a private key behind it. An in-process
    counter resets on every container restart, so on a platform that restarts
    freely the "hard stop" would be a hard stop per restart -- which is not a
    cap at all. Reading the day's spend from here survives that.

    `ip_hash` is a salted digest, never the address: enough to rate-limit one
    caller, not enough to be a record of who read what. Failed calls are stored
    too -- an upstream error still costs an attempt, and a rate limiter that
    only counts successes can be spun by making requests that fail.
    """

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False, index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    ip_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(96), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (Index("ix_llm_usage_ip_time", "ip_hash", "created_at"),)


# ---------------------------------------------------------------------------
# Accounts: who may call the API, and what they have paid for
# ---------------------------------------------------------------------------


def _new_id() -> str:
    """A fresh row id. `str(uuid4())` rather than a native UUID column because
    the same schema has to create itself on SQLite (tests, local runs) and on
    Postgres (Railway) from one `create_all`, and a 36-char string behaves
    identically on both."""
    return str(uuid.uuid4())


def current_month(now: dt.datetime | None = None) -> str:
    """The calendar month a call counts against, as "2026-09".

    UTC, not local time. The alternative -- the server's idea of "this month"
    -- moves the reset moment whenever the platform's timezone changes, which
    would silently hand somebody a second free allowance.
    """
    return (now or _utcnow()).strftime("%Y-%m")


class ApiUser(Base):
    """One row per API key. The whole access-control state of one caller.

    Access is granted by hand: there is no payment processor in this service.
    Money arrives out of band (e-transfer, PayPal) and `POST /admin/grant-access`
    flips the two fields below. That is the entire billing system, and keeping
    it two plain columns is what makes it safe to operate from a phone.

    `api_key` is stored in the clear rather than hashed. It is a read-only key
    over data that is already public (SEC filings), the worst case of a leak is
    somebody else's free quota being spent, and a recoverable key means a lost
    key is a lookup rather than a re-registration. That trade would be wrong for
    a password and is right for this.
    """

    __tablename__ = "api_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    api_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # "free" | "pro". A plain string, not a SQLAlchemy Enum: a native Postgres
    # enum type cannot be widened by the additive ALTER that `init_db` runs, so
    # adding a third tier later would need a real migration to add a word.
    subscription_tier: Mapped[str] = mapped_column(
        String(8), nullable=False, default="free"
    )
    has_paid_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class UsageLog(Base):
    """One row per metered API call. The meter itself, not a summary of it.

    Rows rather than a counter on `api_users`, for the reason the rest of this
    schema keeps events: a counter cannot answer "which endpoint did they
    actually use" or be recut after the fact, and it cannot be audited when a
    caller disputes their usage. `month` is denormalised out of `called_at` so
    the limit check is an indexed equality test instead of a date range scan.
    """

    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    endpoint_called: Mapped[str] = mapped_column(String(128), nullable=False)
    called_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["api_users.id"], ondelete="CASCADE"),
        Index("ix_usage_user_month", "user_id", "month"),
    )


class AdminAction(Base):
    """Every grant and revoke, kept. The record of who was given what, when.

    Persisted rather than only logged because this is the paper trail behind
    real money: somebody paid, and the only evidence that access was granted is
    this row. Container logs on this platform are ephemeral and rotate away,
    which makes them the wrong home for the one record a payment dispute would
    turn on. Failed attempts are stored too -- a wrong admin secret against this
    endpoint is the single thing worth noticing here.
    """

    __tablename__ = "admin_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Salted digest of the caller's address, never the address -- enough to see
    # that fifty failed attempts came from one place, not a log of who called.
    actor_ip_hash: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(String(300))


def current_day(now: dt.datetime | None = None) -> str:
    """The UTC day a demo call counts against, as "2026-09-06". UTC for the same
    reason `current_month` is: a server whose timezone moves would hand somebody
    a second allowance."""
    return (now or _utcnow()).strftime("%Y-%m-%d")


class DemoUsage(Base):
    """One row per anonymous demo call, keyed by address digest and UTC day.

    A table rather than a dict in process memory, for the reason `llm_usage`
    gives: this platform restarts containers freely, and an in-memory limiter
    resets to zero on every restart -- so "5 a day" would mean "5 per deploy",
    which is not a limit. The demo runs on a real key with a real monthly
    ceiling behind it, so the per-address gate has to survive a bounce.

    `ip_hash` is the same salted digest used for page views: enough to tell two
    callers apart for a day, not a record of who looked at what.
    """

    __tablename__ = "demo_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    ip_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    ticker: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_demo_ip_day", "ip_hash", "day"),)


class MagicLink(Base):
    """One row per login link issued. Single use, short lived.

    The token is stored as issued rather than hashed, matching how `api_users`
    already holds its keys: hashing here would be a lock on a door standing
    beside an open one, and the window is fifteen minutes and one use wide
    either way.

    `used` is a column rather than a delete so a second click on the same link
    can be told apart from a link that never existed -- the first deserves
    "already used, here is a fresh one", the second does not.
    """

    __tablename__ = "magic_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )


ALL_TABLES = [
    UniverseSnapshot,
    DailyBar,
    Fundamental,
    EstimateSnapshot,
    EarningsEvent,
    FilingEvent,
    InsiderTransaction,
    NewsAggregate,
    MacroSnapshot,
    StageResult,
    DailyScore,
    Thesis,
    RunLog,
    ReportArtifact,
    CacheEntry,
    LlmUsage,
    PageView,
    ApiUser,
    UsageLog,
    AdminAction,
    DemoUsage,
    MagicLink,
]

__all__ = [c.__name__ for c in ALL_TABLES] + ["Base", "ALL_TABLES"]

# Keep the import referenced so linters don't strip it; used by migrations.
_ = ForeignKeyConstraint


class JobState(Base):
    """One row per scheduled data job. The auto-updater's memory.

    Persisted rather than kept in process memory for the same reason
    `llm_usage` is: this runs on a platform that restarts containers freely,
    and an in-memory "last run at" resets to nothing on every restart. That
    turns a quarterly job into a job that re-downloads the SEC datasets on
    every deploy, and turns a failure backoff into no backoff at all -- a
    crash-looping container would hammer sec.gov once per boot.

    `next_earliest_at` is the gate the loop actually reads: a single naive-UTC
    timestamp meaning "do not attempt this job before then". Every reason to
    wait -- a fresh success, an exponential backoff after a failure, a quarter
    SEC has not published yet -- collapses into that one field, so the loop has
    exactly one thing to check and /admin has exactly one thing to show.
    """

    __tablename__ = "job_state"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    next_earliest_at: Mapped[dt.datetime | None] = mapped_column(DateTime, index=True)
    last_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(300))
    # Plain English, shown on /admin: "loaded 2026q2", "2026q2 not published yet".
    last_detail: Mapped[str | None] = mapped_column(String(200))
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # For the bulk-dataset jobs: the newest quarter whose ZIP actually loaded,
    # as "2026q2". It cannot be inferred from the fundamentals table any more,
    # because the frames sweep fills the same rows for the same quarter from a
    # different endpoint -- so a table-only check would report the authoritative
    # quarterly load as already done and it would never run again.
    last_quarter_loaded: Mapped[str | None] = mapped_column(String(8))
