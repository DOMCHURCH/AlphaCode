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
from fastapi.staticfiles import StaticFiles
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


async def _boot(app: FastAPI) -> None:
    """Migrate the DB and (optionally) start the scheduler, off the critical path.

    Run in a background task so the HTTP server binds and answers /health
    immediately -- a slow or unreachable Postgres must never stall startup past
    Railway's healthcheck window. The DB work runs in a worker thread so its
    synchronous connect can't block the event loop; /health reports DB state
    independently.
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(init_db), timeout=30)
        log.info("db_ready")
    except Exception as exc:  # noqa: BLE001 - health endpoint reports it
        log.error("db_init_failed", error=str(exc)[:300])

    if get_settings().enable_scheduler:
        try:
            from src.scheduler import build_scheduler

            scheduler = build_scheduler()
            scheduler.start()
            app.state.scheduler = scheduler
            s = get_settings()
            log.info(
                "scheduler_started_in_api",
                schedule=f"{s.run_hour:02d}:{s.run_minute:02d} "
                f"{s.run_timezone} mon-fri",
            )
        except Exception as exc:  # noqa: BLE001 - serving must survive
            log.error("scheduler_start_failed", error=str(exc)[:300])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the service without blocking the healthcheck.

    Startup (DB migration + optional in-process scheduler) runs as a background
    task so `uvicorn` starts accepting requests immediately. In single-service
    mode (ENABLE_SCHEDULER=true) the daily funnel cron runs in this same process.
    """
    try:
        configure_logging()
    except Exception:  # noqa: BLE001 - never let logging setup stall startup
        pass
    app.state.scheduler = None
    boot_task = asyncio.create_task(_boot(app))
    try:
        yield
    finally:
        boot_task.cancel()
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            log.info("scheduler_stopped_in_api")


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
_backfill_lock = asyncio.Lock()

# Serve the dashboard's CSS/JS (and any future assets) as static files, so the
# front-end lives in real .css/.js files instead of one inlined HTML blob.
_STATIC_DIR = Path(__file__).parent / "report" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


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


@app.get("/stock/{symbol}", response_class=HTMLResponse)
def stock_detail(symbol: str) -> HTMLResponse:
    """A full, standalone detail page for one ticker: price chart with event
    markers, factor radar, 8-quarter fundamentals, peer comparison, and the
    written thesis. Assembled from stored data, so any screened name opens."""
    from src.report.detail import build_stock_detail

    with session_scope() as session:
        html = build_stock_detail(session, symbol)
    if html is None:
        raise HTTPException(404, f"No stored data for {symbol.upper()}")
    return HTMLResponse(html)


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
    """Manual trigger. Returns immediately; the run proceeds in the background.

    Open by design -- no token needed. A run is single-flighted by `_run_lock`,
    so hitting this repeatedly just returns "already running" rather than
    stacking work."""
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


@app.post("/backfill", response_model=RunResponse)
async def trigger_backfill(
    background: BackgroundTasks,
    days: int = Query(600, ge=1, le=2000),
    fundamentals: bool = False,
) -> RunResponse:
    """Load history so the funnel has something to screen. Curl-triggerable so no
    shell is needed. Returns immediately; the load runs in the background.

    Open by design -- no token needed; single-flighted by `_backfill_lock`.
    `days` price sessions of bars (~500 Polygon calls). `fundamentals=true` also
    pulls SEC XBRL as-reported fundamentals, which is slow (can take hours for the
    full universe) -- do it after the bars load succeeds, not on the first call.
    """
    if _backfill_lock.locked():
        return RunResponse(accepted=False, detail="A backfill is already running.")
    background.add_task(_backfill_bg, days, fundamentals)
    return RunResponse(
        accepted=True,
        detail=(
            f"Backfill queued: {days} sessions of bars"
            + (" + SEC fundamentals (slow)" if fundamentals else "")
            + ". Watch GET /status for progress."
        ),
    )


async def _backfill_bg(days: int, fundamentals: bool) -> None:
    from src.backfill import backfill_bars, backfill_fundamentals

    async with _backfill_lock:
        try:
            n = await backfill_bars(days)
            log.info("backfill_bars_done", rows=n)
            if fundamentals:
                m = await backfill_fundamentals()
                log.info("backfill_fundamentals_done", rows=m)
        except Exception as exc:  # noqa: BLE001 - logged, never crashes the API
            log.exception("backfill_failed", error=str(exc))


