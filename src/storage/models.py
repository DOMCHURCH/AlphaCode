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
    # The name this filer reported under before the current one, and the date
    # that name stopped being in force. Both nullable and both from SEC's
    # `formerNames`.
    #
    # Kept HERE, beside `name`, because it is the same fact about the same
    # registrant and the company page already reads this row -- so showing it
    # costs no extra query. The alternative was a second table and a second
    # read on the hot path, to display one line.
    #
    # It matters because `name` is SEC's CURRENT name for a CIK while the
    # filings on the page are whatever was filed earlier: Equity Residential's
    # balance sheets render under "VIVMARK RESIDENTIAL", which is correct and
    # unrecognisable. SEC has the same problem and does not solve it --
    # `companyfacts` labels those identical facts with the new name too.
    former_name: Mapped[str | None] = mapped_column(String(256))
    former_name_until: Mapped[dt.date | None] = mapped_column(Date)
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


class CompanyPageExtras(Base):
    """Pre-rendered prose and lists for one company page. One row per ticker.

    WHY A TABLE AND NOT A QUERY. The three things this holds -- a written
    introduction, four sector peers, the last few filings -- each need reads
    the page does not otherwise do: a SECOND fundamentals period for the
    year-on-year line, a scan of same-sector tickers, a filings lookup. At
    6,169 pages that is thousands of queries an hour for text that changes when
    a filing lands, which is quarterly. Computing it per render would be paying
    a per-request price for a per-quarter fact.

    WHY NOT AN IN-PROCESS CACHE. The site runs more than one worker and
    restarts on every deploy, so a memory cache pays the cold cost per worker
    per deploy, and the cold cost here is the expensive part. A table is warm
    the moment it is written and is shared by every process.

    WHAT THE PAGE PAYS. One primary-key read. Not zero -- and the page already
    performs several reads to build its three views, so this is a small
    addition to an existing cost rather than a new one. It is deliberately a
    SINGLE row containing every extra, so the count cannot creep: adding
    another section to the page must not add another query.

    STALENESS IS EXPLICIT. `computed_at` and `source_period_end` say what this
    was built from. Nothing here is authoritative -- every figure is
    recoverable from `fundamentals` and `filing_events` -- so a row that is
    missing or out of date costs a section of the page, never a wrong number.
    `scripts/backfill_page_extras.py` rebuilds one ticker or all of them.
    """

    __tablename__ = "company_page_extras"

    ticker: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Rendered HTML fragments, escaped at build time.
    intro_html: Mapped[str | None] = mapped_column(Text)
    peers_html: Mapped[str | None] = mapped_column(Text)
    filings_html: Mapped[str | None] = mapped_column(Text)
    # A complete <script type="application/ld+json"> block for this company.
    jsonld: Mapped[str | None] = mapped_column(Text)
    # The balance sheet this was written from, so a reader of the row can tell
    # whether it predates the newest filing without re-deriving it.
    source_period_end: Mapped[dt.date | None] = mapped_column(Date)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)


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

    Access is two plain fields, flipped by exactly one function
    (`accounts.apply_admin_action`). Two things call it: `POST
    /admin/grant-access`, which an operator runs by hand, and the Stripe
    webhook, which runs it when a payment settles. Keeping the card path and
    the manual path on one switch means a refund, a comp and a chargeback are
    all the same operation, and the state of an account can still be read and
    changed from a phone.

    `api_key` holds a SHA-256 HEX DIGEST, never the key itself. The key is
    shown once, when it is issued, and is unrecoverable afterwards -- there is
    no query that can produce it, here or from a database dump. An earlier
    version stored it in the clear on the argument that it only guards public
    SEC data; that argument covers the value of the data and not the cost of
    the leak, because the same row carries an email address and the key is what
    spends somebody's paid quota. Hashing costs one `sha256` per request and
    removes the whole class of problem.

    SHA-256 rather than bcrypt on purpose. This is a 256-bit random token, not
    a password: there is no dictionary to run against it, so the slow hash buys
    nothing and would put a ~100ms KDF on every single API call.
    """

    __tablename__ = "api_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    # The SHA-256 hex digest of the key: always 64 characters, whatever the
    # key's own length. The column stays at 128 rather than shrinking to 64 --
    # narrowing it would be a destructive ALTER on a live table to save eight
    # bytes a row, and `_widen_columns` only ever grows.
    api_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # The first 8 characters of the key, kept so the dashboard can show WHICH
    # key an account holds without being able to show the key. Nullable
    # because `_sync_added_columns` skips a NOT NULL column with no default,
    # which would have meant this silently never existing in production.
    api_key_prefix: Mapped[str | None] = mapped_column(String(16))
    # "free" | "pro". A plain string, not a SQLAlchemy Enum: a native Postgres
    # enum type cannot be widened by the additive ALTER that `init_db` runs, so
    # adding a third tier later would need a real migration to add a word.
    subscription_tier: Mapped[str] = mapped_column(
        String(8), nullable=False, default="free"
    )
    has_paid_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # When this account accepted the terms. Recorded rather than assumed: the
    # whole value of an acceptance checkbox is being able to say afterwards that
    # it was ticked, and a box enforced only in the browser can be skipped with
    # one curl.
    terms_accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # bcrypt hash, or NULL for an account that only ever uses magic links.
    # Nullable is the whole point: passwords are an ALTERNATIVE here, not a
    # requirement, and an account that never sets one must stay usable.
    password_hash: Mapped[str | None] = mapped_column(String(128))
    # When Pro lapses. NULL means "never" -- which covers both a free account
    # and a comped one, and is the only safe reading for the rows that existed
    # before this column did. Treating NULL as "already expired" would have
    # silently demoted every Pro customer on deploy.
    pro_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # Set when the operator has been warned this subscription is nearly up, and
    # cleared by every grant. Without it the daily job mails the same warning
    # every day for three days running.
    pro_reminder_sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # Stripe's side of the same account. Both nullable, because most accounts
    # never pay and a dataset buyer who checks out as a guest has a customer
    # but no subscription. They exist so a webhook that arrives with nothing
    # but a customer id -- which is all `customer.subscription.deleted` carries
    # about the person -- can still find the row it has to change.
    #
    # Deliberately unindexed. `_sync_added_columns` adds a COLUMN to a live
    # table and never an INDEX, so declaring one here would give a fresh
    # database an index the production one does not have -- and the lookup runs
    # at most once per webhook over a table of accounts, which is a scan
    # nobody will ever feel.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    # Which Stripe price this subscription is on: "monthly" | "annual" | NULL.
    #
    # NOT folded into `subscription_tier`. That column is the ACCESS LEVEL --
    # free or pro -- and a dozen call sites compare it to those two words. The
    # plan is a different fact about the same account (what they pay and how
    # often), it changes without the access level changing, and conflating the
    # two would mean every `== "pro"` test in the codebase had to learn about
    # billing cadence. `customer.subscription.updated` writes it so an upgrade
    # made in Stripe is visible here.
    pro_plan: Mapped[str | None] = mapped_column(String(16))
    # Consecutive failed renewal invoices. Reset to zero by any successful
    # payment, so this counts a RUN of failures rather than a lifetime total --
    # a customer who fails once, fixes their card and fails again a year later
    # is not two-thirds of the way to being cut off.
    #
    # Nullable with a default rather than NOT NULL, because `_sync_added_columns`
    # adds a column to a live table without a server default: every existing row
    # would hold NULL in a NOT NULL column and the ALTER would be rejected. Read
    # it as `int(row.payment_failure_count or 0)` and NULL means zero.
    payment_failure_count: Mapped[int | None] = mapped_column(Integer, default=0)
    # Set when the failure run reached its limit and access was withdrawn. A
    # separate field from `subscription_tier` on purpose: dropping to free is
    # what happens to the ALLOWANCE, and this is why it happened, which is the
    # part a support conversation needs and the tier alone cannot say.
    api_access_paused: Mapped[bool | None] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class StripeEvent(Base):
    """One row per Stripe event this service has already acted on.

    Stripe delivers at least once, not exactly once: a webhook whose reply is
    slow, lost, or a 500 is redelivered for three days. Without this table the
    second delivery of one `checkout.session.completed` adds a second 31-day
    Pro period for a single payment -- free access nobody paid for, arriving
    silently.

    The event id is the primary key, so the duplicate is refused by the
    database rather than by a query that raced. `type` is kept because the
    first question about a stuck webhook is which kind of event stopped
    arriving, and `received_at` because the second is when.
    """

    __tablename__ = "stripe_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, index=True
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

    The token is stored as issued rather than hashed. That used to be argued
    from `api_users`, which held its keys the same way -- a lock on a door
    standing beside an open one. `api_users` hashes now, so the argument has
    to stand on its own, and it does: this token is single use and dies
    fifteen minutes after it is written, so what a leaked table yields is a
    login link that has already expired or already been spent.

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
    # Whether the terms were accepted when this link was ASKED for. Carried on
    # the token because the account is created when the link is CLICKED, and
    # there is no checkbox at that moment -- the person is reading their email.
    # Requiring acceptance on the request instead would mean asking existing
    # users to re-consent on every sign-in.
    terms_accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
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



class SeededKey(Base):
    """A key issued by hand, to somebody who has no account here.

    For partner and influencer outreach: the recipient gets a working key in a
    message and never sees a signup form, a card field or an email
    confirmation. That is the whole feature, and it is why this table exists
    instead of a flag on `api_users` -- a row over there is a CUSTOMER, counted
    in the roster, counted in revenue estimates, and carrying columns
    (`stripe_customer_id`, `has_paid_download`, `pro_expires_at`) that mean
    nothing here. Mixing the two would make "how many customers are there" a
    question with two answers.

    NO FOREIGN KEY TO `api_users`, deliberately and permanently. These keys are
    decoupled: deleting the outreach programme must not touch a customer, and
    deleting a customer must not touch these. Nothing here joins.

    The consequence to know about is that `usage_logs.user_id` is a foreign key
    into `api_users`, so a seeded key cannot be metered there. Its meter is the
    two columns at the bottom of this table: a month and a count, rolled over
    when the month changes. A counter rather than rows, which is the opposite
    of the choice `usage_logs` makes and right for the opposite reason -- there
    are a handful of these keys, nobody is going to audit a partner's usage
    against an invoice, and the alternative was a foreign key this table must
    not have.
    """

    __tablename__ = "seeded_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    # The digest, exactly as `api_users.api_key` holds it: same generator, same
    # SHA-256, same prefix rule. A seeded key is indistinguishable from any
    # other key to whoever is holding it, and unrecoverable here.
    api_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    api_key_prefix: Mapped[str | None] = mapped_column(String(16))
    # Who it went to, in whatever words the operator used. UNIQUE because it is
    # the handle for revoking: `revoke_seed_key.py --label x` has to name one
    # key, and two keys sharing a label makes that command a coin toss.
    label: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # Issued from `scripts/issue_seed_key.py` or from the admin panel, which
    # call the same function. Both are behind ADMIN_SECRET and nothing else.
    # How the operator wants to READ that label: "Stefano Amorelli —
    # sec-edgar-mcp" against a label of "stefano-sec-edgar-mcp". The label
    # stays the handle -- unique, typed at a terminal, the thing revocation
    # takes -- and this is the same key said in words. Nullable because rows
    # issued before this column existed have none, and every reader falls back
    # to the label rather than rendering a blank.
    display_name: Mapped[str | None] = mapped_column(String(200))
    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow
    )
    # Calls per calendar month. Stands in for the tier allowance entirely --
    # these keys have no tier, because a tier is a thing you pay for.
    rate_limit_override: Mapped[int] = mapped_column(Integer, nullable=False)
    # Why this row exists, as a value rather than as tribal knowledge. One
    # value today; a second outreach programme gets its own rather than being
    # told apart by reading labels.
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="influencer_seed"
    )
    notes: Mapped[str | None] = mapped_column(String(500))
    # Set, never deleted. A revoked key stops authenticating immediately and
    # stays in the list, because "we gave this person a key and took it back"
    # is the thing worth being able to see later.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    # The meter. `usage_month` is the month `calls_this_month` counts, so a
    # rollover is a comparison rather than a scheduled job.
    usage_month: Mapped[str | None] = mapped_column(String(7))
    calls_this_month: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

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
