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

    from src import api, backfill

    api._backfill_gate.reset()
    api._reconcile_gate.reset()
    # The reload/dump state is deliberately process-global (one service, one
    # job at a time), so it survives between tests unless reset here.
    backfill._RELOAD_STATE.update(
        phase="idle", started_at=None, finished_at=None, rows_deleted=None,
        rows_written=None, quarters_requested=None, last_error=None,
        verification=None,
    )
    backfill._RAW_FACTS_STATE.update(
        phase="idle", request=None, started_at=None, finished_at=None,
        last_error=None, result=None,
    )
    backfill._LAST_EXTRACTION_REPORTS.clear()
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


# ---------------------------------------------------------------------- verify
def _seed_reference_company() -> None:
    """JPM at its real consolidated figures, so verification should pass."""
    _seed_fundamentals([
        _fund("JPM", "total_assets", 4_424_900_000_000.0),
        _fund("JPM", "total_liabilities", 4_062_462_000_000.0),
        _fund("JPM", "total_equity", 362_438_000_000.0),
    ])


def test_verify_fails_loudly_on_an_empty_table(client):
    body = client.get("/admin/verify").json()
    assert body["passed"] is False
    assert "FAIL" in body["summary"]
    assert body["companies"]["JPM"]["found"] is False


def test_verify_passes_on_the_real_jpm_figures(client):
    _seed_reference_company()
    jpm = client.get("/admin/verify").json()["companies"]["JPM"]

    assert jpm["passed"] is True
    assert jpm["metrics"]["total_assets"]["actual"] == 4_424_900_000_000.0
    assert jpm["metrics"]["total_assets"]["drift_pct"] == 0.0
    assert jpm["metrics"]["total_equity"]["actual"] == 362_438_000_000.0
    assert jpm["identity"]["checkable"] is True
    assert jpm["identity"]["balanced"] is True


def test_verify_fails_on_the_old_wrong_jpm_figures(client):
    """The exact numbers the old parser stored must not pass."""
    _seed_fundamentals([
        _fund("JPM", "total_assets", 641_190_000_000.0),   # EMEA segment
        _fund("JPM", "total_equity", -1_426_000_000.0),    # hedge component
    ])
    jpm = client.get("/admin/verify").json()["companies"]["JPM"]

    assert jpm["passed"] is False
    assert jpm["metrics"]["total_assets"]["passed"] is False
    assert jpm["metrics"]["total_equity"]["passed"] is False


def test_verify_flags_an_impossible_total(client):
    _seed_fundamentals([_fund("FCX", "total_assets", -20_400_000_000.0)])
    fcx = client.get("/admin/verify").json()["companies"]["FCX"]

    assert fcx["passed"] is False
    assert "total assets is" in fcx["impossible"]


def test_verify_accepts_aals_genuinely_negative_equity(client):
    """A stockholders' deficit is correct for AAL and must not be 'fixed'."""
    _seed_fundamentals([_fund("AAL", "total_equity", -3_900_000_000.0)])
    aal = client.get("/admin/verify").json()["companies"]["AAL"]

    assert aal["passed"] is True
    assert aal["metrics"]["total_equity"]["actual"] < 0


def test_verify_reports_coverage(client):
    _seed_reference_company()
    cov = client.get("/admin/verify").json()["coverage"]

    assert cov["tickers_with_any_fundamentals"] == 1
    assert cov["tickers_renderable"] == 1
    assert cov["by_concept"]["total_assets"]["coverage_pct"] == 100.0
    assert cov["by_concept"]["goodwill"]["coverage_pct"] == 0.0


