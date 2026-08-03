"""API surface tests.

The api is the deploy surface, so its endpoints get real coverage here. Uses a
file-backed DB shared between the seeding and the app, and FastAPI's TestClient.
"""

from __future__ import annotations

import datetime as dt

import pytest

AS_OF = dt.date(2025, 6, 2)


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("API_KEY", "")  # open by default
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


@pytest.fixture
def client(api_db):
    from fastapi.testclient import TestClient

    from src import api

    api._backfill_gate.reset()
    api._reconcile_gate.reset()
    with TestClient(api.app) as c:
        yield c


def _seed_fundamentals(rows: list[dict]) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as session:
        for r in rows:
            session.add(Fundamental(**r))


def _fund(ticker: str, metric: str, value: float) -> dict:
    return {
        "ticker": ticker, "metric": metric, "value": value,
        "period_end": dt.date(2025, 12, 31), "fiscal_period": "FY",
        "filing_date": dt.date(2026, 2, 13), "source": "sec", "restated": False,
    }


# --------------------------------------------------------------------- basics
def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_api_index_lists_the_surviving_endpoints(client):
    body = client.get("/api").json()
    assert "/admin" in body["endpoints"]
    assert "/admin/balance-sheet" in body["endpoints"]
    # The funnel is gone; nothing may advertise a run or a report.
    joined = " ".join(body["endpoints"])
    assert "/run" not in joined and "/report" not in joined


def test_favicon_is_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]


def test_static_assets_are_served(client):
    for path in ("/static/admin.css", "/static/admin.js"):
        assert client.get(path).status_code == 200, path


def test_status_reports_counts(client):
    body = client.get("/status").json()
    assert body["price_bars"] == 0
    assert "backfill" in body