# Human-readable stage labels + the count each stage narrows the funnel to, so
# the one-button UI can render a progress bar from the raw checkpoints.
_STAGE_STEPS: list[dict[str, Any]] = [
    {"stage": 0, "label": "Building the universe", "target": 6000},
    {"stage": 1, "label": "Trend gate — is it going up right now?", "target": 1200},
    {"stage": 2, "label": "Multi-factor scoring", "target": 400},
    {"stage": 3, "label": "Catalysts & news", "target": 100},
    {"stage": 4, "label": "LLM triage", "target": 25},
    {"stage": 5, "label": "LLM deep dive & scoring", "target": 10},
    {"stage": 6, "label": "Writing the report", "target": 10},
]
_TOTAL_STAGES = len(_STAGE_STEPS)

# Stage 1 evaluates a 52-week high, a 12-month return and a 200-day SMA, so the
# funnel needs about a year of sessions before its first run means anything.
# Readiness is measured in *trading days loaded*, not raw bar count -- one
# grouped-daily call adds ~10k bars for a single session, so a bar-count
# threshold flips "ready" after ~10 days when the gate still has no history.
MIN_HISTORY_DATES = 252


def _current_run_progress(session: Any) -> dict[str, Any] | None:
    """Live stage progress for the in-flight run, or None when idle."""
    run = repository.latest_running_run(session)
    if run is None:
        return None
    stages = repository.stage_progress(session, run.run_id)
    reached = max((s["stage"] for s in stages), default=0)
    # The pipeline checkpoints a stage *after* it completes, so the stage
    # actively being worked is the one after the highest checkpoint.
    active = min(reached + 1, _TOTAL_STAGES - 1)
    return {
        "run_id": run.run_id,
        "as_of": run.as_of_date.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "active_stage": active,
        "active_label": _STAGE_STEPS[active]["label"],
        "stages_total": _TOTAL_STAGES,
        "percent": round(100 * active / (_TOTAL_STAGES - 1)),
        "steps": [
            {
                **_STAGE_STEPS[i],
                "done": i <= reached,
                "active": i == active,
                "survivors": next(
                    (s["exit_count"] for s in stages if s["stage"] == i), None
                ),
            }
            for i in range(_TOTAL_STAGES)
        ],
    }


@app.get("/status")
def status() -> dict[str, Any]:
    """Row counts so you can watch the backfill fill up and confirm readiness."""
    from sqlalchemy import func

    from src.storage.models import DailyBar, UniverseSnapshot

    out: dict[str, Any] = {"backfill_running": _backfill_lock.locked(),
                           "run_in_progress": _run_lock.locked()}
    try:
        with session_scope() as session:
            out["price_bars"] = session.execute(
                select(func.count()).select_from(DailyBar)
            ).scalar_one()
            out["distinct_tickers_with_bars"] = session.execute(
                select(func.count(func.distinct(DailyBar.ticker)))
            ).scalar_one()
            latest_bar = session.execute(
                select(func.max(DailyBar.date))
            ).scalar_one()
            out["latest_bar_date"] = latest_bar.isoformat() if latest_bar else None
            # Trading days loaded -- the honest measure of "enough history".
            out["bar_dates"] = session.execute(
                select(func.count(func.distinct(DailyBar.date)))
            ).scalar_one()
            out["history_target"] = MIN_HISTORY_DATES
            out["universe_snapshots"] = session.execute(
                select(func.count(func.distinct(UniverseSnapshot.as_of_date)))
            ).scalar_one()
            out["runs"] = len(repository.list_runs(session, limit=1000))
            out["current_run"] = _current_run_progress(session)
        out["ready_for_first_run"] = (out.get("bar_dates") or 0) >= MIN_HISTORY_DATES
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out


_DASHBOARD = Path(__file__).parent / "report" / "templates" / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    """The dashboard: latest report, past reports, live status, run/backfill
    buttons. A static page that reads the JSON endpoints below via fetch()."""
    try:
        return HTMLResponse(_DASHBOARD.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse(
            "<h1>Daily Equity Alpha Funnel</h1><p>See <a href='/api'>/api</a>.</p>"
        )


@app.get("/api")
def api_index() -> JSONResponse:
    return JSONResponse(
        {
            "service": "Daily Equity Alpha Funnel",
            "endpoints": [
                "/health", "/status", "/reports", "/report/{date}",
                "/report/{date}/html", "/report/{date}/pdf",
                "/ticker/{symbol}/history", "/validation",
                "POST /backfill", "POST /run",
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