# ---------------------------------------------------------------------- reload
def test_reload_refuses_without_confirm(client):
    """An open endpoint that deletes every row must not fire on a stray tap."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental
    from sqlalchemy import func, select

    _seed_reference_company()
    r = client.post("/admin/reload-fundamentals")

    assert r.status_code == 400
    assert "confirm=true" in r.json()["detail"]
    with session_scope() as s:
        still_there = s.execute(select(func.count()).select_from(Fundamental)).scalar_one()
    assert still_there == 3, "a refused reload must not have deleted anything"


def test_reload_accepts_with_confirm(client, monkeypatch):
    import src.api as api

    seen: list[int] = []

    async def fake_bg(quarters: int) -> None:
        seen.append(quarters)

    monkeypatch.setattr(api, "_reload_bg", fake_bg)
    r = client.post("/admin/reload-fundamentals?confirm=true&quarters=7")

    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert seen == [7]


def test_reload_state_is_exposed_for_progress(client):
    body = client.get("/admin.json").json()
    assert body["reload"]["phase"] == "idle"


def test_wipe_empties_the_table(client):
    from src.backfill import wipe_fundamentals
    from src.storage.db import session_scope
    from src.storage.models import Fundamental
    from sqlalchemy import func, select

    _seed_reference_company()
    deleted = wipe_fundamentals()

    assert deleted == 3
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(Fundamental)).scalar_one() == 0


# -------------------------------------------------------------------- raw facts
def _msft_zip() -> bytes:
    """A quarter carrying MSFT's Assets/Equity, with dimensional decoys."""
    import io
    import zipfile

    sub_cols = ["adsh", "cik", "name", "form", "period", "filed", "fp"]
    num_cols = ["adsh", "tag", "version", "coreg", "ddate", "qtrs", "uom",
                "segments", "value"]

    def tsv(cols, rows):
        return "\n".join(
            ["\t".join(cols)]
            + ["\t".join(str(r.get(c, "")) for c in cols) for r in rows]
        )

    def num(**kw):
        r = {c: "" for c in num_cols}
        r.update(adsh="m1", version="us-gaap/2025", ddate="20251231", uom="USD")
        r.update(kw)
        return r

    sub = [{"adsh": "m1", "cik": "0000789019", "name": "MICROSOFT CORP",
            "form": "10-Q", "period": "20251231", "filed": "20260128", "fp": "Q2"}]
    nums = [
        num(tag="Assets", qtrs="0", segments="BusinessSegments=Azure", value="200000000000"),
        num(tag="Assets", qtrs="0", value="665300000000"),
        num(tag="StockholdersEquity", qtrs="0",
            segments="EquityComponents=CommonStockMember", value="100000000000"),
        num(tag="StockholdersEquity", qtrs="0", value="390875000000"),
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub.txt", tsv(sub_cols, sub))
        z.writestr("num.txt", tsv(num_cols, nums))
    return buf.getvalue()


def test_raw_facts_dump_isolates_the_consolidated_row(client, monkeypatch):
    """The dump must show exactly one consolidated instant per tag."""
    import src.backfill as bf

    async def fake_download(year, quarter):
        return _msft_zip()

    monkeypatch.setattr(bf, "download_dataset_for_dump", fake_download)

    import asyncio
    result = asyncio.run(
        bf.run_raw_facts_dump("MSFT", 2026, 1, ("Assets", "StockholdersEquity"), "20251231")
    )

    assets = result["by_tag"]["Assets"]
    assert assets["row_count"] == 2
    assert assets["filter_selects_exactly_one"] is True
    assert assets["consolidated_instant"][0]["value"] == "665300000000"
    # The column that separates them is named explicitly.
    assert "segments" in assets["varying_columns"]

    equity = result["by_tag"]["StockholdersEquity"]
    assert equity["filter_selects_exactly_one"] is True
    assert equity["consolidated_instant"][0]["value"] == "390875000000"


def test_raw_facts_reports_a_missing_company_clearly(client, monkeypatch):
    import asyncio

    import src.backfill as bf

    async def fake_download(year, quarter):
        return _msft_zip()

    monkeypatch.setattr(bf, "download_dataset_for_dump", fake_download)
    result = asyncio.run(bf.run_raw_facts_dump("JPM", 2026, 1, ("Assets",), None))
    assert "No submissions" in result["error"]


def test_raw_facts_endpoint_accepts_and_backgrounds(client, monkeypatch):
    import src.api as api

    seen: list[tuple] = []

    async def fake_bg(ticker, year, quarter, tags, ddate, cik):
        seen.append((ticker, year, quarter, tags, ddate))

    monkeypatch.setattr(api, "_raw_facts_bg", fake_bg)
    r = client.post(
        "/admin/raw-facts?ticker=MSFT&year=2026&quarter=1"
        "&tags=Assets,StockholdersEquity&ddate=20251231"
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert seen == [("MSFT", 2026, 1, ("Assets", "StockholdersEquity"), "20251231")]


def test_raw_facts_rejects_empty_tags(client):
    assert client.post("/admin/raw-facts?tags=").status_code == 400


def test_raw_facts_state_is_exposed(client):
    assert client.get("/admin.json").json()["raw_facts"]["phase"] == "idle"


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