# ---------------------------------------------------------------------- admin
def test_root_redirects_to_admin(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/admin"


def test_admin_page_and_json(client):
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Admin" in page.text

    body = client.get("/admin.json").json()
    for key in ("verdict", "data_health", "config", "logs", "actions", "extraction"):
        assert key in body, key


def test_admin_config_never_prints_secret_values(monkeypatch, client):
    monkeypatch.setenv("POLYGON_API_KEY", "super-secret-value")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    body = client.get("/admin.json").json()
    assert "super-secret-value" not in str(body["config"])
    get_settings.cache_clear()


def test_admin_verdict_calls_out_an_empty_fundamentals_table(client):
    body = client.get("/admin.json").json()
    assert body["verdict"]["headline"] == "Fundamentals table is empty"


def test_old_diagnostics_path_is_gone(client):
    assert client.get("/diagnostics").status_code == 404
    assert client.get("/diagnostics.json").status_code == 404


def test_funnel_endpoints_are_gone(client):
    for path in ("/reports", "/validation", "/llm-check", "/report/latest"):
        assert client.get(path).status_code == 404, path
    # 404, not 405: the route is gone entirely, not merely wrong-method.
    assert client.post("/run").status_code == 404


# --------------------------------------------------------------- balance sheet
def test_balance_sheet_reports_a_ticker_with_no_data(client):
    body = client.get("/admin/balance-sheet?tickers=NOPE").json()
    assert body["company_sheets"]["NOPE"] == {"found": False}


def test_balance_sheet_checks_the_accounting_identity(client):
    _seed_fundamentals([
        _fund("JPM", "total_assets", 4_424_900_000_000.0),
        _fund("JPM", "total_liabilities", 4_062_462_000_000.0),
        _fund("JPM", "total_equity", 362_438_000_000.0),
    ])
    body = client.get("/admin/balance-sheet?tickers=JPM").json()
    sheet = body["company_sheets"]["JPM"]

    assert sheet["found"] is True
    assert sheet["assets"]["total_assets"]["value"] == 4_424_900_000_000.0
    assert sheet["equity"]["shareholders_equity"]["value"] == 362_438_000_000.0

    check = sheet["balance_check"]
    assert check["error"] is None
    assert check["balanced"] is True
    assert check["diff_pct"] == 0.0


def test_balance_sheet_will_not_call_a_zero_total_balanced(client):
    """The old check compared 0 to a negative and returned balanced=true."""
    _seed_fundamentals([
        _fund("ZERO", "total_assets", 0.0),
        _fund("ZERO", "total_equity", -1_426_000_000.0),
    ])
    check = client.get("/admin/balance-sheet?tickers=ZERO").json()[
        "company_sheets"]["ZERO"]["balance_check"]

    assert check["balanced"] is False
    assert "zero or negative" in check["error"]


def test_balance_sheet_says_so_when_the_identity_is_uncheckable(client):
    """No reported total liabilities -> do not invent one by summing parts."""
    _seed_fundamentals([
        _fund("PART", "total_assets", 1_000.0),
        _fund("PART", "current_liabilities", 400.0),
        _fund("PART", "total_equity", 500.0),
    ])
    check = client.get("/admin/balance-sheet?tickers=PART").json()[
        "company_sheets"]["PART"]["balance_check"]

    assert check["balanced"] is False
    assert "total_liabilities missing" in check["error"]


def test_balance_sheet_marks_missing_concepts_rather_than_zero(client):
    _seed_fundamentals([_fund("THIN", "total_assets", 1_000.0)])
    sheet = client.get("/admin/balance-sheet?tickers=THIN").json()[
        "company_sheets"]["THIN"]

    assert sheet["assets"]["goodwill"]["missing"] is True
    assert sheet["assets"]["goodwill"]["value"] is None
    assert "goodwill" in sheet["missing_concepts"]


def test_balance_sheet_reports_coverage(client):
    _seed_fundamentals([
        _fund("A", "total_assets", 100.0),
        _fund("A", "total_equity", 50.0),
        _fund("B", "total_assets", 200.0),
    ])
    cov = client.get("/admin/balance-sheet?tickers=A,B").json()["coverage"]

    assert cov["tickers_with_any_fundamentals"] == 2
    assert cov["by_concept"]["total_assets"]["tickers_with_data"] == 2
    assert cov["by_concept"]["total_assets"]["coverage_pct"] == 100.0
    # Keyed by concept name; the equity concept reads the `total_equity` metric.
    assert cov["by_concept"]["shareholders_equity"]["metric"] == "total_equity"
    assert cov["by_concept"]["shareholders_equity"]["coverage_pct"] == 50.0
    # Renderable needs BOTH assets and equity, so only A counts.
    assert cov["tickers_renderable"] == 1


# -------------------------------------------------------------------- backfill
def test_backfill_needs_no_token(client, monkeypatch):
    import src.api as api

    async def fake_bg(kind: str, days: int) -> None:
        return None

    monkeypatch.setattr(api, "_backfill_bg", fake_bg)
    r = client.post("/backfill?kind=bars")
    assert r.status_code == 200
    assert r.json()["accepted"] is True


def test_backfill_dispatches_on_kind_not_always_bars(client, monkeypatch):
    import src.api as api

    seen: list[str] = []

    async def fake_bg(kind: str, days: int) -> None:
        seen.append(kind)

    monkeypatch.setattr(api, "_backfill_bg", fake_bg)
    for kind in ("bars", "sectors", "fundamentals", "earnings"):
        api._backfill_gate.reset()
        assert client.post(f"/backfill?kind={kind}").status_code == 200
    assert seen == ["bars", "sectors", "fundamentals", "earnings"]


def test_backfill_rejects_unknown_kind(client):
    assert client.post("/backfill?kind=nonsense").status_code == 400


def test_backfill_is_rate_limited(client, monkeypatch):
    import src.api as api
    from src.config.settings import get_settings

    monkeypatch.setenv("BACKFILL_RATE_PER_HOUR", "1")
    get_settings.cache_clear()
    api._backfill_gate.reset()

    async def fake_bg(kind: str, days: int) -> None:
        return None

    monkeypatch.setattr(api, "_backfill_bg", fake_bg)
    assert client.post("/backfill?kind=bars").status_code == 200
    assert client.post("/backfill?kind=bars").status_code == 429
    get_settings.cache_clear()
