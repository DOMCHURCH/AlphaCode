"""FastAPI read API and report server."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
)
from pydantic import BaseModel
from sqlalchemy import select

from src.config.settings import get_settings
from src.logging_config import configure_logging
from src.storage import repository
from src.storage.db import init_db, session_scope
from src.storage.models import DailyBar, DailyScore, Thesis
from src.validation.backtest import benchmark_against_null
from src.validation.ic import backfill_forward_returns, ic_report

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Migrate the database (tables + indexes) against the real DATABASE_URL when
    the service boots. Failures are logged, not fatal -- /health reports the DB
    state, and Railway's healthcheck grace period lets Postgres come up."""
    configure_logging()
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001 - health endpoint reports it
        log.error("db_init_failed", error=str(exc))
    yield


app = FastAPI(
    title="Daily Equity Alpha Funnel",
    version="1.0.0",
    description=(
        "Research and idea-generation tool. Surfaces candidates for a human to "
        "evaluate. Scores are the output of a heuristic pipeline plus a language "
        "model's interpretation, not a prediction. Nothing here is investment advice."
    ),
    lifespan=lifespan,
)

_run_lock = asyncio.Lock()


class HealthResponse(BaseModel):
    status: str
    database: str
    latest_run: str | None = None
    latest_status: str | None = None
    version: str = "1.0.0"


class NameSummary(BaseModel):
    ticker: str
    total_score: int
    conviction: str | None = None
    subscores: dict[str, Any] | None = None
    thesis: str | None = None
    bull_case: str | None = None
    bear_case: str | None = None
    invalidation: str | None = None
    time_horizon_days: int | None = None
    key_risks: list[Any] = []
    catalysts_ahead: list[Any] = []


class ReportResponse(BaseModel):
    as_of: dt.date
    regime: str | None = None
    funnel: dict[str, Any] | None = None
    cost_usd: float | None = None
    names: list[NameSummary]
    report_url: str | None = None


class RunResponse(BaseModel):
    accepted: bool
    run_id: str | None = None
    detail: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    db_status = "ok"
    latest_run = latest_status = None
    try:
        with session_scope() as session:
            runs = repository.list_runs(session, limit=1)
            if runs:
                latest_run = runs[0].as_of_date.isoformat()
                latest_status = runs[0].status
    except Exception as exc:  # noqa: BLE001
        db_status = f"error: {str(exc)[:120]}"
    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
        latest_run=latest_run,
        latest_status=latest_status,
    )


@app.get("/report/{date}", response_model=ReportResponse)
def get_report(date: str) -> ReportResponse:
    as_of = _parse_date(date)
    with session_scope() as session:
        run = repository.get_run(session, as_of)
        theses = repository.get_theses(session, as_of)
        if not theses and run is None:
            raise HTTPException(404, f"No run found for {as_of}")
        return ReportResponse(
            as_of=as_of,
            regime=run.regime if run else None,
            funnel=run.funnel_counts if run else None,
            cost_usd=run.cost_usd if run else None,
            report_url=f"/report/{as_of.isoformat()}/html" if run and run.report_path else None,
            names=[
                NameSummary(
                    ticker=t.ticker,
                    total_score=t.total_score,
                    conviction=t.conviction,
                    subscores=t.subscores,
                    thesis=t.thesis,
                    bull_case=t.bull_case,
                    bear_case=t.bear_case,
                    invalidation=t.invalidation,
                    time_horizon_days=t.time_horizon_days,
                    key_risks=t.key_risks or [],
                    catalysts_ahead=t.catalysts_ahead or [],
                )
                for t in theses
            ],
        )


@app.get("/report/{date}/html", response_class=HTMLResponse)
def get_report_html(date: str) -> HTMLResponse:
    as_of = _parse_date(date)
    # Prefer the DB copy: on Railway the worker rendered on a different, ephemeral
    # filesystem, so the api's local disk almost never has the file.
    with session_scope() as session:
        artifact = repository.get_report_artifact(session, as_of)
        if artifact and artifact.html:
            return HTMLResponse(artifact.html)
        run = repository.get_run(session, as_of)
    path = Path(run.report_path) if run and run.report_path else None
    if path is None or not path.exists():
        path = Path(get_settings().report_dir) / f"report_{as_of.isoformat()}.html"
    if not path.exists():
        raise HTTPException(404, f"No rendered report for {as_of}")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/report/{date}/pdf")
def get_report_pdf(date: str) -> Response:
    as_of = _parse_date(date)
    filename = f"report_{as_of.isoformat()}.pdf"
    with session_scope() as session:
        artifact = repository.get_report_artifact(session, as_of)
        if artifact and artifact.pdf:
            return Response(
                content=bytes(artifact.pdf),
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{filename}"'},
            )
    path = Path(get_settings().report_dir) / filename
    if not path.exists():
        raise HTTPException(404, f"No PDF for {as_of}")
    return FileResponse(path, media_type="application/pdf", filename=filename)


