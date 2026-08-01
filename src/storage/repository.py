"""Write-side helpers. Upserts, checkpoints, score persistence."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from typing import Any

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
    StageResult,
    Thesis,
    UniverseSnapshot,
)


def _upsert(
    session: Session,
    model: type[Base],
    rows: Sequence[dict[str, Any]],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
    chunk: int = 2000,
) -> int:
    """Dialect-aware ON CONFLICT DO UPDATE. Works on Postgres and SQLite."""
    if not rows:
        return 0
    dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
    ins = pg_insert if dialect == "postgresql" else sqlite_insert
    total = 0
    for i in range(0, len(rows), chunk):
        batch = rows[i : i + chunk]
        stmt = ins(model).values(batch)
        cols = update_cols or [
            c for c in batch[0].keys() if c not in conflict_cols and c != "id"
        ]
        if cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict_cols),
                set_={c: getattr(stmt.excluded, c) for c in cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_cols))
        session.execute(stmt)
        total += len(batch)
    return total


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
    return _upsert(
        session,
        Fundamental,
        list(rows),
        ["ticker", "metric", "period_end", "source", "filing_date"],
    )


def save_estimates(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(
        session, EstimateSnapshot, list(rows), ["ticker", "captured_on", "horizon"]
    )


def save_earnings(session: Session, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(session, EarningsEvent, list(rows), ["ticker", "report_date"])


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
                "payload": {"data": jsonable(payload)},
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
def start_run(session: Session, run_id: str, as_of: dt.date) -> None:
    """Idempotent. A resume reuses the original run_id, so re-inserting would
    violate the unique constraint -- reset the existing row to running instead."""
    existing = session.execute(
        select(RunLog).where(RunLog.run_id == run_id)
    ).scalar_one_or_none()
    if existing is not None:
        existing.status = "running"
        existing.error = None
        existing.finished_at = None
        return
    session.add(RunLog(run_id=run_id, as_of_date=as_of, status="running"))


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
