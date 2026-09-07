"""FastAPI read API and admin surface.

The ranking funnel is gone. What remains is the data plane -- price bars, the
sector map, and SEC as-reported fundamentals -- plus `/admin`, the phone-first
break-glass page that found every bug in this project.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from src.config.settings import get_settings
from src.locks import BACKFILL
from src.logging_config import configure_logging
from src import scheduler
from src.storage import repository
from src.storage.db import init_db, session_scope
from src.storage.models import DailyBar

log = structlog.get_logger(__name__)


async def _boot(app: FastAPI) -> None:
    """Migrate the DB off the critical path.

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

    # The demo account, created or refreshed here rather than on first use: a
    # cold container must not serve the home page before the account behind its
    # demo exists, and a rotated DEMO_API_KEY should take effect on deploy
    # instead of whenever the next visitor happens to try it. Runs after
    # init_db so the table it writes to is there.
    try:
        from src.demo import ensure_demo_user

        await asyncio.to_thread(ensure_demo_user)
    except Exception as exc:  # noqa: BLE001 - a missing demo must not stop boot
        log.warning("demo_user_setup_skipped", error=str(exc)[:200])

    await _verify_ask_model()

    # The home page states its own identity pass rate. Computing it walks every
    # company, so it is warmed once here rather than on a reader's request; the
    # page renders without it and simply omits the line until it lands.
    try:
        from src.company.stats import warm_identity

        asyncio.create_task(asyncio.to_thread(warm_identity))
    except Exception as exc:  # noqa: BLE001 - never block boot on a statistic
        log.warning("site_identity_warm_skipped", error=str(exc)[:200])

    # The data keeps itself current from here. Started after init_db so the
    # first tick reads a migrated schema, and as a background task so a slow
    # first refresh never delays boot. See src/scheduler.py -- it decides what
    # is due by reading the data, which is what makes a deploy that lands late
    # in the quarter (or after an outage) catch up on its own.
    if scheduler.enabled():
        app.state.auto_update_task = asyncio.create_task(scheduler.run_forever())
    else:
        log.info("auto_update_disabled", mode=get_settings().auto_update,
                 env=get_settings().env)

    app.state.boot_complete = True


# The question box's model, resolved once at boot. None means the box is off,
# and _ASK_MODEL_ERROR says exactly why.
_ASK_MODEL: Any = None
_ASK_MODEL_ERROR: str | None = None


async def _verify_ask_model() -> None:
    """Resolve LLM_ASK_MODEL against OpenRouter's live model list.

    Loud, but not fatal. A wrong model id must never be discovered one reader
    at a time as an upstream 404, so it is checked here and the reason is
    carried to /admin, to /health's own log line, and to the endpoint's 503.
    It does not abort the process: the balance-sheet pages are the product and
    a misconfigured question box is not a reason to take them offline.
    """
    global _ASK_MODEL, _ASK_MODEL_ERROR
    from src.llm.client import ModelUnavailable, resolve_model

    s = get_settings()
    if not s.openrouter_api_key:
        _ASK_MODEL, _ASK_MODEL_ERROR = None, "OPENROUTER_API_KEY is not set."
        log.warning("llm_ask_disabled", reason=_ASK_MODEL_ERROR)
        return
    try:
        _ASK_MODEL = await resolve_model(s.llm_ask_model)
        _ASK_MODEL_ERROR = None
    except ModelUnavailable as exc:
        _ASK_MODEL, _ASK_MODEL_ERROR = None, str(exc)
        log.error("llm_ask_model_unavailable", model=s.llm_ask_model,
                  reason=str(exc)[:300])
    except Exception as exc:  # noqa: BLE001 - boot must complete regardless
        _ASK_MODEL = None
        _ASK_MODEL_ERROR = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.error("llm_ask_verify_failed", error=_ASK_MODEL_ERROR)


