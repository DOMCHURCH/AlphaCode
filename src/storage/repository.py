"""Write-side helpers. Upserts, checkpoints, score persistence."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from src.storage.models import (
    Base,
    DailyBar,
    DailyScore,
    EarningsEvent,
    EstimateSnapshot,
    FilingEvent,
    Fundamental,
    InsiderTransaction,
    MacroSnapshot,
    NewsAggregate,
    ReportArtifact,
    RunLog,
    SectorMap,
    StageResult,
    Thesis,
    UniverseSnapshot,
)

log = structlog.get_logger(__name__)

# Write accounting. A stage that TRIES to persist a lot and writes nothing is a
# silent-failure bug -- e.g. a bad upsert raised per ticker and got swallowed by
# gather_bounded -- not an empty result. Every _upsert tallies attempted-vs-written
# per table here; the pipeline resets this per stage and aborts on a wholesale
# zero (assert_writes). Single event loop, so a plain dict is enough.
_write_ledger: dict[str, dict[str, int]] = {}


def reset_write_ledger() -> None:
    _write_ledger.clear()


def get_write_ledger() -> dict[str, dict[str, int]]:
    return {t: dict(s) for t, s in _write_ledger.items()}


def _record_write(table: str, attempted: int, written: int) -> None:
    s = _write_ledger.setdefault(table, {"attempted": 0, "written": 0})
    s["attempted"] += attempted
    s["written"] += written


def assert_writes(min_attempts: int = 100) -> list[str]:
    """Tables that attempted > `min_attempts` writes but wrote 0 -- a silent
    write failure the pipeline must abort on, distinct from an empty result."""
    return [
        t for t, s in _write_ledger.items()
        if s["attempted"] > min_attempts and s["written"] == 0
    ]


def dedupe_on(
    rows: Sequence[dict[str, Any]], key_cols: Sequence[str], *, label: str = ""
) -> tuple[list[dict[str, Any]], int]:
    """Collapse `rows` to one row per `key_cols`, keeping the FIRST occurrence.

    Postgres refuses an ON CONFLICT DO UPDATE that would touch the same row twice
    in one statement ("cannot affect row a second time"), so a batch carrying the
    same natural key twice aborts the whole insert. Callers order `rows` so that
    the record they want to survive comes first (see `save_fundamentals`, which
    sorts by filing_date ascending -- earliest, i.e. as-first-reported, wins).

    Returns (deduped_rows, n_dropped). Never merges values across duplicates: one
    record wins whole, so a surviving row is always a real record as filed.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        k = tuple(r.get(c) for c in key_cols)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    dropped = len(rows) - len(out)
    if dropped:
        log.info(
            "upsert_deduped",
            table=label or "rows",
            key=list(key_cols),
            kept=len(out),
            dropped=dropped,
            # A large fraction means the key is wrong, not that SEC restates a lot.
            dropped_pct=round(100 * dropped / max(1, len(rows)), 1),
        )
    return out, dropped


def _upsert(
    session: Session,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    chunk: int = 2000,
) -> int:
    """Dialect-aware ON CONFLICT DO UPDATE. Works on Postgres and SQLite.

    Returns the number of rows actually written (executed), and records
    attempted-vs-written in the ledger even when a batch raises -- so a swallowed
    failure still shows up as attempted>0, written=0.

    Rows are always deduped on `conflict_cols` first. That is a structural
    guarantee, not an optimisation: without it any caller passing the same natural
    key twice takes down the whole statement with a CardinalityViolation.
    """
    if not rows:
        return 0
    rows, _ = dedupe_on(rows, conflict_cols, label=model.__tablename__)
    if not rows:
        return 0
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    ins = pg_insert if dialect == "postgresql" else sqlite_insert
    attempted = len(rows)
    written = 0
    try:
        for i in range(0, len(rows), chunk):
            batch = rows[i : i + chunk]
            stmt = ins(model).values(batch)
            cols = update_cols or [
                c for c in batch[0].keys() if c not in conflict_cols and c != "id"
            ]
            if cols:
                # Subscript, NOT attribute access: a column named `items`/`keys`/
                # `values`/`count` collides with ColumnCollection's methods, so
                # `stmt.excluded.items` returns the bound method (-> "can't adapt
                # type 'method'"). `stmt.excluded[c]` always returns the column.
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(conflict_cols),
                    set_={c: stmt.excluded[c] for c in cols},
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
            session.execute(stmt)
            written += len(batch)
        return written
    finally:
        _record_write(model.__tablename__, attempted, written)


