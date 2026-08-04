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
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from src.config.settings import get_settings
from src.logging_config import configure_logging
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

    app.state.boot_complete = True


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Boot the service without blocking the healthcheck."""
    try:
        configure_logging()
    except Exception:  # noqa: BLE001 - never let logging setup stall startup
        pass
    app.state.boot_complete = False
    boot_task = asyncio.create_task(_boot(app))
    try:
        yield
    finally:
        boot_task.cancel()


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

_backfill_lock = asyncio.Lock()


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


_BACKFILL_KINDS = ("bars", "sectors", "fundamentals", "earnings")


@app.post("/backfill", response_model=RunResponse)
async def trigger_backfill(
    background: BackgroundTasks,
    kind: str = "bars",
    days: int = Query(600, ge=1, le=2000),
    # Back-compat with the old boolean flags; `kind` is the current interface.
    fundamentals: bool = False,
    sectors: bool = False,
) -> RunResponse:
    """Load ONE kind of data. `kind` = bars | sectors | fundamentals | earnings.

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
            elif kind == "fundamentals":
                m = await backfill_fundamentals()
                log.info("backfill_fundamentals_done", rows=m)
                record_backfill_result("fundamentals", m)
            elif kind == "earnings":
                e = await backfill_earnings()
                log.info("backfill_earnings_done", rows=e)
                record_backfill_result("earnings", e)
        except Exception as exc:  # noqa: BLE001 - logged, never crashes the API
            record_backfill_error(str(exc))
            record_backfill_result(kind, 0, error=str(exc))
            log.exception("backfill_failed", kind=kind, error=str(exc))


@app.get("/status")
def status() -> dict[str, Any]:
    """Row counts so you can watch the backfill fill up and confirm readiness."""
    from sqlalchemy import func

    from src.backfill import get_backfill_state
    from src.storage.models import UniverseSnapshot

    out: dict[str, Any] = {"backfill_running": _backfill_lock.locked()}
    out["backfill"] = get_backfill_state()
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


def _admin_verdict(health: dict[str, Any], db_ok: bool) -> dict[str, Any]:
    """One plain-English line: what's wrong and what to do. Ordered by severity."""
    if not db_ok:
        return {"level": "error", "headline": "Database unreachable",
                "detail": "The service can't read its own data.",
                "action": "Check DATABASE_URL and that Postgres is up."}

    fu = health.get("fundamentals") or {}
    if isinstance(fu, dict) and not fu.get("error") and (fu.get("rows") or 0) == 0:
        return {
            "level": "warn",
            "headline": "Fundamentals table is empty",
            "detail": "No SEC as-reported facts have been loaded.",
            "action": "Tap Fundamentals to load the quarterly datasets.",
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
        return {"level": "warn", "headline": f"Price data is {st} days stale",
                "detail": "Bars have not refreshed recently.",
                "action": "Tap Backfill to refresh the bars."}

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
        get_raw_facts_state,
        get_reload_state,
    )
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

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "verdict": _admin_verdict(health, db_ok),
        "data_health": health,
        "config": _config_report(),
        "logs": get_recent_logs(limit=200),
        "actions": _admin_actions(),
        "extraction": get_extraction_reports(),
        "reload": get_reload_state(),
        "sec_cache": cache_status(),
        "raw_facts": get_raw_facts_state(),
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
    return HTMLResponse(render_company_page(view, flow=flow, scale=scale))


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
def home() -> HTMLResponse:
    """The front door: a sentence, a search box, and five real balance sheets.

    Each thumbnail is built by the same `build_view1` the full page uses, so the
    home page can never advertise a shape the page then contradicts. A ticker
    whose drawing will not build is offered without one -- never with a
    placeholder, which would be a picture of nothing presented as a company.
    """
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
    return HTMLResponse(render_home(pairs))


@app.get("/search")
def search(q: str = Query("", max_length=64)) -> Response:
    """One ticker in, straight to its page.

    A redirect rather than a rendered result: the answer to "JPM" is JPM's page,
    and a search-results screen between the two would be a page whose only job
    is to be clicked through. A symbol we hold no data for still goes to
    /company, which is the one place that can say so and offer alternatives.
    """
    symbol = _clean_ticker(q)
    if not symbol:
        from src.company.suggest import suggestions
        from src.report.home_page import render_search_empty

        return HTMLResponse(render_search_empty(suggestions()), status_code=400)
    if not _is_ticker_shaped(symbol):
        from src.report.company_page import render_not_found

        return HTMLResponse(
            render_not_found(symbol, "That does not look like a ticker symbol."),
            status_code=404,
        )
    return RedirectResponse(url=f"/company/{symbol}", status_code=303)


@app.get("/api")
def api_index() -> JSONResponse:
    return JSONResponse(
        {
            "service": "To Scale",
            "description": (
                "Filed financial statements, drawn at true proportion."
            ),
            "endpoints": [
                "/", "/search?q=TICKER", "/company/{ticker}",
                "/health", "/status", "/reconcile",
                "/admin", "/admin.json", "/admin/balance-sheet", "/admin/verify",
                "/admin/universe-check",
                "POST /backfill", "POST /admin/reload-fundamentals",
                "POST /admin/raw-facts",
            ],
            "disclaimer": (
                "Descriptive data only. Makes no predictions and produces no scores."
            ),
        }
    )