def ask_status() -> dict[str, Any]:
    """Whether the question box is on, and the numbers behind it, for /admin."""
    s = get_settings()
    out: dict[str, Any] = {
        "configured_model": s.llm_ask_model,
        "available": _ASK_MODEL is not None,
        "error": _ASK_MODEL_ERROR,
        "caps": {
            "per_ip_per_hour": s.llm_ask_per_ip_per_hour,
            "per_day": s.llm_ask_per_day,
            "daily_cost_usd": s.llm_ask_daily_cost_usd,
        },
    }
    if _ASK_MODEL is not None:
        out["model"] = {
            "id": _ASK_MODEL.id,
            "name": _ASK_MODEL.name,
            "prompt_usd_per_mtok": round(_ASK_MODEL.prompt_usd_per_token * 1e6, 4),
            "completion_usd_per_mtok": round(
                _ASK_MODEL.completion_usd_per_token * 1e6, 4
            ),
        }
    try:
        from src.llm.ask import usage_today

        out["today"] = usage_today()
    except Exception as exc:  # noqa: BLE001 - /admin must still render
        out["today"] = {"error": str(exc)[:200]}
    return out


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the service without blocking the healthcheck."""
    try:
        configure_logging()
    except Exception:  # noqa: BLE001 - never let logging setup stall startup
        pass
    app.state.boot_complete = False
    app.state.auto_update_task = None
    boot_task = asyncio.create_task(_boot(app))
    try:
        yield
    finally:
        boot_task.cancel()
        task = getattr(app.state, "auto_update_task", None)
        if task is not None:
            task.cancel()


app = FastAPI(
    title="To Scale",
    version="2.0.0",
    description=(
        "Filed financial statements, drawn at true proportion. SEC "
        "as-reported fundamentals, price bars, and the sector map. Descriptive "
        "only -- it makes no predictions and produces no scores."
    ),
    lifespan=lifespan,
)

@app.middleware("http")
async def count_visits(request: Request, call_next):
    """Record reader-facing page requests, without ever affecting the response.

    The write happens AFTER the response is produced and in a worker thread, so
    a synchronous insert cannot block the event loop or add latency the reader
    experiences. Every qualifying request is written -- no sampling and no
    in-memory buffer, because a buffer loses whatever it holds when the
    container recycles, and a visitor count that quietly drops rows is worse
    than no visitor count.
    """
    response = await call_next(request)
    try:
        from src import analytics

        path = request.url.path
        if request.method == "GET" and analytics.should_count(path):
            await asyncio.to_thread(
                analytics.record,
                path,
                analytics.client_ip(
                    request.headers,
                    request.client.host if request.client else "unknown",
                ),
                request.headers.get("user-agent"),
                status=response.status_code,
                referrer=request.headers.get("referer"),
            )
    except Exception as exc:  # noqa: BLE001 - never let counting break a page
        log.warning("visit_count_failed", error=str(exc)[:200])
    return response


# Every data load takes this, scheduled or manual, so an auto-refresh and a tap
# on /admin can never run at once. It lives in src/locks.py because the
# auto-updater holds the SAME lock and the two modules must not import each
# other.
_backfill_lock = BACKFILL


class _RateGate:
    """A per-process sliding-window rate limit for the open POST endpoints.

    `limit_fn` is read live so the limit is configurable via settings (0
    disables). Not per-IP -- this is a single personal service, and a global cap
    is what stops abuse.
    """

    def __init__(self, limit_fn: Callable[[], int]) -> None:
        self._limit_fn = limit_fn
        self._hits: deque[float] = deque()

    def check(self) -> float | None:
        """None if allowed (and records the hit); else seconds until retry."""
        limit = self._limit_fn()
        if limit <= 0:
            return None
        now = time.monotonic()
        cutoff = now - 3600
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()
        if len(self._hits) >= limit:
            return max(1.0, 3600 - (now - self._hits[0]))
        self._hits.append(now)
        return None

    def peek(self) -> float | None:
        """Like check() but WITHOUT recording a hit -- for reporting whether an
        action button should be enabled, which must not consume the budget."""
        limit = self._limit_fn()
        if limit <= 0:
            return None
        now = time.monotonic()
        cutoff = now - 3600
        hits = [h for h in self._hits if h >= cutoff]
        if len(hits) >= limit:
            return max(1.0, 3600 - (now - hits[0]))
        return None

    def reset(self) -> None:
        self._hits.clear()


_backfill_gate = _RateGate(lambda: get_settings().backfill_rate_per_hour)
_reconcile_gate = _RateGate(lambda: get_settings().reconcile_rate_per_hour)
# Registration is open and each key carries a free monthly allowance, so the
# per-key limit is only worth as much as a key costs to obtain. This is what
# makes it cost something.
_register_gate = _RateGate(lambda: get_settings().register_rate_per_hour)
# Key recovery sends mail to an address the requester does not have to own, so
# it is capped globally as well as per-address. Without the global cap, one
# script walking a list of addresses is a spam run with this service's name on it.
_resend_gate = _RateGate(lambda: get_settings().resend_rate_per_hour)
# Login links go to inboxes the requester does not have to own, so the same
# global ceiling applies as for key recovery.
_magic_link_gate = _RateGate(lambda: get_settings().magic_link_rate_per_hour)


def _enforce_rate(gate: _RateGate, what: str) -> None:
    retry = gate.check()
    if retry is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit reached for {what}. Try again in {int(retry)}s.",
            headers={"Retry-After": str(int(retry))},
        )


_STATIC_DIR = Path(__file__).parent / "report" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# The mark is the drawing in miniature -- owns, owed, left over -- on the same
# soft ground as the page. Inlined as SVG so there's no binary asset to ship.
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="9" fill="#E7EBF2"/>'
    '<path d="M16 4A12 12 0 0 1 28 16H16Z" fill="#2340BE"/>'
    '<path d="M28 16A12 12 0 0 1 16 28V16Z" fill="#F3C218"/>'
    '<path d="M16 28A12 12 0 0 1 4 16H16Z" fill="#E1362C"/>'
    '<path d="M4 16A12 12 0 0 1 16 4V16Z" fill="#CBD3E0"/>'
    "</svg>"
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(
        content=_FAVICON,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str = "2.0.0"


class RunResponse(BaseModel):
    accepted: bool
    run_id: str | None = None
    detail: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    db_status = "ok"
    try:
        with session_scope() as session:
            session.execute(select(1))
    except Exception as exc:  # noqa: BLE001
        db_status = f"error: {str(exc)[:120]}"
    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        database=db_status,
    )


# Cached result of the network-backed check, so /admin can render it without a
# call on every refresh (and without burning the reconcile rate limit).
_RECONCILE_CACHE: dict[str, Any] = {"at": None, "data": None}


@app.get("/reconcile")
async def reconcile_endpoint(sample: int = Query(15, ge=1, le=50)) -> dict[str, Any]:
    """The same checks as `python -m src.reconcile`, over HTTP for phones with no
    shell. Read-only and rate-limited (it makes a few network calls). Nothing is
    fabricated -- missing inputs are reported as such."""
    _enforce_rate(_reconcile_gate, "reconcile")
    from src.reconcile import reconcile

    data = await reconcile(sample=sample)
    _RECONCILE_CACHE["at"] = dt.datetime.now(dt.UTC).isoformat()
    _RECONCILE_CACHE["data"] = data
    return data


_BACKFILL_KINDS = (
    "bars", "sectors", "filings", "fundamentals", "earnings", "names",
)


@app.post("/backfill", response_model=RunResponse)
async def trigger_backfill(
    background: BackgroundTasks,
    kind: str = "bars",
    days: int = Query(600, ge=1, le=2000),
    # Back-compat with the old boolean flags; `kind` is the current interface.
    fundamentals: bool = False,
    sectors: bool = False,
) -> RunResponse:
    """Load ONE kind of data.

    `kind` = bars | sectors | filings | fundamentals | earnings | names.
    `filings` is the fast one: SEC's XBRL frames, which carry the current
    quarter months before its bulk dataset exists.

    Open by design; single-flighted by `_backfill_lock`.
    """
    if fundamentals:
        kind = "fundamentals"
    elif sectors:
        kind = "sectors"
    kind = kind.lower()
    if kind not in _BACKFILL_KINDS:
        raise HTTPException(400, f"kind must be one of {list(_BACKFILL_KINDS)}")
    _enforce_rate(_backfill_gate, "backfills")
    if _backfill_lock.locked():
        return RunResponse(accepted=False, detail="A backfill is already running.")
    background.add_task(_backfill_bg, kind, days)
    return RunResponse(
        accepted=True, detail=f"{kind} backfill queued. Watch GET /status for progress."
    )


async def _backfill_bg(kind: str, days: int) -> None:
    from src.backfill import (
        backfill_bars,
        backfill_earnings,
        backfill_filings,
        backfill_fundamentals,
        backfill_sectors,
        record_backfill_error,
        record_backfill_result,
    )

    # Log the requested kind BEFORE any work, so the log always says what was
    # asked for -- not just what ran.
    log.info("backfill_requested", kind=kind, days=days)
    async with _backfill_lock:
        try:
            if kind == "bars":
                n = await backfill_bars(days)
                log.info("backfill_bars_done", rows=n)
                record_backfill_result("bars", n)
            elif kind == "sectors":
                sm = await backfill_sectors()
                log.info("backfill_sectors_done", mapped=sm)
                record_backfill_result("sectors", sm)
            elif kind == "filings":
                f = await backfill_filings()
                log.info("backfill_filings_done", rows=f)
                record_backfill_result("filings", f)
            elif kind == "fundamentals":
                m = await backfill_fundamentals()
                log.info("backfill_fundamentals_done", rows=m)
                record_backfill_result("fundamentals", m)
            elif kind == "earnings":
                e = await backfill_earnings()
                log.info("backfill_earnings_done", rows=e)
                record_backfill_result("earnings", e)
            elif kind == "names":
                from src.backfill import backfill_company_names

                nm = await backfill_company_names()
                log.info("backfill_names_done", rows=nm)
                record_backfill_result("names", nm)
        except Exception as exc:  # noqa: BLE001 - logged, never crashes the API
            record_backfill_error(str(exc))
            record_backfill_result(kind, 0, error=str(exc))
            log.exception("backfill_failed", kind=kind, error=str(exc))


def demo_is_working() -> bool:
    """Whether /api/demo would answer -- the account resolves, not merely that
    DEMO_API_KEY is present."""
    from src import demo

    try:
        return demo.account() is not None
    except Exception:  # noqa: BLE001 - a status read must never throw
        return False


def mailer_is_configured() -> bool:
    """Whether outbound mail would actually send, without trying to send."""
    from src import mailer

    try:
        return mailer.is_configured()
    except Exception:  # noqa: BLE001 - a status read must never throw
        return False


def auth_is_enabled() -> bool:
    from src import auth

    try:
        return auth.is_enabled()
    except Exception:  # noqa: BLE001
        return False


@app.get("/status")
def status() -> dict[str, Any]:
    """Row counts so you can watch the backfill fill up and confirm readiness."""
    from sqlalchemy import func

    from src import demo
    from src.backfill import get_backfill_state
    from src.storage.models import UniverseSnapshot

    out: dict[str, Any] = {"backfill_running": _backfill_lock.locked()}
    out["backfill"] = get_backfill_state()
    # Whether the optional switches this deployment reads are actually reaching
    # the process. Booleans only -- never a value, and nothing here that is not
    # already inferable from a 503 on the endpoint it gates. It exists because
    # "I set that variable" and "the running container can see that variable"
    # are different claims, and telling them apart otherwise needs the admin
    # secret to reach /admin.
    out["features"] = {
        # Not `is_enabled()`: that only says the variable arrived. This says
        # the endpoint would actually answer, which is the question being
        # asked -- the two came apart once already, and a flag reading True
        # next to a 503 is worse than no flag.
        "demo": demo_is_working(),
        "demo_key_set": demo.is_enabled(),
        "email": mailer_is_configured(),
        "login": auth_is_enabled(),
    }
    # What is keeping the data current, and what it is waiting on. Cheap (DB
    # reads only), and the first thing to look at when a number looks old.
    out["auto_update"] = scheduler.report()
    try:
        with session_scope() as session:
            out["price_bars"] = session.execute(
                select(func.count()).select_from(DailyBar)
            ).scalar_one()
            out["distinct_tickers_with_bars"] = session.execute(
                select(func.count(func.distinct(DailyBar.ticker)))
            ).scalar_one()
            latest_bar = session.execute(select(func.max(DailyBar.date))).scalar_one()
            out["latest_bar_date"] = latest_bar.isoformat() if latest_bar else None
            out["bar_dates"] = session.execute(
                select(func.count(func.distinct(DailyBar.date)))
            ).scalar_one()
            out["universe_snapshots"] = session.execute(
                select(func.count(func.distinct(UniverseSnapshot.as_of_date)))
            ).scalar_one()
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out


# ---------------------------------------------------------------------------
# /admin -- the one place to look when something breaks. A phone-first page that
# aggregates every check into a single screen (and a single copy-to-clipboard
# blob). All the assembly below is CHEAP (DB reads + in-memory state): the
# network-backed check (reconcile) is shown from cache so the page can refresh
# without making calls or burning a rate limit.
# ---------------------------------------------------------------------------
def _config_report() -> list[dict[str, Any]]:
    """Every env var as present / missing / invalid -- never the value itself."""
    s = get_settings()

    def opt(name: str, present: bool, note: str) -> dict[str, Any]:
        return {"name": name, "status": "present" if present else "missing", "note": note}

    rows: list[dict[str, Any]] = [
        {"name": "DATABASE_URL",
         "status": "present" if s.database_url else "missing",
         "note": "postgres" if not s.is_sqlite else "sqlite (dev/local)"},
    ]
    ua_ok = bool(s.sec_user_agent) and "@" in s.sec_user_agent \
        and "example.com" not in s.sec_user_agent
    rows.append({
        "name": "SEC_USER_AGENT",
        "status": "present" if ua_ok else ("invalid" if s.sec_user_agent else "missing"),
        "note": "ok" if ua_ok else "needs a real contact email — SEC blocks blank/default UAs",
    })
    rows.append(opt("POLYGON_API_KEY", bool(s.polygon_api_key), "optional — bars + reference"))
    rows.append({
        "name": "POLYGON_TIER", "status": "info",
        "note": f"{s.polygon_tier} — "
        + ("bars come from Stooq bulk; Polygon capped at "
           f"{s.polygon_free_rate_per_min}/min, not used for backfill"
           if s.polygon_tier == "free"
           else "Polygon allowed for backfill"),
    })
    rows.append(opt("FMP_API_KEY", bool(s.fmp_api_key), "optional — caps, GICS sectors, batch EOD"))
    rows.append(opt("FINNHUB_API_KEY", bool(s.finnhub_api_key), "optional — estimates/earnings"))
    rows.append(opt("FRED_API_KEY", bool(s.fred_api_key), "optional — macro regime tilt"))
    rows.append(opt("REDIS_URL", bool(s.redis_url), "optional — shared rate limits"))
    return rows


def _company_name_coverage() -> dict[str, Any]:
    """Whether search-by-name can work at all.

    Names live in `universe.name`, which is SEC's own company title. When this
    reads zero, name search is off and says so rather than reporting "no
    match" -- the two have different causes and only one is the reader's.
    """
    from sqlalchemy import distinct, func

    from src.company.lookup import names_loaded
    from src.storage.models import Fundamental, UniverseSnapshot

    out: dict[str, Any] = {"with_name": 0, "universe_tickers": 0,
                           "drawable_with_name": 0}
    try:
        with session_scope() as s:
            out["with_name"] = names_loaded()
            out["universe_tickers"] = int(s.execute(
                select(func.count(distinct(UniverseSnapshot.ticker)))
            ).scalar_one() or 0)
            # The number that decides whether search is useful: a name on a
            # company with nothing to draw is not a searchable company.
            out["drawable_with_name"] = int(s.execute(
                select(func.count(distinct(UniverseSnapshot.ticker))).where(
                    UniverseSnapshot.name.isnot(None),
                    UniverseSnapshot.name != "",
                    UniverseSnapshot.ticker.in_(
                        select(Fundamental.ticker).where(
                            Fundamental.metric == "total_assets",
                            Fundamental.value > 0,
                        )
                    ),
                )
            ).scalar_one() or 0)
    except Exception as exc:  # noqa: BLE001 - /admin must still render
        out["error"] = str(exc)[:200]
    return out


def _admin_data_health(session: Any) -> dict[str, Any]:
    from sqlalchemy import func

    from src.backfill import get_backfill_state

    s = get_settings()
    bf = get_backfill_state()
    errors: dict[str, str] = {}

    price_bars = session.execute(select(func.count()).select_from(DailyBar)).scalar_one()
    tickers = session.execute(
        select(func.count(func.distinct(DailyBar.ticker)))
    ).scalar_one()
    latest = session.execute(select(func.max(DailyBar.date))).scalar_one()
    bar_dates = session.execute(
        select(func.count(func.distinct(DailyBar.date)))
    ).scalar_one()
    staleness = (dt.date.today() - latest).days if latest else None

    coverage: dict[str, Any] = {
        "tickers_loaded": tickers, "min_for_valid_run": s.min_universe_size,
    }
    try:
        sector_map = repository.sector_map_stats(session)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash on this
        sector_map = {"error": str(exc)[:200]}
    try:
        fundamentals = repository.fundamentals_stats(session)
    except Exception as exc:  # noqa: BLE001
        fundamentals = {"error": str(exc)[:200]}
    adjustment: dict[str, Any] = {"status": "unchecked"}
    rc = _RECONCILE_CACHE["data"]
    if rc:
        sym = rc.get("symbology_sec") or {}
        if isinstance(sym, dict):
            coverage["sec_universe"] = sym.get("sec_tickers")
            coverage["joined_with_sec"] = sym.get("joined")
            coverage["join_rate"] = sym.get("join_rate_vs_sec")
        adj = rc.get("adjustment") or {}
        summary = adj.get("summary") or {}
        checked = adj.get("tested_with_known_splits", adj.get("checked", 0))
        overall = (
            "unadjusted" if summary.get("unadjusted") else
            "adjusted" if (summary.get("adjusted") and not summary.get("inconclusive")) else
            "inconclusive" if checked else "unchecked"
        )
        adjustment = {
            "status": overall, "checked": checked, "summary": summary,
            "verdicts": adj.get("verdicts") or {},
            "details": adj.get("details") or {},
            "unadjusted_names": adj.get("unadjusted_names") or [],
            "control": adj.get("control") or {},
            "splits_source": adj.get("splits_source"),
            "note": adj.get("note"),
        }
        if rc.get("sec_error"):
            errors["sec"] = str(rc["sec_error"])[:200]

    return {
        "backfill": {
            "source": bf.get("source"),
            "sources_available": bf.get("sources_available"),
            "phase": bf.get("phase"),
            "last_error": bf.get("last_error"),
            "polygon_tier": bf.get("polygon_tier") or s.polygon_tier,
            "last_progress_at": bf.get("last_progress_at"),
            "units_done": bf.get("units_done"),
            "units_total": bf.get("units_total"),
            "unit": bf.get("unit"),
            "results": bf.get("results") or {},
        },
        "coverage": coverage,
        "recency": {
            "latest_bar_date": latest.isoformat() if latest else None,
            "staleness_days": staleness,
        },
        "history": {"loaded": bar_dates},
        "adjustment": adjustment,
        "sector_map": sector_map,
        "fundamentals": fundamentals,
        "company_names": _company_name_coverage(),
        "price_bars": price_bars,
        "reconcile_checked_at": _RECONCILE_CACHE["at"],
        "errors": errors,
    }


def _admin_actions() -> dict[str, Any]:
    def rate_reason(gate: _RateGate) -> str | None:
        retry = gate.peek()
        if retry is None:
            return None
        return f"rate limited — try again in ~{int(retry // 60) + 1} min"

    def act(busy: bool, busy_msg: str, rate: str | None) -> dict[str, Any]:
        if busy:
            return {"enabled": False, "reason": busy_msg}
        if rate:
            return {"enabled": False, "reason": rate}
        return {"enabled": True, "reason": None}

    bf_busy = _backfill_lock.locked()
    bf_act = act(bf_busy, "a backfill is already running", rate_reason(_backfill_gate))
    out = {"reconcile": act(False, "", rate_reason(_reconcile_gate))}
    for kind in _BACKFILL_KINDS:
        out[f"backfill_{kind}"] = dict(bf_act)
    out["backfill"] = dict(bf_act)
    # Shares the backfill lock and gate: a reload IS a backfill, plus a wipe.
    out["reload_fundamentals"] = dict(bf_act)
    out["raw_facts"] = dict(bf_act)
    out["verify"] = {"enabled": True, "reason": None}
    out["universe_check"] = {"enabled": True, "reason": None}
    return out


def _admin_verdict(
    health: dict[str, Any], db_ok: bool, auto: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One plain-English line: what's wrong and what to do. Ordered by severity.

    Stale data is only a call to action when nothing is already coming for it.
    With the auto-updater on, "the bars are three days old" is a status, not a
    chore -- so the verdict says what will happen and when, and only asks for a
    tap when the updater is off or has actually failed.
    """
    if not db_ok:
        return {"level": "error", "headline": "Database unreachable",
                "detail": "The service can't read its own data.",
                "action": "Check DATABASE_URL and that Postgres is up."}

    auto = auto or {}
    auto_on = bool(auto.get("enabled"))
    failing = [
        j for j in (auto.get("jobs") or [])
        if (j.get("consecutive_failures") or 0) > 0
    ]

    fu = health.get("fundamentals") or {}
    if isinstance(fu, dict) and not fu.get("error") and (fu.get("rows") or 0) == 0:
        return {
            "level": "warn",
            "headline": "Fundamentals table is empty",
            "detail": "No SEC as-reported facts have been loaded.",
            "action": (
                "The auto-updater will load the quarterly datasets at its next "
                "check; tap Fundamentals to start now."
                if auto_on
                else "Tap Fundamentals to load the quarterly datasets."
            ),
        }

    if failing:
        j = failing[0]
        return {
            "level": "error",
            "headline": f"Auto-update failing: {j.get('name')}",
            "detail": (
                f"{j.get('consecutive_failures')} consecutive failures. "
                f"{j.get('last_error') or ''}"
            ).strip(),
            "action": "It keeps retrying with a growing backoff. Fix the source.",
        }

    adj = health["adjustment"]
    if adj.get("status") == "unadjusted":
        bad = [t for t, v in (adj.get("verdicts") or {}).items() if v == "unadjusted"]
        return {
            "level": "error",
            "headline": "Prices look UNADJUSTED",
            "detail": (f"{', '.join(bad[:5])} did not adjust across a known split. "
                       "Splits read as crashes and split names look deleted."),
            "action": "Fix the price source (or set POLYGON_TIER=paid) and re-backfill.",
        }

    bf = health["backfill"]
    if bf.get("phase") == "error":
        return {
            "level": "error",
            "headline": f"Backfill blocked: {bf.get('source') or 'all sources'}",
            "detail": bf.get("last_error") or "The last backfill failed.",
            "action": "Tap Backfill to retry, or check the source is reachable.",
        }

    st = health["recency"].get("staleness_days")
    if st is not None and st > 5:
        bars = next(
            (j for j in (auto.get("jobs") or []) if j.get("name") == "bars"), {}
        )
        if auto_on:
            return {
                "level": "warn",
                "headline": f"Price data is {st} days stale",
                "detail": (
                    "The auto-updater is on and has this job queued: "
                    f"{bars.get('now') or 'bars are behind'}."
                ),
                "action": "Nothing to do — it refreshes itself. Tap Backfill to hurry it.",
            }
        return {"level": "warn", "headline": f"Price data is {st} days stale",
                "detail": "Bars have not refreshed recently, and AUTO_UPDATE is off.",
                "action": "Tap Backfill to refresh the bars, or set AUTO_UPDATE=on."}

    if adj.get("status") == "unchecked":
        return {"level": "ok", "headline": "Everything working",
                "detail": "Data is fresh. Price adjustment not yet verified this session.",
                "action": "Tap Check data health to confirm splits are adjusted."}
    return {"level": "ok", "headline": "Everything working",
            "detail": "Data is fresh, prices are adjusted, and the config resolves.",
            "action": None}