# ---------------------------------------------------------------------------
def save_universe(
    session: Session, as_of: dt.date, rows: Sequence[dict[str, Any]]
) -> int:
    payload = [{**r, "as_of_date": as_of} for r in rows]
    return _upsert(
        session, UniverseSnapshot, payload, ["as_of_date", "ticker"]
    )


def save_bars(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, DailyBar, list(rows), ["ticker", "date"])


def save_fundamentals(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    """Persist as-reported fundamentals, one row per natural key per load.

    SEC's bulk num.txt carries the same (ticker, metric, period_end) fact more
    than once in a single quarter's file: amended filings (10-K/A, 10-Q/A) repeat
    prior facts, and several XBRL tags map to one of our metrics, so a filing that
    reports both aliases yields two identical-key rows.

    Within a load we collapse to the EARLIEST filing_date -- what was actually
    known at the time. Keeping the latest would import a later restatement into an
    earlier date, which is exactly the lookahead bias the PIT layer exists to
    prevent. `filing_date` stays in the DB constraint on purpose: a restatement
    filed later is a genuinely new fact, visible only from its own filing date, so
    a later load may legitimately add a second row for the same period, and
    `pit.get_fundamentals` picks the latest one visible as of the read date.
    """
    ordered = sorted(
        rows,
        # None sorts last, so a row with a real filing_date always beats one without.
        key=lambda r: (r.get("filing_date") is None, r.get("filing_date") or dt.date.max),
    )
    collapsed, _ = dedupe_on(
        ordered,
        ["ticker", "metric", "period_end", "source"],
        label="fundamentals:earliest_filing",
    )
    return _upsert(
        session,
        Fundamental,
        collapsed,
        ["ticker", "metric", "period_end", "source", "filing_date"],
    )


def save_estimates(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(
        session, EstimateSnapshot, list(rows), ["ticker", "captured_on", "horizon"]
    )


def save_earnings(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    """Persist earnings events, one row per (ticker, report_date) per load.

    Same collision as fundamentals, from the same source: a company can file a
    10-Q and a 10-K/A on one day, so the bulk extract yields two events with the
    same natural key. `report_date` IS the filing date here, so "earliest filing"
    cannot break the tie and there is no lookahead either way -- both were known
    that day. We break it deterministically instead: prefer the record that
    carries a real actual_eps, then the LATEST period_end, which is the freshest
    fiscal period reported that day (the actual earnings event, not an amendment
    of an old one).
    """
    ordered = sorted(
        rows,
        key=lambda r: (
            r.get("actual_eps") is None,          # rows with EPS first
            -(r.get("period_end") or dt.date.min).toordinal(),  # latest period first
        ),
    )
    collapsed, _ = dedupe_on(
        ordered, ["ticker", "report_date"], label="earnings:same_day_filings"
    )
    return _upsert(session, EarningsEvent, collapsed, ["ticker", "report_date"])


def save_filings(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, FilingEvent, list(rows), ["accession", "ticker"])


def save_insider_transactions(
    session: Session, rows: Sequence[dict[str, Any]]
) -> int:
    if not rows:
        return 0
    session.add_all([InsiderTransaction(**r) for r in rows])
    return len(rows)


def replace_insider_transactions(
    session: Session, ticker: str, rows: Sequence[dict[str, Any]]
) -> int:
    """Insider rows have no natural key we trust, so we replace per ticker."""
    session.execute(
        delete(InsiderTransaction).where(InsiderTransaction.ticker == ticker)
    )
    return save_insider_transactions(session, rows)


def save_news(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, NewsAggregate, list(rows), ["ticker", "as_of_date"])


def save_sector_map(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, SectorMap, list(rows), ["ticker"])


def get_sector_map(session: Session) -> dict[str, dict[str, Any]]:
    """{ticker -> {sector, sector_source}} for every cached name.

    Includes unmapped names (sector=None, sector_source='unknown') so the
    universe builder can record provenance for all of them, not just the mapped
    ones -- IC analysis needs to know which sectors are guessed vs real.
    """
    rows = session.execute(
        select(SectorMap.ticker, SectorMap.sector, SectorMap.sector_source)
    ).all()
    return {t: {"sector": s, "sector_source": src} for t, s, src in rows}


def sector_map_stats(session: Session, sample: int = 8) -> dict[str, Any]:
    """Sector-map coverage + a sample of raw SIC -> GICS mappings, for
    /diagnostics -- so 'sector coverage is 0' is answerable without a shell:
    is the map empty (backfill not run), or populated but unmapped (SIC->GICS
    gap)?"""
    from sqlalchemy import func

    total = session.execute(
        select(func.count()).select_from(SectorMap)
    ).scalar_one()
    mapped = session.execute(
        select(func.count()).select_from(SectorMap).where(SectorMap.sector.isnot(None))
    ).scalar_one()
    rows = session.execute(
        select(
            SectorMap.ticker, SectorMap.sic, SectorMap.sic_description, SectorMap.sector
        ).where(SectorMap.sector.isnot(None)).limit(sample)
    ).all()
    unmapped_rows = session.execute(
        select(SectorMap.ticker, SectorMap.sic, SectorMap.sic_description)
        .where(SectorMap.sector.is_(None)).limit(sample)
    ).all()
    return {
        "total": total,
        "mapped": mapped,
        "unmapped": total - mapped,
        "mapped_ratio": round(mapped / total, 3) if total else 0.0,
        "sample_mapped": [
            {"ticker": t, "sic": sic, "desc": desc, "sector": sec}
            for t, sic, desc, sec in rows
        ],
        "sample_unmapped": [
            {"ticker": t, "sic": sic, "desc": desc}
            for t, sic, desc in unmapped_rows
        ],
    }


def fundamentals_stats(session: Session) -> dict[str, Any]:
    """Fundamentals coverage for /diagnostics -- answers 'the fundamentals table
    is empty' with numbers: row count, distinct tickers, latest filing, and how
    many of the current universe have at least one record."""
    from sqlalchemy import func

    rows = session.execute(
        select(func.count()).select_from(Fundamental)
    ).scalar_one()
    distinct = session.execute(
        select(func.count(func.distinct(Fundamental.ticker)))
    ).scalar_one()
    latest = session.execute(
        select(func.max(Fundamental.filing_date))
    ).scalar_one_or_none()

    latest_uni = session.execute(
        select(func.max(UniverseSnapshot.as_of_date))
    ).scalar_one_or_none()
    uni_total = 0
    uni_with = 0
    if latest_uni is not None:
        uni_tickers = set(
            session.execute(
                select(UniverseSnapshot.ticker).where(
                    UniverseSnapshot.as_of_date == latest_uni
                )
            ).scalars().all()
        )
        uni_total = len(uni_tickers)
        if uni_tickers and rows:
            fund_tickers = set(
                session.execute(select(func.distinct(Fundamental.ticker))).scalars().all()
            )
            uni_with = len(uni_tickers & fund_tickers)
    return {
        "rows": rows,
        "distinct_tickers": distinct,
        "latest_filing_date": latest.isoformat() if latest else None,
        "universe_total": uni_total,
        "universe_with_fundamentals": uni_with,
        "universe_coverage": round(uni_with / uni_total, 3) if uni_total else 0.0,
    }


def sector_map_ciks(session: Session) -> set[str]:
    """CIKs already mapped, so a sector backfill can skip them (pull once)."""
    rows = session.execute(
        select(SectorMap.cik).where(SectorMap.cik.isnot(None))
    ).scalars()
    return {str(c) for c in rows}


def save_macro(session: Session, row: dict[str, Any]) -> None:
    _upsert(session, MacroSnapshot, [row], ["as_of_date"])


def save_theses(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, Thesis, list(rows), ["as_of_date", "ticker"])


def save_scores(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, DailyScore, list(rows), ["as_of_date", "ticker"])


def save_report_artifact(
    session: Session,
    as_of: dt.date,
    *,
    run_id: str | None = None,
    html: str | None = None,
    pdf: bytes | None = None,
) -> None:
    """Persist the rendered report to the DB so the api service can serve it
    regardless of which service (or filesystem) rendered it."""
    _upsert(
        session,
        ReportArtifact,
        [
            {
                "as_of_date": as_of,
                "run_id": run_id,
                "html": html,
                "pdf": pdf,
                "html_bytes": len(html.encode()) if html else 0,
                "pdf_bytes": len(pdf) if pdf else 0,
            }
        ],
        ["as_of_date"],
    )


def get_report_artifact(session: Session, as_of: dt.date) -> ReportArtifact | None:
    return session.execute(
        select(ReportArtifact).where(ReportArtifact.as_of_date == as_of)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------
def jsonable(obj: Any) -> Any:
    """Coerce a checkpoint payload into something the JSON column accepts.

    Stage 3 detail carries dates, numpy scalars and NaN, none of which
    json.dumps handles. NaN becomes None rather than the non-standard `NaN`
    literal, so the stored JSON is portable and reloads as a proper null.
    """
    import math

    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    # numpy / pandas scalars expose .item()
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return jsonable(item())
        except (ValueError, TypeError):
            pass
    return str(obj)


def save_checkpoint(
    session: Session,
    run_id: str,
    as_of: dt.date,
    stage: int,
    *,
    entry_count: int,
    exit_count: int,
    duration_s: float,
    api_calls: int,
    payload: Any,
    rejected: Any = None,
) -> None:
    from src.logging_config import peak_rss_mb

    _upsert(
        session,
        StageResult,
        [
            {
                "run_id": run_id,
                "as_of_date": as_of,
                "stage": stage,
                "entry_count": entry_count,
                "exit_count": exit_count,
                "duration_s": duration_s,
                "api_calls": api_calls,
                # `data` is the resume payload; `rss_mb` is the process high-water
                # mark captured as this stage checkpoints, surfaced in /diagnostics.
                "payload": {"data": jsonable(payload), "rss_mb": peak_rss_mb()},
                "rejected": (
                    {"data": jsonable(rejected)} if rejected is not None else None
                ),
            }
        ],
        ["run_id", "stage"],
    )


def load_checkpoint(session: Session, run_id: str, stage: int) -> Any | None:
    row = session.execute(
        select(StageResult)
        .where(StageResult.run_id == run_id)
        .where(StageResult.stage == stage)
    ).scalar_one_or_none()
    if row is None or row.payload is None:
        return None
    return row.payload.get("data")


def latest_run_id(session: Session, as_of: dt.date) -> str | None:
    return session.execute(
        select(RunLog.run_id)
        .where(RunLog.as_of_date == as_of)
        .order_by(RunLog.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def latest_running_run(session: Session) -> RunLog | None:
    """The most recently started run still marked `running`, if any.

    Powers the live progress the one-button UI polls for: it lets `/status`
    report which stage the in-flight run has reached without any shared memory
    between the API request and the background pipeline task.
    """
    return session.execute(
        select(RunLog)
        .where(RunLog.status == "running")
        .order_by(RunLog.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def stage_progress(session: Session, run_id: str) -> list[dict[str, Any]]:
    """Per-stage entry/exit counts for a run, ordered by stage.

    Read straight off the checkpoints the pipeline writes as it advances, so the
    front-end can show "Stage 3: 400 -> 100" while the funnel is still running.
    Includes created_at timestamp so staleness detection can measure checkpoint age.
    """
    rows = session.execute(
        select(StageResult)
        .where(StageResult.run_id == run_id)
        .order_by(StageResult.stage)
    ).scalars()
    return [
        {
            "stage": r.stage,
            "entry_count": r.entry_count,
            "exit_count": r.exit_count,
            "duration_s": r.duration_s,
            "api_calls": r.api_calls,
            "rss_mb": (r.payload or {}).get("rss_mb"),
            "created_at": r.created_at,
        }
        for r in rows
    ]


def latest_run(session: Session) -> RunLog | None:
    """The most recently STARTED run (running or finished), for diagnostics."""
    return session.execute(
        select(RunLog).order_by(RunLog.started_at.desc()).limit(1)
    ).scalar_one_or_none()


def mark_orphaned_runs_failed(session: Session) -> int:
    """Fail any run still marked `running` with no recent progress.

    A run executes in-process; a container restart (redeploy or OOM) kills it
    without calling finish_run, leaving RunLog stuck at "running" forever. That
    ghost then shows up in /status.current_run and the site polls it endlessly.
    Also marks runs that have been "running" for >10 minutes with no stage
    checkpoint as stalled/hung, releasing the single-flight lock.
    Called at startup and every 10 minutes via the orphan sweep job.
    """
    stale_threshold = dt.datetime.utcnow() - dt.timedelta(minutes=10)

    # Find all running runs
    running = session.execute(
        select(RunLog).where(RunLog.status == "running")
    ).scalars().all()

    marked = 0
    for r in running:
        # Check if this run is stale: either started >10 min ago, OR if it has
        # a checkpoint, that checkpoint is >10 min old (indicating stuck in a stage).
        is_old_start = r.started_at < stale_threshold

        # Find the most recent checkpoint for this run
        latest_checkpoint = session.execute(
            select(StageResult)
            .where(StageResult.run_id == r.run_id)
            .order_by(StageResult.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        is_stale_checkpoint = (
            latest_checkpoint and latest_checkpoint.created_at < stale_threshold
        )

        if is_old_start or is_stale_checkpoint:
            r.status = "failed"
            reason = "no progress for 10+ minutes (likely process restart or hung stage)"
            r.error = (r.error or reason)[:2000]
            r.finished_at = dt.datetime.utcnow()
            marked += 1

    return marked


def fail_run_if_running(session: Session, run_id: str, error: str) -> bool:
    """Mark one run `failed` iff it is still `running`. Returns whether it did.

    The pipeline runs in a child process; if that child is OOM-killed or
    segfaults, its own error handler never runs, so its RunLog stays `running`
    forever and drives the /status poll loop. The parent that spawned it calls
    this on a non-zero exit to reconcile the ghost. Idempotent: a run that
    already finished (the child wrote its own success/failure) is left alone.
    """
    run = session.execute(
        select(RunLog).where(RunLog.run_id == run_id)
    ).scalar_one_or_none()
    if run is None or run.status != "running":
        return False
    run.status = "failed"
    run.error = (error or "the run process exited without finishing")[:2000]
    run.finished_at = dt.datetime.now(dt.UTC)
    return True


def max_checkpoint_stage(session: Session, run_id: str) -> int:
    """Highest stage with a saved checkpoint for this run, or 0 if none.

    A resume replays from `this + 1`: everything through here is restorable, the
    next stage is where the run actually died.
    """
    val = session.execute(
        select(StageResult.stage)
        .where(StageResult.run_id == run_id)
        .order_by(StageResult.stage.desc())
        .limit(1)
    ).scalar_one_or_none()
    return int(val) if val is not None else 0


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------
def start_run(
    session: Session, run_id: str, as_of: dt.date, mode: str = "full"
) -> None:
    """Idempotent. A resume reuses the original run_id, so re-inserting would
    violate the unique constraint -- reset the existing row to running instead."""
    existing = session.execute(
        select(RunLog).where(RunLog.run_id == run_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = "running"
        existing.error = None
        existing.finished_at = None
        existing.mode = mode
        return
    session.add(RunLog(run_id=run_id, as_of_date=as_of, status="running", mode=mode))


def finish_run(
    session: Session,
    run_id: str,
    *,
    status: str,
    regime: str | None = None,
    funnel_counts: dict | None = None,
    api_calls: dict | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    report_path: str | None = None,
    error: str | None = None,
) -> None:
    row = session.execute(
        select(RunLog).where(RunLog.run_id == run_id)
    ).scalar_one_or_none()
    if row is None:
        return
    row.finished_at = dt.datetime.now(dt.UTC)
    row.status = status
    row.regime = regime
    row.funnel_counts = funnel_counts
    row.api_calls = api_calls
    row.tokens_in = tokens_in
    row.tokens_out = tokens_out
    row.cost_usd = cost_usd
    row.report_path = report_path
    row.error = error


def get_run(session: Session, as_of: dt.date) -> RunLog | None:
    return session.execute(
        select(RunLog)
        .where(RunLog.as_of_date == as_of)
        .order_by(RunLog.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def list_runs(session: Session, limit: int = 30) -> list[RunLog]:
    return list(
        session.execute(
            select(RunLog).order_by(RunLog.as_of_date.desc()).limit(limit)
        )
        .scalars()
        .all()
    )


def get_theses(session: Session, as_of: dt.date) -> list[Thesis]:
    return list(
        session.execute(
            select(Thesis)
            .where(Thesis.as_of_date == as_of)
            .order_by(Thesis.total_score.desc())
        )
        .scalars()
        .all()
    )


def get_scores(
    session: Session, as_of: dt.date, min_stage: int = 1
) -> list[DailyScore]:
    return list(
        session.execute(
            select(DailyScore)
            .where(DailyScore.as_of_date == as_of)
            .where(DailyScore.stage_reached >= min_stage)
        )
        .scalars()
        .all()
    )


def iter_score_dates(session: Session) -> Iterable[dt.date]:
    return session.execute(
        select(DailyScore.as_of_date).distinct().order_by(DailyScore.as_of_date)
    ).scalars()