@app.get("/reports")
def list_reports(limit: int = Query(30, ge=1, le=200)) -> list[dict[str, Any]]:
    with session_scope() as session:
        return [
            {
                "as_of": r.as_of_date.isoformat(),
                "run_id": r.run_id,
                "status": r.status,
                "regime": r.regime,
                "funnel": r.funnel_counts,
                "cost_usd": r.cost_usd,
                "duration_s": (
                    (r.finished_at - r.started_at).total_seconds()
                    if r.finished_at and r.started_at
                    else None
                ),
            }
            for r in repository.list_runs(session, limit)
        ]


@app.get("/ticker/{symbol}/history")
def ticker_history(
    symbol: str, days: int = Query(180, ge=1, le=1500)
) -> dict[str, Any]:
    symbol = symbol.upper()
    since = dt.date.today() - dt.timedelta(days=days)
    with session_scope() as session:
        bars = list(
            session.execute(
                select(DailyBar)
                .where(DailyBar.ticker == symbol)
                .where(DailyBar.date >= since)
                .order_by(DailyBar.date)
            ).scalars()
        )
        scores = list(
            session.execute(
                select(DailyScore)
                .where(DailyScore.ticker == symbol)
                .where(DailyScore.as_of_date >= since)
                .order_by(DailyScore.as_of_date)
            ).scalars()
        )
        theses = list(
            session.execute(
                select(Thesis)
                .where(Thesis.ticker == symbol)
                .where(Thesis.as_of_date >= since)
                .order_by(Thesis.as_of_date)
            ).scalars()
        )
    if not bars and not scores:
        raise HTTPException(404, f"No history for {symbol}")
    return {
        "ticker": symbol,
        "bars": [
            {
                "date": b.date.isoformat(), "open": b.open, "high": b.high,
                "low": b.low, "close": b.close, "volume": b.volume,
            }
            for b in bars
        ],
        "scores": [
            {
                "date": s.as_of_date.isoformat(),
                "stage_reached": s.stage_reached,
                "factor_composite": s.factor_composite,
                "catalyst_score": s.catalyst_score,
                "llm_total_score": s.llm_total_score,
                "final_rank": s.final_rank,
                "factor_detail": s.factor_detail,
                "fwd_ret_1d": s.fwd_ret_1d,
                "fwd_ret_5d": s.fwd_ret_5d,
                "fwd_ret_21d": s.fwd_ret_21d,
            }
            for s in scores
        ],
        "theses": [
            {
                "date": t.as_of_date.isoformat(),
                "total_score": t.total_score,
                "thesis": t.thesis,
                "invalidation": t.invalidation,
                "conviction": t.conviction,
            }
            for t in theses
        ],
    }


@app.get("/validation")
def validation(
    since: str | None = None, backfill: bool = True
) -> dict[str, Any]:
    """IC tracking, factor decay, turnover, and the null benchmark."""
    since_date = _parse_date(since) if since else None
    with session_scope() as session:
        if backfill:
            backfill_forward_returns(session, dt.date.today())
        report = ic_report(session, since_date)
        report["benchmark_vs_null"] = benchmark_against_null(session).to_dict()
    report["notes"] = (
        "21-day IC above 0.03 sustained is a real signal; below 0.02 is dead "
        "weight. If the strategy does not beat the random-draw null, only the "
        "trend gate is doing work."
    )
    return report


@app.post("/run", response_model=RunResponse)
async def trigger_run(
    background: BackgroundTasks,
    date: str | None = None,
    skip_llm: bool = False,
) -> RunResponse:
    """Manual trigger. Returns immediately; the run proceeds in the background."""
    if _run_lock.locked():
        return RunResponse(
            accepted=False, detail="A run is already in progress."
        )
    as_of = _parse_date(date) if date else None
    background.add_task(_run_pipeline_bg, as_of, skip_llm)
    return RunResponse(
        accepted=True,
        detail=f"Run queued for {as_of or 'the last trading day'}.",
    )


async def _run_pipeline_bg(as_of: dt.date | None, skip_llm: bool) -> None:
    from src.pipeline import run_pipeline

    async with _run_lock:
        try:
            await run_pipeline(as_of, skip_llm=skip_llm)
        except Exception as exc:  # noqa: BLE001 - logged, never crashes the API
            log.exception("manual_run_failed", error=str(exc))


@app.get("/")
def root() -> JSONResponse:
    return JSONResponse(
        {
            "service": "Daily Equity Alpha Funnel",
            "endpoints": [
                "/health", "/reports", "/report/{date}", "/report/{date}/html",
                "/report/{date}/pdf", "/ticker/{symbol}/history", "/validation",
                "POST /run",
            ],
            "disclaimer": (
                "Research and idea-generation only. Not investment advice."
            ),
        }
    )


def _parse_date(value: str) -> dt.date:
    if value in ("latest", "today"):
        with session_scope() as session:
            runs = repository.list_runs(session, limit=1)
        if not runs:
            raise HTTPException(404, "No runs recorded yet")
        return runs[0].as_of_date
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError as exc:
        raise HTTPException(400, f"Invalid date {value!r}; use YYYY-MM-DD") from exc