@app.get("/admin.json")
def admin_json() -> dict[str, Any]:
    """Everything the /admin page renders, in one cheap payload."""
    from src.backfill import (
        get_extraction_reports,
        get_frames_reports,
        get_raw_facts_state,
        get_reload_state,
    )
    from src.analytics import summary as visit_summary
    from src.ingest.sec_cache import cache_status
    from src.logging_config import get_recent_logs

    db_ok = True
    health: dict[str, Any]
    try:
        with session_scope() as session:
            health = _admin_data_health(session)
    except Exception as exc:  # noqa: BLE001 - say so on the page, never blank
        db_ok = False
        health = {
            "error": str(exc)[:200],
            "backfill": {}, "coverage": {}, "recency": {},
            "history": {"loaded": 0},
            "adjustment": {"status": "unchecked"}, "errors": {"database": str(exc)[:200]},
        }

    auto = scheduler.report()
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "verdict": _admin_verdict(health, db_ok, auto),
        "data_health": health,
        "config": _config_report(),
        "logs": get_recent_logs(limit=200),
        "actions": _admin_actions(),
        "extraction": get_extraction_reports(),
        "reload": get_reload_state(),
        "sec_cache": cache_status(),
        "ask": ask_status(),
        "visitors": visit_summary(),
        "raw_facts": get_raw_facts_state(),
        "auto_update": auto,
        "frames": get_frames_reports(),
        "backfill_running": _backfill_lock.locked(),
    }


_ADMIN_PAGE = Path(__file__).parent / "report" / "templates" / "admin.html"


def _asset_version() -> str:
    """Newest mtime across the static assets, as a cache-busting stamp.

    Without this a browser can keep serving a cached admin.js after a deploy,
    so a shipped fix looks like a fix that did not work -- and the next hour
    goes into debugging code that is not the code running. Cheap insurance.
    """
    try:
        return str(int(max(
            f.stat().st_mtime for f in _STATIC_DIR.iterdir() if f.is_file()
        )))
    except (OSError, ValueError):
        return "0"


def _versioned(html: str) -> str:
    v = _asset_version()
    return (html.replace("/static/admin.css", f"/static/admin.css?v={v}")
                .replace("/static/admin.js", f"/static/admin.js?v={v}"))


@app.get("/admin", response_class=HTMLResponse)
def admin_page() -> HTMLResponse:
    """The break-glass page: verdict, data health, config, logs, and action
    buttons -- everything on one phone screen, with one-tap copy."""
    try:
        return HTMLResponse(_versioned(_ADMIN_PAGE.read_text(encoding="utf-8")))
    except OSError:
        return HTMLResponse(
            "<h1>Admin</h1><p>See <a href='/admin.json'>/admin.json</a>.</p>"
        )


@app.get("/admin/balance-sheet")
def admin_balance_sheet(
    tickers: str = Query("JPM,AAL,MSFT,WMT,FCX"),
) -> dict[str, Any]:
    """Verify the extraction against production data.

    Per ticker: every balance-sheet concept with its value, period end, filing
    date and whether it is missing; whether assets equal liabilities plus equity
    and by how much it misses. Across the table: per-concept coverage, and how
    many tickers carry enough to render a balance sheet.
    """
    from sqlalchemy import func, text

    from src.company.balancesheet import BALANCE_SHEET_CONCEPTS, get_balance_sheet
    from src.storage.models import Fundamental

    try:
        as_of = dt.date.today()
        requested = [t.strip().upper() for t in tickers.split(",") if t.strip()]

        company_sheets: dict[str, Any] = {}
        for ticker in requested:
            bs = get_balance_sheet(ticker, as_of=as_of)
            if bs is None:
                company_sheets[ticker] = {"found": False}
                continue

            def val(group: dict[str, Any], name: str) -> float | None:
                v = group.get(name)
                if v is None or v.missing or v.value is None:
                    return None
                return v.value

            total_assets = val(bs.assets, "total_assets")
            total_equity = val(bs.equity, "shareholders_equity")
            # The identity balances against TOTAL equity incl. noncontrolling
            # interests where reported; `Assets` is consolidated and the
            # parent-only figure is not.
            equity_incl_nci = val(bs.equity, "total_equity_incl_nci")
            equity_for_identity = (
                equity_incl_nci if equity_incl_nci is not None else total_equity
            )
            total_liabilities = val(bs.liabilities, "total_liabilities")
            current_liabilities = val(bs.liabilities, "current_liabilities")
            long_term_debt = val(bs.liabilities, "long_term_debt")

            # Prefer reported total liabilities. Falling back to a sum of the
            # parts would invent a total the filing never stated, so when it is
            # absent the identity is simply not checkable.
            liab_for_identity = total_liabilities
            identity_basis = "total_liabilities"

            check: dict[str, Any] = {
                "total_assets": total_assets,
                "total_liabilities": total_liabilities,
                "current_liabilities": current_liabilities,
                "long_term_debt": long_term_debt,
                "total_equity": total_equity,
                "total_equity_incl_nci": equity_incl_nci,
                "equity_basis": (
                    "total_equity_incl_nci" if equity_incl_nci is not None
                    else "total_equity"
                ),
                "basis": identity_basis,
                "diff_pct": None,
                "balanced": False,
                "error": None,
            }
            if total_assets is None:
                check["error"] = "total_assets missing — cannot check the identity"
            elif total_assets <= 0:
                check["error"] = (
                    f"total_assets is {total_assets:,.0f}; a balance sheet cannot "
                    "have zero or negative total assets"
                )
            elif liab_for_identity is None or equity_for_identity is None:
                missing_side = (
                    "total_liabilities" if liab_for_identity is None else "total_equity"
                )
                check["error"] = f"{missing_side} missing — cannot check the identity"
            else:
                rhs = liab_for_identity + equity_for_identity
                diff = abs(total_assets - rhs) / total_assets * 100
                check["liabilities_plus_equity"] = rhs
                check["diff_pct"] = round(diff, 2)
                check["balanced"] = diff < 1.0

            def dump(group: dict[str, Any]) -> dict[str, Any]:
                return {
                    name: {
                        "value": v.value,
                        "period_end": v.period_end.isoformat() if v.period_end else None,
                        "filing_date": v.filing_date.isoformat() if v.filing_date else None,
                        "missing": v.missing,
                        "restated": v.restated,
                    }
                    for name, v in group.items()
                }

            company_sheets[ticker] = {
                "found": True,
                "company_name": bs.company_name,
                "period_end": bs.period_end.isoformat() if bs.period_end else None,
                "filing_date": bs.filing_date.isoformat() if bs.filing_date else None,
                "assets": dump(bs.assets),
                "liabilities": dump(bs.liabilities),
                "equity": dump(bs.equity),
                "balance_check": check,
                "missing_concepts": bs.missing_concepts,
                "data_quality_issues": bs.data_quality_issues,
            }

        with session_scope() as session:
            universe_total = session.execute(
                select(func.count(func.distinct(Fundamental.ticker)))
            ).scalar_one() or 0

            rows = session.execute(
                select(Fundamental.metric, func.count(func.distinct(Fundamental.ticker)))
                .group_by(Fundamental.metric)
            ).all()
            have = {metric: n for metric, n in rows}

            coverage = {}
            for concept, metric in sorted(BALANCE_SHEET_CONCEPTS.items()):
                n = have.get(metric, 0)
                coverage[concept] = {
                    "metric": metric,
                    "tickers_with_data": n,
                    "coverage_pct": round(100.0 * n / universe_total, 1)
                    if universe_total else 0.0,
                }

            renderable = session.execute(
                text(
                    "SELECT COUNT(*) FROM ("
                    "  SELECT ticker FROM fundamentals"
                    "  WHERE metric IN ('total_assets','total_equity')"
                    "  GROUP BY ticker HAVING COUNT(DISTINCT metric) = 2"
                    ") t"
                )
            ).scalar() or 0

        return {
            "as_of": str(as_of),
            "requested_tickers": requested,
            "company_sheets": company_sheets,
            "coverage": {
                "by_concept": coverage,
                "tickers_renderable": renderable,
                "tickers_with_any_fundamentals": universe_total,
            },
        }

    except Exception as exc:  # noqa: BLE001 - show the full error, never a blank page
        import traceback

        return {"error": str(exc)[:300], "traceback": traceback.format_exc()[:2000]}


@app.post("/admin/reload-fundamentals", response_model=RunResponse)
async def admin_reload_fundamentals(
    background: BackgroundTasks,
    confirm: bool = Query(
        False,
        description="Must be true. Guards an unauthenticated, irreversible wipe.",
    ),
    quarters: int = Query(7, ge=1, le=20),
) -> RunResponse:
    """Replace the fundamentals table through the rebuilt extractor.

    Downloads every quarter first and aborts untouched if any of them cannot be
    fetched; the delete and the reload then run in one transaction, so the table
    is either fully replaced or exactly as it was.

    Runs in the background; poll `/admin.json` -> `reload` for progress, or
    `/admin` for the rendered version. On completion the payload carries the
    per-quarter extraction report and the five-company verification.

    `confirm=true` is still required -- the successful path does replace every
    row, and this endpoint is open.
    """
    if not confirm:
        raise HTTPException(
            400,
            "Refusing to replace the fundamentals table without confirm=true.",
        )
    _enforce_rate(_backfill_gate, "backfills")
    if _backfill_lock.locked():
        return RunResponse(
            accepted=False, detail="A backfill or reload is already running."
        )
    background.add_task(_reload_bg, quarters)
    return RunResponse(
        accepted=True,
        detail=f"Reload queued: download {quarters} quarters, then replace and "
               "verify. Nothing is deleted until every quarter is on disk. "
               "Watch /admin for progress.",
    )


async def _reload_bg(quarters: int) -> None:
    from src.backfill import record_backfill_result, reload_fundamentals

    async with _backfill_lock:
        try:
            out = await reload_fundamentals(quarters=quarters)
            record_backfill_result("fundamentals", out["rows_written"])
        except Exception as exc:  # noqa: BLE001 - state carries it to /admin
            record_backfill_result("fundamentals", 0, error=str(exc))
            log.exception("admin_reload_failed", error=str(exc))


@app.get("/company/{ticker}", response_class=HTMLResponse)
def company_page(ticker: str) -> HTMLResponse:
    """One ticker in, one page out. Two database reads, nothing that can hang."""
    from src.company.view1 import build_view1
    from src.company.view2 import build_view2
    from src.company.view3 import build_view3
    from src.report.company_page import render_company_page, render_not_found

    symbol = _clean_ticker(ticker)
    if not _is_ticker_shaped(symbol):
        return HTMLResponse(
            render_not_found(symbol, "That does not look like a ticker symbol."),
            status_code=404,
        )
    try:
        view = build_view1(symbol)
    except Exception as exc:  # noqa: BLE001 - a broken page must still say why
        log.exception("company_page_failed", ticker=symbol, error=str(exc))
        return HTMLResponse(
            render_not_found(symbol, f"Something went wrong reading it: {exc}"),
            status_code=500,
        )
    if view is None:
        return HTMLResponse(
            render_not_found(
                symbol,
                "There are no filed fundamentals for it in the database, or it "
                "reports no total for assets — so there is nothing to draw to scale.",
            ),
            status_code=404,
        )

    # Views 3 and 2 are independent of view 1 and of each other: a company whose
    # income statement is incomplete still gets its balance sheet drawn. Each
    # returns None rather than a partial picture, and a None is simply absent.
    try:
        flow = build_view3(symbol)
    except Exception as exc:  # noqa: BLE001 - one view must not take the page
        log.warning("view3_failed", ticker=symbol, error=str(exc)[:200])
        flow = None
    scale = None
    if flow is not None:
        try:
            scale = build_view2(
                symbol, flow.revenue, flow.period_basis, flow.period_end,
                company_name=view.company_name,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("view2_failed", ticker=symbol, error=str(exc)[:200])
    return HTMLResponse(
        render_company_page(
            view, flow=flow, scale=scale, ask_available=_ASK_MODEL is not None
        )
    )


class AskRequest(BaseModel):
    question: str


@app.post("/company/{ticker}/ask")
async def company_ask(ticker: str, body: AskRequest, request: Request) -> JSONResponse:
    """Answer one question about one company's filed figures.

    The model is sent that company's numbers and nothing else -- the same
    figures already drawn on the page. It never sees a raw filing, another
    company, or a price, so it has nothing to compare, rank or forecast with,
    which is the first of the two layers stopping it from doing so.

    Public endpoint with a paid key behind it, so the caps are checked against
    persisted usage before the upstream call, never after.
    """
    from src.company.view1 import build_view1
    from src.llm.ask import RateLimited, answer_question, hash_ip

    s = get_settings()
    if _ASK_MODEL is None:
        # Say which half is broken. "Unavailable" alone sends the operator
        # looking in the wrong place.
        raise HTTPException(
            503,
            _ASK_MODEL_ERROR or "The question box is not configured.",
        )

    question = body.question.strip()
    if not question:
        raise HTTPException(400, "Ask a question first.")
    if len(question) > s.llm_ask_max_question_chars:
        raise HTTPException(
            400,
            f"Questions are capped at {s.llm_ask_max_question_chars} characters.",
        )

    symbol = _clean_ticker(ticker)
    if not _is_ticker_shaped(symbol):
        raise HTTPException(404, "That does not look like a ticker symbol.")
    view = await asyncio.to_thread(build_view1, symbol)
    if view is None:
        raise HTTPException(404, f"There are no filed figures for {symbol}.")

    client_host = request.client.host if request.client else "unknown"
    ip_hash = hash_ip(client_host)
    try:
        answer = await answer_question(_ASK_MODEL, view, question, ip_hash)
    except RateLimited as exc:
        raise HTTPException(
            429, str(exc), headers={"Retry-After": str(exc.retry_after_s)}
        ) from exc
    except Exception as exc:  # noqa: BLE001 - already recorded as a failed row
        log.warning("company_ask_failed", ticker=symbol, error=str(exc)[:300])
        raise HTTPException(
            502, "The model did not answer. Try again in a moment."
        ) from exc

    return JSONResponse({
        "answer": answer.text,
        "ticker": symbol,
        "period_end": view.period_end.isoformat(),
        "model": answer.model,
        "prompt_tokens": answer.prompt_tokens,
        "completion_tokens": answer.completion_tokens,
        "cost_usd": round(answer.cost_usd, 6),
    })


@app.get("/admin/universe-check")
def admin_universe_check() -> dict[str, Any]:
    """Run the accounting identity over EVERY ticker, not just the five.

    The five-company check catches a parser reading the wrong fact. This catches
    whether the parse is right across the whole file: the drift distribution,
    the worst offenders by name and number, period-on-period scale errors that
    no per-period check can see, and a per-sector breakdown -- because a failure
    concentrated in banks or REITs is a structural tag problem for that filer
    class, not noise.

    Read-only; a few seconds of DB work, no network.
    """
    from src.company.universe_check import run_universe_check

    try:
        return run_universe_check()
    except Exception as exc:  # noqa: BLE001 - show the error, never a blank page
        import traceback

        return {"error": str(exc)[:300], "traceback": traceback.format_exc()[:2000]}


@app.post("/admin/raw-facts", response_model=RunResponse)
async def admin_raw_facts(
    background: BackgroundTasks,
    ticker: str = Query("MSFT"),
    year: int = Query(2026, ge=2009, le=2100),
    quarter: int = Query(1, ge=1, le=4),
    tags: str = Query("Assets,StockholdersEquity"),
    ddate: str = Query("", description="YYYYMMDD period end, blank for all"),
    cik: str = Query("", description="override the CIK lookup"),
) -> RunResponse:
    """Dump raw num.txt rows for one company, to settle a disputed figure.

    This is the check that resolved the JPM bug, as an endpoint. It shows every
    column of every row carrying a tag, which columns differ across them, and
    what the consolidated-instant filter selects. A reference figure is only
    ever corrected against this -- never against the parser's own output.

    Downloads a ~100MB ZIP, so it runs in the background; poll `/admin.json` ->
    `raw_facts`.
    """
    tag_tuple = tuple(t.strip() for t in tags.split(",") if t.strip())
    if not tag_tuple:
        raise HTTPException(400, "tags must name at least one XBRL tag")
    _enforce_rate(_backfill_gate, "backfills")
    if _backfill_lock.locked():
        return RunResponse(
            accepted=False, detail="A backfill or reload is already running."
        )
    background.add_task(
        _raw_facts_bg, ticker, year, quarter, tag_tuple, ddate or None, cik or None
    )
    return RunResponse(
        accepted=True,
        detail=f"Dumping {ticker} {list(tag_tuple)} from {year}q{quarter}. "
               "Watch /admin for the result.",
    )


async def _raw_facts_bg(
    ticker: str, year: int, quarter: int,
    tags: tuple[str, ...], ddate: str | None, cik: str | None,
) -> None:
    from src.backfill import run_raw_facts_dump

    async with _backfill_lock:
        try:
            await run_raw_facts_dump(ticker, year, quarter, tags, ddate, cik)
        except Exception as exc:  # noqa: BLE001 - state carries it to /admin
            log.exception("admin_raw_facts_failed", error=str(exc))


@app.get("/admin/verify")
def admin_verify() -> dict[str, Any]:
    """Check the five reference companies against their known figures.

    Returns actual vs expected with a pass/fail per company, the A = L + E
    identity where it is checkable, and per-concept coverage across every ticker
    that has any fundamentals. Read-only; safe to hit any time.
    """
    from src.company.verify import concept_coverage, verify_companies

    try:
        result = verify_companies()
        result["coverage"] = concept_coverage()
        return result
    except Exception as exc:  # noqa: BLE001 - show the error, never a blank page
        import traceback

        return {"error": str(exc)[:300], "traceback": traceback.format_exc()[:2000]}


def _clean_ticker(raw: str) -> str:
    """A ticker as typed by a human: spaces, lowercase, a stray $ or a pasted
    '$JPM ' all mean the same symbol."""
    return raw.strip().lstrip("$").strip().upper()[:16]


def _is_ticker_shaped(symbol: str) -> bool:
    """Letters, with dots and dashes allowed for class shares (BRK.B, RDS-A)."""
    if not symbol:
        return False
    return symbol.replace(".", "").replace("-", "").isalnum()


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    """The front door: a sentence, a search box, and five real balance sheets.

    Each thumbnail is built by the same `build_view1` the full page uses, so the
    home page can never advertise a shape the page then contradicts. A ticker
    whose drawing will not build is offered without one -- never with a
    placeholder, which would be a picture of nothing presented as a company.
    """
    from src.company.stats import site_stats
    from src.company.suggest import suggestions
    from src.company.view1 import build_view1
    from src.report.home_page import render_home

    pairs = []
    for s in suggestions():
        try:
            pairs.append((s, build_view1(s.ticker)))
        except Exception as exc:  # noqa: BLE001 - one bad ticker must not take the page
            log.warning("home_thumbnail_failed", ticker=s.ticker, error=str(exc)[:200])
            pairs.append((s, None))
    return HTMLResponse(
        _versioned(
            render_home(pairs, stats=site_stats(), nav=_nav_for(request, "home"))
        )
    )


@app.get("/about")
def about() -> RedirectResponse:
    """The explanation lives on the home page now, below the search.

    Kept as a redirect rather than deleted: it was a real URL for a while, and
    a link that used to work should keep working.
    """
    return RedirectResponse(url="/#how", status_code=307)


@app.get("/search")
def search(q: str = Query("", max_length=64)) -> Response:
    """A ticker or a company name in, a company out.

    A redirect rather than a rendered result wherever the answer is
    unambiguous: the answer to "JPM" is JPM's page, and a results screen
    between the two would be a page whose only job is to be clicked through.
    Several matches get a list, because then there is a real choice to make.

    An exact ticker always wins. "AAL" is American Airlines, not the closest
    company whose name happens to contain those letters.
    """
    from src.company.lookup import resolve
    from src.company.suggest import suggestions

    raw = q.strip()
    if not raw:
        from src.report.home_page import render_search_empty

        return HTMLResponse(render_search_empty(suggestions()), status_code=400)

    found = resolve(raw)
    if found.kind in ("ticker", "one"):
        return RedirectResponse(url=f"/company/{found.ticker}", status_code=303)
    if found.kind == "many":
        from src.report.home_page import render_matches

        return HTMLResponse(render_matches(raw, found.matches, suggestions()))
    if found.kind == "fuzzy":
        # Suggested, never followed: resolving a misspelling automatically puts
        # a company on screen that nobody asked for, under a heading that reads
        # as the site asserting it is the right one. 404, because nothing
        # actually matched what was typed.
        from src.report.home_page import render_matches

        return HTMLResponse(
            render_matches(raw, found.matches, suggestions(), did_you_mean=True),
            status_code=404,
        )
    # Nothing resolved. A ticker-shaped query still goes to /company/{SYMBOL}:
    # that is the canonical, shareable URL for a company, and it is the one
    # page that can say what is missing about that specific symbol.
    #
    # But only a SHORT one. "Walmart" is alphanumeric and so passed this test,
    # which sent somebody who typed a company name to /company/WALMART -- a
    # page about a symbol that does not exist and never will, presented as
    # though the site had understood them. Real tickers are one to five
    # characters (plus a class suffix); anything longer that resolved to
    # nothing is a name that did not match, and gets told so.
    symbol = _clean_ticker(raw)
    if _is_ticker_shaped(symbol) and len(symbol.replace(".", "").replace("-", "")) <= 5:
        return RedirectResponse(url=f"/company/{symbol}", status_code=303)

    if found.kind == "no_names":
        from src.report.home_page import render_no_names

        return HTMLResponse(render_no_names(raw, suggestions()), status_code=404)

    from src.report.company_page import render_not_found

    return HTMLResponse(
        render_not_found(
            raw[:40], f"No companies found matching “{raw[:40]}”."
        ),
        status_code=404,
    )


# ---------------------------------------------------------------------------
# The paid API: register, call, check, download -- and the manual grant switch
# ---------------------------------------------------------------------------

# Imported at module level, unlike the rest of this file's lazy imports: a
# `Depends(...)` is evaluated when the decorator runs, so the dependency has to
# be a real function object here and not a name looked up later.
from src.accounts import get_current_user as _ACCOUNT_DEP  # noqa: E402
from src.accounts import get_current_user_flexible as _DOWNLOAD_DEP  # noqa: E402


class RegisterRequest(BaseModel):
    email: str
    accept_terms: bool = False


class GrantRequest(BaseModel):
    email: str
    action: str


def _stamp_terms_quietly(email: str) -> None:
    from src import auth

    try:
        auth.stamp_terms(email)
    except Exception as exc:  # noqa: BLE001 - the account exists either way
        log.warning("terms_stamp_failed", error=str(exc)[:120])


def _require_terms(accepted: bool) -> None:
    """Refuse to create an account without acceptance.

    Enforced on the server because a checkbox is otherwise decoration: the
    endpoints are public JSON and anybody can post to them without ever having
    seen the form. The point of asking is to be able to say afterwards that it
    was asked and answered.
    """
    if not accepted:
        raise HTTPException(
            status_code=422,
            detail=(
                "Please accept the Terms of Service and Privacy Policy to "
                "create an account."
            ),
        )


def _caller_ip_hash(request: Request) -> str:
    from src import analytics

    return analytics.hash_ip(
        analytics.client_ip(
            request.headers, request.client.host if request.client else "unknown"
        )
    )


@app.post("/api/auth/register", status_code=201)
def api_register(body: RegisterRequest) -> JSONResponse:
    """One email in, one API key out. Free tier, no payment, no confirmation.

    An address that already has a key gets 409 and NOT the key. Handing it back
    would make this open endpoint a lookup service: type a customer's address,
    receive their paid key.
    """
    from src import accounts

    # Before the write, and before the validity check, so a loop cannot probe
    # this endpoint for free by sending addresses it knows will be rejected.
    _enforce_rate(_register_gate, "registration")

    _require_terms(body.accept_terms)
    address = accounts.normalise_email(body.email)
    if not accounts.valid_email(address):
        raise HTTPException(
            status_code=422, detail="That does not look like an email address."
        )
    try:
        account = accounts.register(address)
    except accounts.EmailTaken:
        raise HTTPException(
            status_code=409,
            detail=(
                "That address is already registered. Keys are not re-sent from "
                "here -- contact the site owner if you have lost yours."
            ),
        ) from None
    _stamp_terms_quietly(address)
    log.info("api_user_registered", email=address)
    return JSONResponse(
        status_code=201,
        content={
            "api_key": account.api_key,
            "email": account.email,
            "tier": account.tier,
            "calls_limit": account.call_limit,
            "note": "Send this key as an X-API-Key header. Keep it: it is not re-issued.",
        },
    )


class ResendRequest(BaseModel):
    email: str


@app.post("/api/auth/resend-key")
def api_resend_key(body: ResendRequest, tasks: BackgroundTasks) -> JSONResponse:
    """Mail somebody the key they already have. Never shows it in the response.

    This is the counterpart to register's 409, and it is safe for the same
    reason that refusal is: the key travels to the REGISTERED inbox and nowhere
    else, so typing a stranger's address sends mail to the stranger and teaches
    the sender nothing.

    The reply is identical whether or not the address is registered. Anything
    else turns this into an account-enumeration oracle, and it would be a poor
    trade to close that door on `/register` and open it here.

    Sending happens in a background task: a relay that hangs for its full
    ten-second timeout must not be a ten-second request.
    """
    from src import accounts, mailer

    _enforce_rate(_resend_gate, "key recovery")

    address = accounts.normalise_email(body.email)
    configured = mailer.is_configured()
    s = get_settings()
    where = s.admin_email or "the site owner"

    if not configured:
        # Told plainly rather than pretending to send. A recovery flow that
        # accepts the request and drops it leaves somebody waiting on an email
        # that was never going to arrive.
        return JSONResponse(
            status_code=503,
            content={
                "sent": False,
                "detail": f"Email is not configured. Contact {where} to recover your key.",
            },
        )

    # Uniform response, computed before any branch on whether the user exists.
    ok = {
        "sent": True,
        "detail": (
            "If that address has a key, it has just been emailed. Check spam."
        ),
    }

    if accounts.valid_email(address):
        cooling = accounts.resend_cooldown_remaining(address)
        if cooling:
            # Still the uniform answer: a distinct "wait" reply for registered
            # addresses only would leak exactly what the uniform reply hides.
            log.info("resend_on_cooldown", seconds_left=cooling)
            return JSONResponse(ok)
        account = accounts.by_email(address)
        if account is not None:
            accounts.mark_resent(address)
            tasks.add_task(mailer.send_api_key, account.email, account.api_key)
            log.info("resend_queued", email=address)
        else:
            log.info("resend_unknown_address", email=address)
    return JSONResponse(ok)


class MagicLinkRequest(BaseModel):
    email: str
    accept_terms: bool = False


class VerifyRequest(BaseModel):
    token: str


@app.post("/api/auth/magic-link")
def api_magic_link(body: MagicLinkRequest, tasks: BackgroundTasks) -> JSONResponse:
    """Email a one-time login link. Same reply whatever the address.

    Issued for addresses that have never registered too -- verifying the link
    creates the account. The alternative is "register, be told 409, then ask
    for a link", which is three steps to do what one email already does.
    """
    from src import accounts, auth, demo, mailer

    if not auth.is_enabled():
        raise auth.LoginDisabled()
    _enforce_rate(_magic_link_gate, "login links")

    address = accounts.normalise_email(body.email)
    ok = JSONResponse(
        {"message": "If that email exists, a magic link has been sent"}
    )
    if not mailer.is_configured():
        where = get_settings().admin_email or "the site owner"
        return JSONResponse(
            status_code=503,
            content={
                "message": f"Email is not configured. Contact {where} to sign in."
            },
        )
    # The shared demo account is not a person and has no inbox. Barring it here
    # stops the row being pried loose via a login and then regenerate-key.
    if address == demo.DEMO_EMAIL or not accounts.valid_email(address):
        return ok

    token = auth.create_link(address, accept_terms=body.accept_terms)
    if token is None:
        log.info("magic_link_on_cooldown")  # uniform reply regardless
        return ok
    tasks.add_task(
        mailer.send_magic_link,
        address,
        auth.link_url(token),
        get_settings().magic_link_ttl_s // 60,
    )
    return ok


@app.post("/api/auth/verify")
def api_verify(body: VerifyRequest) -> JSONResponse:
    """Spend a login token and set the session cookie.

    POST, not GET, and that is the whole point. Mail scanners and link
    prefetchers issue a GET on every URL in a message, so a GET that consumed
    the token would mean the user's own click always arrived second, to a link
    that had already been used. The emailed link lands on a page (GET
    /auth/verify) that only LOOKS at the token; this consumes it.
    """
    from src import auth

    if not auth.is_enabled():
        raise auth.LoginDisabled()

    token = body.token.strip()
    accepted = auth.token_accepted_terms(token)
    email = auth.consume_token(token)
    if email is None:
        raise HTTPException(
            status_code=401, detail="Invalid or expired magic link"
        )
    account = auth.account_for_login(email, accepted_terms=accepted)
    response = JSONResponse({"ok": True, "email": account.email})
    auth.issue_session(response, account.email)
    log.info("session_started", email=account.email)
    return response


@app.get("/api/auth/me")
def api_me(request: Request) -> JSONResponse:
    """Who is signed in, and their key. Cookie-gated, never key-gated.

    Returning the API key here is what lets the dashboard show it instead of
    asking for a paste -- which also means this cookie IS the key, hence Secure
    and HttpOnly on it.
    """
    from src import accounts, auth

    account = auth.require_account(request)
    payload = accounts.status_payload(account)
    payload["api_key"] = account.api_key
    payload["signed_in"] = True
    return JSONResponse(payload)


@app.post("/api/auth/logout")
def api_logout() -> JSONResponse:
    from src import auth

    response = JSONResponse({"message": "Logged out"})
    auth.clear_session(response)
    return response


@app.post("/api/auth/regenerate-key")
def api_regenerate_key(request: Request) -> JSONResponse:
    """Replace the signed-in account's API key. Session auth only.

    Deliberately not reachable with the API key itself: the reason to press
    this is that the key has leaked, and a leaked key that can rotate itself
    lets whoever holds it lock the owner out.
    """
    from src import auth

    account = auth.require_account(request)
    new_key = auth.regenerate_key(account.email)
    return JSONResponse({"api_key": new_key})


class PasswordLogin(BaseModel):
    email: str
    password: str
    # Only read where an ACCOUNT IS CREATED. Sign-in does not re-ask: you
    # accepted when you signed up, and a login form that demands it again is
    # asking for consent it already has.
    accept_terms: bool = False


class PasswordOnly(BaseModel):
    password: str


class PasswordChange(BaseModel):
    current_password: str = ""
    new_password: str


def _uniform_login_failure() -> HTTPException:
    """One reply for every way a password login can fail.

    Wrong address, wrong password, and an account that only uses magic links
    all land here. Telling them apart -- "no password set for this account" --
    would confirm the address is registered, which is exactly what the uniform
    replies on /register and /magic-link exist to prevent.

    So the message covers the third case instead of detecting it. Somebody who
    signed up with a link and has never set a password reads this and knows
    exactly what to do; somebody probing for registered addresses learns
    nothing, because everybody gets the same sentence.
    """
    return HTTPException(
        status_code=401,
        detail=(
            "Incorrect email or password. If you signed up with a magic link "
            "you may not have set a password yet — sign in with a link and "
            "set one from the Account tab."
        ),
    )


@app.post("/api/auth/login")
def api_password_login(body: PasswordLogin) -> JSONResponse:
    """Sign in with a password. Same cookie a magic link would have set."""
    from src import auth

    if not auth.is_enabled():
        raise auth.LoginDisabled()

    email = body.email.strip()
    if auth.login_attempts_remaining(email) <= 0:
        # Counted per ADDRESS: stuffing one account comes from many IPs, and an
        # IP cap would lock out everyone behind a single office connection.
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again in an hour, or use a magic link.",
        )

    account = auth.authenticate(email, body.password)
    if account is None:
        auth.record_failed_login(email)
        log.info("password_login_failed", email=accounts_norm(email))
        raise _uniform_login_failure()

    auth.reset_login_attempts(email)
    response = JSONResponse({"ok": True, "email": account.email})
    auth.issue_session(response, account.email)
    log.info("session_started", email=account.email, via="password")
    return response


def accounts_norm(email: str) -> str:
    from src import accounts

    return accounts.normalise_email(email)


@app.post("/api/auth/register-password", status_code=201)
def api_register_password(body: PasswordLogin) -> JSONResponse:
    """Create an account with a password and sign in immediately.

    409 on an existing address, for the same reason /register gives one: this
    endpoint takes no proof of the address, so letting it SET a password on an
    account that already exists would hand that account to anyone who knew the
    email.
    """
    from src import accounts, auth

    if not auth.is_enabled():
        raise auth.LoginDisabled()
    _enforce_rate(_register_gate, "registration")

    address = accounts.normalise_email(body.email)
    if not accounts.valid_email(address):
        raise HTTPException(
            status_code=422, detail="That does not look like an email address."
        )
    _require_terms(body.accept_terms)
    auth.check_password_shape(body.password)
    try:
        account = accounts.register(address)
    except accounts.EmailTaken:
        raise HTTPException(
            status_code=409,
            detail=(
                "That address already has an account. Sign in, or use "
                "“forgot password” to get a link."
            ),
        ) from None

    auth.set_password(address, body.password)
    auth.stamp_terms(address)
    response = JSONResponse(
        status_code=201,
        content={
            "ok": True,
            "email": account.email,
            "api_key": account.api_key,
        },
    )
    auth.issue_session(response, account.email)
    log.info("account_created_with_password", email=address)
    return response


@app.post("/api/auth/set-password")
def api_set_password(body: PasswordOnly, request: Request) -> JSONResponse:
    """Set a first password. Session only -- the session already proves the
    address, which is the same bar a password reset would clear."""
    from src import auth

    account = auth.require_account(request)
    if account.has_password:
        raise HTTPException(
            status_code=409,
            detail="This account already has a password. Use change-password.",
        )
    auth.set_password(account.email, body.password)
    return JSONResponse({"ok": True, "has_password": True})


@app.post("/api/auth/change-password")
def api_change_password(body: PasswordChange, request: Request) -> JSONResponse:
    """Replace an existing password. Requires the current one.

    Asked for even though the session alone would do, because a session is a
    thing that can be left open on a shared machine and a password change is
    the one action that locks the real owner out.
    """
    from src import auth

    account = auth.require_account(request)
    if not account.has_password:
        raise HTTPException(
            status_code=409,
            detail="This account has no password yet. Use set-password.",
        )
    if auth.authenticate(account.email, body.current_password) is None:
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    auth.set_password(account.email, body.new_password)
    return JSONResponse({"ok": True, "has_password": True})


@app.post("/api/auth/forgot-password")
def api_forgot_password(body: ResendRequest, tasks: BackgroundTasks) -> JSONResponse:
    """Forgotten passwords are recovered with a magic link, not a reset token.

    Deliberately the same machinery as signing in: one token type, one expiry,
    one set of edge cases. Clicking the link signs you in, and a new password is
    set from the Account tab -- so there is no separate reset page that has to
    be secured all over again.
    """
    return api_magic_link(
        MagicLinkRequest(email=body.email, accept_terms=True), tasks
    )


@app.get("/admin/subscriptions")
def admin_subscriptions(
    request: Request,
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
) -> JSONResponse:
    """Who is on Pro, and when each one runs out. Soonest first.

    The list a manual billing system cannot work without: renewals here are a
    thing the operator does, so they have to be a thing the operator can SEE.
    Lapsed subscriptions are included rather than filtered -- one that ran out
    yesterday is the most urgent row on the page, not a row to hide.
    """
    from src import accounts

    accounts.verify_admin_secret(
        x_admin_secret, ip_hash=_caller_ip_hash(request)
    )
    rows = accounts.subscriptions()
    return JSONResponse(
        {
            "count": len(rows),
            "lapsed": sum(1 for r in rows if r["lapsed"]),
            "period_days": get_settings().pro_period_days,
            "reminder_days": get_settings().pro_reminder_days,
            "subscriptions": rows,
        }
    )


@app.get("/api/user/status")
def api_user_status(account=Depends(_ACCOUNT_DEP)) -> JSONResponse:
    """Your own tier, entitlement and usage. Does not spend a call --
    a meter you cannot read without moving it is not a meter."""
    from src import accounts

    return JSONResponse(accounts.status_payload(account))


@app.get("/api/company/{ticker}")
def api_company(ticker: str, account=Depends(_ACCOUNT_DEP)) -> JSONResponse:
    """One company's filed balance sheet, as JSON. Costs one metered call.

    The limit is enforced before the read and the call is recorded after it
    succeeds, so a ticker that does not exist is not billed.
    """
    from dataclasses import asdict

    from src import accounts
    from src.company.balancesheet import get_balance_sheet

    accounts.enforce_monthly_limit(account)

    symbol = _clean_ticker(ticker)
    if not _is_ticker_shaped(symbol):
        raise HTTPException(
            status_code=422, detail="That does not look like a ticker symbol."
        )
    try:
        sheet = get_balance_sheet(symbol)
    except Exception as exc:  # noqa: BLE001 - an API error must still say why
        log.exception("api_company_failed", ticker=symbol, error=str(exc))
        raise HTTPException(
            status_code=500, detail=f"Something went wrong reading it: {exc}"
        ) from exc
    if sheet is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No filed fundamentals for {symbol} in the database, or it "
                "reports no total for assets."
            ),
        )

    accounts.record_call(account, "/api/company")
    payload = jsonable_encoder(asdict(sheet))
    payload["source"] = "SEC Financial Statement Data Sets (as reported)"
    return JSONResponse(payload)


@app.get("/api/demo/{ticker}")
def api_demo(ticker: str, request: Request) -> JSONResponse:
    """The home page's live demo: one company, no key required, five a day.

    Same data and same code path as `/api/company/{ticker}` -- it would be a
    poor demo of an API that returned something the API does not. What differs
    is only how the caller is identified: the demo account's key is attached
    here, server-side, and is never sent to the browser.

    Counted per salted address digest per UTC day. Demo calls are recorded in
    `demo_usage` and NOT in `usage_logs`, so anonymous traffic never appears in
    a paying customer's usage figures -- including the demo account's own.
    """
    from dataclasses import asdict

    from src import demo
    from src.company.balancesheet import get_balance_sheet

    # Resolving the account is what makes the demo "run on a key" rather than
    # merely bypass authentication: no demo account, no demo. It also
    # self-provisions, so the first visitor to a cold container is served
    # instead of meeting a 503 while the background boot catches up.
    if demo.account() is None:
        raise HTTPException(
            status_code=503,
            detail="The demo is not configured on this deployment.",
        )

    ip_hash = _caller_ip_hash(request)
    limit = get_settings().demo_calls_per_ip_per_day
    used = demo.calls_today(ip_hash)
    if limit and used >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Demo limit reached: {used}/{limit} calls today. It resets at "
                "midnight UTC. A free key at /dashboard has its own allowance."
            ),
        )

    symbol = _clean_ticker(ticker)
    if not _is_ticker_shaped(symbol):
        raise HTTPException(
            status_code=422, detail="That does not look like a ticker symbol."
        )
    try:
        sheet = get_balance_sheet(symbol)
    except Exception as exc:  # noqa: BLE001
        log.exception("api_demo_failed", ticker=symbol, error=str(exc))
        raise HTTPException(
            status_code=500, detail=f"Something went wrong reading it: {exc}"
        ) from exc
    if sheet is None:
        # Not counted: a wrong guess at a ticker must not spend one of five.
        raise HTTPException(
            status_code=404,
            detail=(
                f"No filed fundamentals for {symbol} in the database, or it "
                "reports no total for assets."
            ),
        )

    demo.record(ip_hash, symbol)
    payload = jsonable_encoder(asdict(sheet))
    payload["source"] = "SEC Financial Statement Data Sets (as reported)"
    payload["demo"] = {
        "calls_used_today": used + 1,
        "calls_limit": limit,
        "note": "Public demo. Get your own key at /dashboard.",
    }
    return JSONResponse(payload)


# `/download` is the same handler, not a redirect. It is the URL that gets typed
# from memory and pasted into an email, and a request to an unregistered path is
# answered by the router before any dependency runs -- so it returns a bare
# `{"detail":"Not Found"}` that looks exactly like an auth failure and sends you
# looking for a bug in the key handling. Registering both spellings costs one
# line and removes that whole false trail. A 307 would not: it drops the
# X-API-Key header on any client that does not re-send headers across a
# redirect, which would break the curl case to fix the browser one.
@app.get("/download", include_in_schema=False)
@app.get("/api/download-dataset")
def api_download_dataset(account=Depends(_DOWNLOAD_DEP)) -> StreamingResponse:
    """The whole `fundamentals` table as CSV. 402 unless the download is paid.

    Streamed, not buffered: this is over a million rows, and building the file
    in memory before sending a byte would take the container down. The response
    therefore has no Content-Length -- the size is not known until the last row
    is read -- so a browser shows an indeterminate progress bar. That is the
    honest trade for not needing a gigabyte of RAM per buyer.
    """
    from src import accounts, dataset

    accounts.require_paid_download(account)
    log.info("dataset_download_started", email=account.email)
    return StreamingResponse(
        dataset.iter_csv(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{dataset.filename()}"',
            # A partially-written CSV that a proxy cached as complete would be
            # indistinguishable from the real file. Never cache this.
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/admin/grant-access")
def admin_grant_access(
    body: GrantRequest,
    request: Request,
    x_admin_secret: str | None = Header(default=None, alias="X-Admin-Secret"),
) -> JSONResponse:
    """The manual payment switch. Authenticated by ADMIN_SECRET, nothing else.

    This is the entire billing system: a payment notice arrives by email, and
    one curl marks the account paid. Unset ADMIN_SECRET is 503 (fail closed),
    a wrong one is 403 and an audit row.

        curl -X POST https://<host>/admin/grant-access \\
          -H "X-Admin-Secret: $ADMIN_SECRET" \\
          -H "Content-Type: application/json" \\
          -d '{"email":"buyer@example.com","action":"grant_download"}'
    """
    from src import accounts

    ip_hash = _caller_ip_hash(request)
    accounts.verify_admin_secret(x_admin_secret, ip_hash=ip_hash)
    account = accounts.apply_admin_action(body.email, body.action, ip_hash=ip_hash)
    return JSONResponse(
        {
            "ok": True,
            "message": f"{body.action} applied to {account.email}.",
            "user": {
                "email": account.email,
                "tier": account.tier,
                "has_paid_download": account.has_paid_download,
                "calls_limit": account.call_limit,
            },
        }
    )


def _nav_for(request: Request, active: str) -> str:
    """The navigation bar for a server-rendered page, session included.

    Rendered here rather than fetched by script: a bar that says "Sign in" for
    half a second to somebody who is signed in is worse than no bar, and it
    would be wrong permanently with scripting off.
    """
    from src import auth
    from src.report.nav import render_nav

    account = auth.current_account(request)
    return render_nav(
        active=active,
        signed_in=account is not None,
        email=account.email if account else "",
        tier=account.tier if account else "free",
        login_enabled=auth.is_enabled(),
    )


@app.post("/logout")
def logout_form(request: Request) -> RedirectResponse:
    """Log out from the nav bar, with or without JavaScript.

    A form POST rather than only the JSON endpoint, because the nav is on pages
    that do not load dashboard.js and must still work with scripting off.
    """
    from src import auth

    response = RedirectResponse("/", status_code=303)
    auth.clear_session(response)
    return response


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request) -> HTMLResponse:
    from src.report.legal import render_terms

    return HTMLResponse(
        _versioned(
            render_terms(
                nav=_nav_for(request, "terms"),
                contact=get_settings().admin_email,
            )
        )
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request) -> HTMLResponse:
    from src.report.legal import render_privacy

    return HTMLResponse(
        _versioned(
            render_privacy(
                nav=_nav_for(request, "privacy"),
                contact=get_settings().admin_email,
            )
        )
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    from src import auth
    from src.report.auth_pages import render_login

    return HTMLResponse(
        _versioned(
            render_login(
                admin_email=get_settings().admin_email,
                enabled=auth.is_enabled(),
                nav=_nav_for(request, "login"),
            )
        )
    )


@app.get("/auth/verify", response_class=HTMLResponse)
def verify_page(token: str = Query("", max_length=128)) -> HTMLResponse:
    """Where the emailed link lands. Looks at the token; does not spend it.

    Spending it here would hand the user's one click to whichever mail scanner
    fetched the URL first. The page reports whether the token is currently good
    and then POSTs to consume it.
    """
    from src import auth
    from src.report.auth_pages import render_verify

    if not auth.is_enabled():
        return HTMLResponse(
            _versioned(render_verify(token="", state="dead")), status_code=503
        )
    state = "ready" if auth.peek_token(token.strip()) else "dead"
    return HTMLResponse(
        _versioned(render_verify(token=token.strip(), state=state)),
        status_code=200 if state == "ready" else 410,
    )


@app.post("/auth/verify")
async def verify_submit(request: Request) -> Response:
    """The no-JavaScript path: the verify page's form posts here directly.

    Same consume-then-redirect as the JSON endpoint, so the dashboard is
    reachable with scripting switched off -- which the rest of this site
    already manages.
    """
    from urllib.parse import parse_qs

    from src import auth
    from src.report.auth_pages import render_verify

    if not auth.is_enabled():
        raise auth.LoginDisabled()
    # Parsed here rather than with `request.form()`, which asserts on
    # python-multipart being installed even for a urlencoded body. This form has
    # one field and no file upload, so a dependency for it would be a whole
    # package to parse `token=...`.
    raw = (await request.body()).decode("utf-8", "replace")
    token = parse_qs(raw).get("token", [""])[0].strip()
    accepted = auth.token_accepted_terms(token)
    email = auth.consume_token(token)
    if email is None:
        return HTMLResponse(
            _versioned(render_verify(token="", state="dead")), status_code=410
        )
    account = auth.account_for_login(email, accepted_terms=accepted)
    response = RedirectResponse("/dashboard", status_code=303)
    auth.issue_session(response, account.email)
    log.info("session_started", email=account.email, path="form")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """The one page a buyer needs: get a key, see the tier, take the download."""
    from src import auth, dataset
    from src.report.dashboard_page import render_dashboard

    s = get_settings()
    try:
        facts = dataset.row_count()
    except Exception as exc:  # noqa: BLE001 - a missing count must not lose the page
        log.warning("dashboard_count_failed", error=str(exc)[:200])
        facts = None
    return HTMLResponse(
        _versioned(
            render_dashboard(
                admin_email=s.admin_email,
                dataset_price=s.dataset_price_usd,
                pro_price=s.pro_price_usd,
                free_limit=s.free_tier_monthly_calls,
                pro_limit=s.pro_tier_monthly_calls,
                fact_count=facts,
                login_enabled=auth.is_enabled(),
                nav=_nav_for(request, "dashboard"),
            )
        )
    )


def _public_origin(request: Request) -> str:
    """The origin as the READER sees it, which is not what the app sees.

    Railway terminates TLS in front of the container and the start command runs
    uvicorn without --proxy-headers, so `request.url.scheme` is the plain http
    of the internal hop. Rendering that into a curl example gives somebody a
    command that answers with a redirect instead of JSON -- on a page whose
    whole promise is that its examples paste and run. The proxy tells us the
    real scheme; the header is trusted because nothing else can reach this
    process.
    """
    scheme = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if scheme not in ("http", "https"):
        scheme = request.url.scheme
    return f"{scheme}://{request.url.netloc}"


@app.get("/google{token}.html", include_in_schema=False)
def google_site_verification(token: str) -> Response:
    """Google Search Console's HTML-file check.

    Serves ONLY a file that has actually been put in `static/verify/`. The
    obvious shortcut -- echo back "google-site-verification: google{token}.html"
    for whatever token is asked for -- would hand the property to anybody who
    can read this route, because the content Google looks for is derivable from
    the filename it asks for. Ownership has to be proved by somebody with write
    access to the repository, which is the whole point of the check.

    `token` is narrowed to the alphanumeric shape Google issues before it is
    used in a path, so nothing here can be walked out of the directory.
    """
    if not token.isalnum():
        raise HTTPException(status_code=404, detail="Not found")
    path = _STATIC_DIR / "verify" / f"google{token}.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return Response(
        content=path.read_bytes(),
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api", response_class=HTMLResponse)
def api_page(request: Request) -> HTMLResponse:
    """The API reference, as a page.

    /api is in the navigation bar, and a nav link that answers with raw JSON
    asks a reader to parse a payload to find out what the product does. The
    index that used to live here is unchanged and now at /api.json, which is
    where something that wants to read it by machine will look anyway.
    """
    from src.report.api_page import render_api

    s = get_settings()
    return HTMLResponse(
        _versioned(
            render_api(
                nav=_nav_for(request, "api"),
                base_url=_public_origin(request),
                free_calls=s.free_tier_monthly_calls,
                pro_calls=s.pro_tier_monthly_calls,
                dataset_price=f"${s.dataset_price_usd}",
            )
        )
    )


@app.get("/api.json")
def api_index() -> JSONResponse:
    return JSONResponse(
        {
            "service": "To Scale",
            "description": (
                "Filed financial statements, drawn at true proportion."
            ),
            "endpoints": [
                "/", "/search?q=TICKER", "/company/{ticker}",
                "/api  (this index, as a page)", "/api.json",
                "/health", "/status", "/reconcile",
                "/admin", "/admin.json", "/admin/balance-sheet", "/admin/verify",
                "/admin/universe-check",
                "POST /backfill", "POST /admin/reload-fundamentals",
                "POST /admin/raw-facts",
            ],
            "keyed_api": {
                "get_a_key": "/dashboard",
                "endpoints": [
                    "POST /api/auth/register",
                    "POST /api/auth/resend-key",
                    "POST /api/auth/magic-link  (dashboard login)",
                    "POST /api/auth/login  (password)",
                    "POST /api/auth/register-password",
                    "POST /api/auth/forgot-password",
                    "GET  /api/auth/me  (session)",
                    "GET  /admin/subscriptions  (admin secret)",
                    "GET /api/company/{ticker}",
                    "GET /api/demo/{ticker}  (no key, 5/day per address)",
                    "GET /api/user/status",
                    "GET /api/download-dataset",
                ],
                "auth": "Send your key as an X-API-Key header.",
                "free_tier": (
                    f"{get_settings().free_tier_monthly_calls} calls per "
                    "calendar month"
                ),
            },
            "disclaimer": (
                "Descriptive data only. Makes no predictions and produces no scores."
            ),
        }
    )
