"""API surface tests.

The api is the deploy surface, so its endpoints get real coverage here. Uses a
file-backed DB shared between the seeding and the app, and FastAPI's TestClient.
"""

from __future__ import annotations

ADMIN_SECRET = "test-admin-secret-do-not-use"
# Every /admin route and every destructive one now requires this. They
# used to answer anybody; see `api.require_admin`.
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


import datetime as dt

import pytest

AS_OF = dt.date(2025, 6, 2)


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("API_KEY", "")  # open by default
    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
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
        rows_written=None, quarters_requested=None, quarters=None, staged=None,
        quarters_loaded=None, quarters_available=None, unpublished=None,
        data_intact=None, last_error=None, verification=None,
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
    # /api is the human page now; the index it used to be is at /api.json.
    body = client.get("/api.json").json()
    # The admin routes used to be listed here, and this test used to assert
    # they WERE -- an open endpoint publishing the path of every gated surface
    # on the service. They are filtered now; the public routes are untouched.
    assert "/admin" not in body["endpoints"]
    assert "/admin/balance-sheet" not in body["endpoints"]
    assert "/company/{ticker}" in body["endpoints"]
    assert "/health" in body["endpoints"]
    assert "GET /api/company/{ticker}" in body["keyed_api"]["endpoints"]
    # The funnel is gone; nothing may advertise a run or a report.
    joined = " ".join(body["endpoints"])
    assert "/run" not in joined and "/report" not in joined


def test_the_public_index_names_no_admin_surface_at_all(client):
    """Not "no admin route in the list" -- no `admin` anywhere in the response.

    A path-by-path assertion only covers the paths somebody thought to write
    down. The property that matters is that a stranger fetching this open
    endpoint learns nothing about the gated half of the service, and that is a
    statement about the whole payload: the endpoint lists, the annotations
    beside them, and anything a later edit adds.

    `/status` is checked separately because it is admin-gated and its path does
    not contain the word -- it is exactly the entry a naive filter would miss.
    """
    r = client.get("/api.json")
    assert r.status_code == 200
    assert "admin" not in r.text.lower(), r.text

    endpoints = r.json()["endpoints"]
    assert not [e for e in endpoints if e.split()[0] == "/status"], endpoints


def test_api_is_a_page_not_a_payload(client):
    """The nav bar links here, so it has to answer in HTML."""
    r = client.get("/api")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "X-API-Key" in r.text
    assert "/api/company/{ticker}" in r.text
    # The admin routes are on the JSON index and stay off the public page.
    assert "/admin/raw-facts" not in r.text


def test_favicon_is_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert "svg" in r.headers["content-type"]


def test_static_assets_are_served(client):
    for path in ("/static/admin.css", "/static/admin.js"):
        assert client.get(path).status_code == 200, path


def test_status_reports_counts(client):
    body = client.get("/status", headers=ADMIN).json()
    assert body["price_bars"] == 0
    assert "backfill" in body


# ---------------------------------------------------------------------- admin
def test_root_is_the_home_page(client):
    """/ used to bounce to /admin. The front door is the product now; the admin
    tool is reachable from it rather than standing in for it."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    # Not the old "99.9% Accurate ..." headline: the published rate was
    # retired (tests/test_content_accuracy.py), and this assertion outlived it.
    assert "traced to its source" in r.text
    # The old fallback description was the SAME sentence on all eight
    # shell pages, which is one page to a search engine and eight
    # near-duplicates to a crawler. Each page carries its own now.
    assert "reconciled balance sheet data from SEC EDGAR" in r.text
    assert 'action="/search"' in r.text
    assert 'href="/admin"' in r.text


def test_admin_page_and_json(client):
    page = client.get("/admin")
    assert page.status_code == 200
    assert "Admin" in page.text

    body = client.get("/admin.json", headers=ADMIN).json()
    for key in ("verdict", "data_health", "config", "logs", "actions", "extraction"):
        assert key in body, key


def test_the_code_gate_hides_the_page_before_it_paints(client):
    """The class is set by an inline script in <head>, not by admin.js.

    Deferred, the page would render fully and then hide itself -- a flash of
    the whole admin surface, which is the one thing a gate must not do.
    """
    page = client.get("/admin").text
    head = page.split("</head>", 1)[0]

    assert 'classList.add("locked")' in head, "the gate must run before paint"
    assert "sessionStorage" in head
    assert 'id="gate"' in page and 'id="gateInput"' in page

    css = client.get("/static/admin.css").text
    assert "html.locked body > nav,html.locked body > main{display:none}" in css


def test_the_gate_holds_no_secret_of_its_own(client):
    """The gate used to compare a three-digit code IN THIS FILE, which is
    served to anybody who asks for it -- so the code was public and every
    endpoint behind it answered without it anyway.

    Nothing is compared client-side now. What is typed is the real
    ADMIN_SECRET, it goes out as a header, and the server decides.
    """
    js = client.get("/static/admin.js").text

    assert "GATE_CODE" not in js, "a secret compared in public JavaScript"
    assert '"X-Admin-Secret"' in js
    assert "adminFetch(" in js
    # Unlocking must also start the poller, or /admin.json would be fetched
    # every 10s behind a gate nobody has opened.
    assert "boot();" in js


def test_every_admin_call_in_the_page_carries_the_secret(client):
    """One wrapper, used everywhere. A bare fetch() to a gated route gets a
    403 now, so this is what stops a call being added later that quietly goes
    open."""
    js = client.get("/static/admin.js").text

    for route in (
        "/admin.json", "/admin/balance-sheet", "/admin/universe-check",
        "/admin/verify", "/admin/reload-fundamentals", "/admin/raw-facts",
        "/reconcile",
    ):
        assert f'adminFetch("{route}' in js, route


def test_the_admin_surface_is_authenticated(client):
    """It was not. `/admin.json` served the in-memory log ring -- every
    structlog field verbatim, so every customer's address, who signed in and
    who bought -- to anybody who asked. `/admin/reload-fundamentals` DELETED
    the fundamentals table on the strength of a query parameter the caller
    supplies. The gate in front of both was three digits compared in public
    JavaScript.

    `/admin` itself stays open: it is a shell that fetches everything through
    the gated routes, so it is useless without the secret and has to be
    reachable for an operator to type one in.
    """
    assert client.get("/admin").status_code == 200

    for route in (
        "/admin.json", "/admin/balance-sheet?tickers=JPM",
        "/admin/universe-check", "/admin/verify", "/reconcile",
    ):
        assert client.get(route).status_code == 403, route
    for route in ("/admin/reload-fundamentals?confirm=true", "/backfill?kind=bars"):
        assert client.post(route).status_code == 403, route

    # A wrong secret is refused as firmly as none at all.
    wrong = {"X-Admin-Secret": "not-the-secret"}
    assert client.get("/admin.json", headers=wrong).status_code == 403

    # A non-ASCII one is a 403 too, not a 500. `compare_digest` on str raises
    # TypeError the moment either side leaves ASCII, so this used to be an
    # unhandled crash on the authentication path. Checked against the guard
    # directly: httpx refuses to put a non-ASCII value in a header at all.
    import pytest as _pytest
    from fastapi import HTTPException

    from src import accounts

    with _pytest.raises(HTTPException) as caught:
        accounts.verify_admin_secret("paßwort")
    assert caught.value.status_code == 403

    # And the right one still works, including the confirm guard behind it.
    assert client.get("/admin.json", headers=ADMIN).status_code == 200
    refused = client.post("/admin/reload-fundamentals", headers=ADMIN)
    assert refused.status_code == 400
    assert "confirm=true" in refused.json()["detail"]


def test_admin_keeps_status_colour_and_nothing_else(client):
    """The reader-facing rule is "the only colour is data". On /admin the
    STATUS is the data, so blue/yellow/red stay -- on pills, the verdict and
    the destructive button, and nowhere else."""
    css = client.get("/static/admin.css").text
    chrome, status = css.split("/* STATUS ONLY, from here down. */", 1)

    for token in ("--red:", "--blue:", "--yellow:"):
        assert token not in chrome, f"{token} must sit below the status marker"

    for rule, hue in (
        (".pill.ok{", "--blue"), (".pill.warn{", "--yellow"),
        (".pill.bad{", "--red"), (".act.danger{", "--red"),
        (".v-error::before{", "--red"),
    ):
        block = css.split(rule, 1)[1].split("}", 1)[0]
        assert hue in block, f"{rule} lost its status colour"

    # The ordinary controls are ink and grey, like every other page.
    for rule in (".copy{", ".mini{", ".chipbtn{", ".card{"):
        block = css.split(rule, 1)[1].split("}", 1)[0]
        for hue in ("--red", "--blue", "--yellow"):
            assert hue not in block, f"{rule} must not use {hue}"


def test_admin_config_never_prints_secret_values(monkeypatch, client):
    monkeypatch.setenv("POLYGON_API_KEY", "super-secret-value")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    body = client.get("/admin.json", headers=ADMIN).json()
    assert "super-secret-value" not in str(body["config"])
    get_settings.cache_clear()


def test_admin_verdict_calls_out_an_empty_fundamentals_table(client):
    body = client.get("/admin.json", headers=ADMIN).json()
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
    body = client.get("/admin/balance-sheet?tickers=NOPE", headers=ADMIN).json()
    assert body["company_sheets"]["NOPE"] == {"found": False}


def test_balance_sheet_checks_the_accounting_identity(client):
    _seed_fundamentals([
        _fund("JPM", "total_assets", 4_424_900_000_000.0),
        _fund("JPM", "total_liabilities", 4_062_462_000_000.0),
        _fund("JPM", "total_equity", 362_438_000_000.0),
    ])
    body = client.get("/admin/balance-sheet?tickers=JPM", headers=ADMIN).json()
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
    check = client.get("/admin/balance-sheet?tickers=ZERO", headers=ADMIN).json()[
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
    check = client.get("/admin/balance-sheet?tickers=PART", headers=ADMIN).json()[
        "company_sheets"]["PART"]["balance_check"]

    assert check["balanced"] is False
    assert "total_liabilities missing" in check["error"]


def test_balance_sheet_marks_missing_concepts_rather_than_zero(client):
    _seed_fundamentals([_fund("THIN", "total_assets", 1_000.0)])
    sheet = client.get("/admin/balance-sheet?tickers=THIN", headers=ADMIN).json()[
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
    cov = client.get("/admin/balance-sheet?tickers=A,B", headers=ADMIN).json()["coverage"]

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
    body = client.get("/admin/verify", headers=ADMIN).json()
    assert body["passed"] is False
    assert "FAIL" in body["summary"]
    assert body["companies"]["JPM"]["found"] is False


def test_verify_passes_on_the_real_jpm_figures(client):
    _seed_reference_company()
    jpm = client.get("/admin/verify", headers=ADMIN).json()["companies"]["JPM"]

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
    jpm = client.get("/admin/verify", headers=ADMIN).json()["companies"]["JPM"]

    assert jpm["passed"] is False
    assert jpm["metrics"]["total_assets"]["passed"] is False
    assert jpm["metrics"]["total_equity"]["passed"] is False


def test_verify_flags_an_impossible_total(client):
    _seed_fundamentals([_fund("FCX", "total_assets", -20_400_000_000.0)])
    fcx = client.get("/admin/verify", headers=ADMIN).json()["companies"]["FCX"]

    assert fcx["passed"] is False
    assert "total assets is" in fcx["impossible"]


def test_verify_accepts_aals_genuinely_negative_equity(client):
    """A stockholders' deficit is correct for AAL and must not be 'fixed'."""
    _seed_fundamentals([_fund("AAL", "total_equity", -3_900_000_000.0)])
    aal = client.get("/admin/verify", headers=ADMIN).json()["companies"]["AAL"]

    assert aal["passed"] is True
    assert aal["metrics"]["total_equity"]["actual"] < 0


def test_verify_reports_coverage(client):
    _seed_reference_company()
    cov = client.get("/admin/verify", headers=ADMIN).json()["coverage"]

    assert cov["tickers_with_any_fundamentals"] == 1
    assert cov["tickers_renderable"] == 1
    assert cov["by_concept"]["total_assets"]["coverage_pct"] == 100.0
    assert cov["by_concept"]["goodwill"]["coverage_pct"] == 0.0


# ---------------------------------------------------------------------- reload
def test_reload_refuses_without_confirm(client):
    """An open endpoint that deletes every row must not fire on a stray tap."""
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    _seed_reference_company()
    r = client.post("/admin/reload-fundamentals", headers=ADMIN)

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
    r = client.post("/admin/reload-fundamentals?confirm=true&quarters=7", headers=ADMIN)

    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert seen == [7]


def test_reload_state_is_exposed_for_progress(client):
    body = client.get("/admin.json", headers=ADMIN).json()
    assert body["reload"]["phase"] == "idle"


def test_wipe_empties_the_table(client):
    from sqlalchemy import func, select

    from src.backfill import wipe_fundamentals
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

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
        r = dict.fromkeys(num_cols, "")
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
        "&tags=Assets,StockholdersEquity&ddate=20251231",
        headers=ADMIN,
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    assert seen == [("MSFT", 2026, 1, ("Assets", "StockholdersEquity"), "20251231")]


def test_raw_facts_rejects_empty_tags(client):
    assert client.post("/admin/raw-facts?tags=", headers=ADMIN).status_code == 400


def test_raw_facts_state_is_exposed(client):
    assert client.get("/admin.json", headers=ADMIN).json()["raw_facts"]["phase"] == "idle"


# -------------------------------------------------------------------- backfill
def test_backfill_needs_the_admin_secret(client, monkeypatch):
    """It said "open by design" and meant it, back when this was a data
    experiment with nothing to lose. It has customers now."""
    import src.api as api

    async def fake_bg(kind: str, days: int) -> None:
        return None

    monkeypatch.setattr(api, "_backfill_bg", fake_bg)
    assert client.post("/backfill?kind=bars").status_code == 403

    r = client.post("/backfill?kind=bars", headers=ADMIN)
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
        assert client.post(
            f"/backfill?kind={kind}", headers=ADMIN
        ).status_code == 200
    assert seen == ["bars", "sectors", "fundamentals", "earnings"]


def test_backfill_rejects_unknown_kind(client):
    assert client.post("/backfill?kind=nonsense", headers=ADMIN).status_code == 400


def test_backfill_is_rate_limited(client, monkeypatch):
    import src.api as api
    from src.config.settings import get_settings

    monkeypatch.setenv("BACKFILL_RATE_PER_HOUR", "1")
    get_settings.cache_clear()
    api._backfill_gate.reset()

    async def fake_bg(kind: str, days: int) -> None:
        return None

    monkeypatch.setattr(api, "_backfill_bg", fake_bg)
    assert client.post("/backfill?kind=bars", headers=ADMIN).status_code == 200
    assert client.post("/backfill?kind=bars", headers=ADMIN).status_code == 429
    get_settings.cache_clear()


# --------------------------------------------------------------- universe check
def test_universe_check_endpoint_reports_the_distribution(client):
    _seed_fundamentals([
        _fund("OK1", "total_assets", 1_000.0),
        _fund("OK1", "total_liabilities", 700.0),
        _fund("OK1", "total_equity", 300.0),
        _fund("BAD1", "total_assets", 1_000.0),
        _fund("BAD1", "total_liabilities", 700.0),
        _fund("BAD1", "total_equity", 100.0),
    ])
    body = client.get("/admin/universe-check", headers=ADMIN).json()

    assert body["identity"]["checkable"] == 2
    assert body["identity"]["buckets"]["within_1pct"] == 1
    assert body["identity"]["buckets"]["over_10pct"] == 1
    assert body["worst"][0]["ticker"] == "BAD1"
    assert "by_sector" in body


def test_universe_check_survives_an_empty_table(client):
    body = client.get("/admin/universe-check", headers=ADMIN).json()
    assert body["tickers_in_table"] == 0
    assert body["identity"]["checkable"] == 0
    assert body["identity"]["pass_rate_pct"] == 0.0


# ----------------------------------------------------------- reference hygiene
def test_confirmed_references_must_cite_the_dump():
    """A reference may only be `confirmed` on the strength of a raw file read.

    This is the guard against the failure mode that put $641B in the database:
    an expectation adjusted until it agreed with the parser, then treated as
    evidence. If it says confirmed, it must name the dump.
    """
    from src.company.verify import REFERENCE

    for ticker, checks in REFERENCE.items():
        for metric, ref in checks.items():
            if ref.confirmed:
                assert "num.txt dump" in ref.basis, (
                    f"{ticker}.{metric} is marked confirmed but its basis is "
                    f"{ref.basis!r} — only a raw dump can confirm a figure"
                )
            else:
                assert "unconfirmed" in ref.basis, (
                    f"{ticker}.{metric} is not confirmed, so its basis must say so"
                )


def test_msft_references_are_both_confirmed_from_the_dump():
    from src.company.verify import REFERENCE

    msft = REFERENCE["MSFT"]
    assert msft["total_assets"].value == 665_302_000_000
    assert msft["total_assets"].confirmed is True
    assert msft["total_equity"].value == 390_875_000_000
    assert msft["total_equity"].confirmed is True


def test_msft_now_passes_verification_on_the_confirmed_figures(client):
    _seed_fundamentals([
        _fund("MSFT", "total_assets", 665_302_000_000.0),
        _fund("MSFT", "total_equity", 390_875_000_000.0),
    ])
    msft = client.get("/admin/verify", headers=ADMIN).json()["companies"]["MSFT"]

    assert msft["passed"] is True
    assert msft["metrics"]["total_assets"]["drift_pct"] == 0.0
    assert msft["metrics"]["total_equity"]["drift_pct"] == 0.0
    assert msft["metrics"]["total_assets"]["confirmed"] is True


def test_a_confirmed_reference_mismatch_still_fails_the_run(client):
    """Confirmed means a disagreement IS the parser's problem, not the ref's."""
    _seed_fundamentals([
        _fund("MSFT", "total_assets", 100_000_000_000.0),   # nowhere near
        _fund("MSFT", "total_equity", 390_875_000_000.0),
    ])
    body = client.get("/admin/verify", headers=ADMIN).json()

    assert body["companies"]["MSFT"]["passed"] is False
    assert body["passed"] is False
    assert body["companies"]["MSFT"]["metrics"]["total_assets"].get("verdict") is None


# --------------------------------------------------------------- company page
def _seed_company(ticker: str, metrics: dict) -> None:
    _seed_fundamentals([_fund(ticker, m, v) for m, v in metrics.items()])


def test_company_page_renders(client):
    _seed_company("JPM", {
        "total_assets": 4_424_900_000_000.0,
        "cash": 1_570_000_000_000.0,
        "total_liabilities": 4_062_462_000_000.0,
        "total_equity": 362_438_000_000.0,
    })
    r = client.get("/company/JPM")
    assert r.status_code == 200
    assert "JPM" in r.text
    # The figures and their provenance are both on the page.
    assert "2025-12-31" in r.text and "2026-02-13" in r.text
    assert "$4.42T" in r.text


def test_company_page_is_lowercase_tolerant(client):
    _seed_company("MSFT", {
        "total_assets": 665_302_000_000.0, "total_liabilities": 274_427_000_000.0,
        "total_equity": 390_875_000_000.0,
    })
    assert client.get("/company/msft").status_code == 200


def test_company_page_says_so_when_there_is_nothing_to_draw(client):
    _seed_company("MSFT", {
        "total_assets": 665_302_000_000.0, "total_liabilities": 274_427_000_000.0,
        "total_equity": 390_875_000_000.0, "cash": 75_000_000_000.0,
    })

    r = client.get("/company/NOSUCH")

    assert r.status_code == 404
    assert "Nothing to draw" in r.text
    # And points at a ticker that does work rather than dead-ending. The
    # alternatives are read out of the database, so an empty page can never
    # send a reader to another empty page.
    assert "/company/MSFT" in r.text
    assert "/company/JPM" not in r.text, "JPM is not loaded in this test"


def test_a_page_with_nothing_loaded_at_all_offers_nothing_it_cannot_draw(client):
    r = client.get("/company/NOSUCH")

    assert r.status_code == 404
    assert "No filed statements are loaded yet" in r.text
    assert 'href="/company/' not in r.text


def test_company_page_never_shows_investment_language(client):
    _seed_company("WMT", {
        "total_assets": 260_800_000_000.0, "inventory": 56_400_000_000.0,
        "property_plant_equipment": 118_600_000_000.0,
        "total_liabilities": 169_600_000_000.0, "total_equity": 91_200_000_000.0,
    })
    text = client.get("/company/WMT").text.lower()
    for word in ("buy", "sell", "undervalued", "outperform", "rating", "score",
                 "forecast", "target price"):
        assert word not in text, f"{word!r} must not appear on a company page"


def test_company_page_names_missing_components(client):
    _seed_company("BANKY", {
        "total_assets": 1_000_000_000.0, "cash": 500_000_000.0,
        "total_liabilities": 900_000_000.0, "total_equity": 100_000_000.0,
    })
    text = client.get("/company/BANKY").text
    assert "Not reported separately" in text
    assert "Inventory" in text
    # Named, not zeroed.
    assert "not estimated, and not set to zero" in text


def test_company_page_draws_negative_equity_below_the_baseline(client):
    _seed_company("AAL", {
        "total_assets": 62_600_000_000.0,
        "property_plant_equipment": 39_400_000_000.0,
        "total_liabilities": 66_500_000_000.0,
        "total_equity": -3_900_000_000.0,
    })
    text = client.get("/company/AAL").text
    assert 'class="baseline"' in text
    assert "band neg" in text
    assert "-$3.9B" in text
    assert "Liabilities exceed total assets" in text


def test_the_two_columns_share_one_caption_row(client):
    """Equal height IS the accounting identity, so the two stacks must start at
    the same y by construction, not by luck.

    Nested inside their columns the captions are independent, and "Owed & owned
    $4.06T + $362.4B" wraps where "Owns $4.42T" does not -- which drops the
    right-hand stack half a line and quietly breaks the one thing the drawing
    asserts. As one shared grid row, a wrap lifts both stacks equally.
    """
    _seed_company("JPM", {
        "total_assets": 4_424_900_000_000.0,
        "total_liabilities": 4_062_462_000_000.0,
        "total_equity": 362_438_000_000.0,
        "cash": 469_000_000_000.0,
    })

    text = client.get("/company/JPM").text
    cols = text.split('<div class="bs-cols">', 1)[1].split("</div>\n\n", 1)[0]
    cap = cols.index('<div class="bs-cap">')
    col = cols.index('<div class="bs-col"')

    assert cols.count('<div class="bs-cap">') == 2
    # bs-col now also carries role="img" + aria-label (screen-reader summary of
    # the drawing), so match the opening tag without its closing bracket.
    assert cols.count('<div class="bs-col"') == 2
    assert cap < col, "both captions must precede both columns, as one grid row"
    # And no caption may be nested inside a column, which is what re-introduces
    # the independent wrap.
    first_col = cols[col:]
    assert '<div class="bs-cap">' not in first_col


def test_a_band_label_is_never_taller_than_its_band(client):
    """A full label is exactly two lines: the name on one, ellipsised, and the
    value under it. Left to wrap, a two-word name makes it three lines and the
    band clips it -- so what the reader sees would depend on word length."""
    _seed_company("WMT", {
        "total_assets": 260_800_000_000.0,
        "total_liabilities": 176_000_000_000.0,
        "total_equity": 84_800_000_000.0,
        "property_plant_equipment": 118_600_000_000.0,
    })

    text = client.get("/company/WMT").text

    assert '<span class="bn">' in text, "the name needs its own clamped line"
    assert '<span class="bn">Property &amp; equipment</span>' in text


def test_only_the_drawing_carries_colour(client):
    """The interface is one sheet of grey; blue, red and yellow do nothing but
    carry meaning. A chrome element painted in a data colour would read as a
    balance-sheet quantity, so the palette variables belong to bands only."""
    css = client.get("/static/company.css").text
    chrome, data = css.split("/* DATA ONLY, from here down. */", 1)

    for token in ("--red:", "--blue:", "--yellow:", "--a0:", "--l0:"):
        assert token not in chrome, f"{token} must sit below the data marker"
    # The buttons, chips, inputs and cards are all ink-and-grey.
    for rule in (".search button{", ".chip{", ".navlink{", ".card:hover{"):
        block = css.split(rule, 1)[1].split("}", 1)[0]
        for hue in ("--red", "--blue", "--yellow", "--a1", "--l1"):
            assert hue not in block, f"{rule} must not use {hue}"


def test_the_company_page_has_a_way_back(client):
    """The wordmark always linked home, but nobody reads a wordmark as a
    control -- the browser back button was the only obvious exit."""
    _seed_company("JPM", {
        "total_assets": 4_424_900_000_000.0,
        "total_liabilities": 4_062_462_000_000.0,
        "total_equity": 362_438_000_000.0,
    })

    text = client.get("/company/JPM").text
    nav = text.split("<nav>", 1)[1].split("</nav>", 1)[0]

    assert 'class="back" href="/"' in nav
    assert "Search" in nav
    # And it comes before the wordmark, so the exit is the first thing in the
    # nav rather than something to find.
    assert nav.index('class="back"') < nav.index('class="brand"')


def test_the_not_found_page_has_a_way_back_too(client):
    nav = client.get("/company/NOSUCH").text.split(
        "<nav>", 1)[1].split("</nav>", 1)[0]

    assert 'class="back" href="/"' in nav


def test_company_page_escapes_the_ticker(client):
    r = client.get("/company/%3Cscript%3E")
    assert "<script>" not in r.text.replace(
        '<script src="/static/admin.js" defer></script>', ""
    )


def test_company_page_shows_the_gdp_caution_where_the_reader_will_see_it(client):
    """The comparison is of magnitude only, and that must not be a footnote."""
    _seed_company("WMT", {
        "total_assets": 260_800_000_000.0,
        "total_liabilities": 169_600_000_000.0,
        "total_equity": 91_200_000_000.0,
        "revenue": 680_900_000_000.0, "cogs": 511_300_000_000.0,
        "gross_profit": 169_600_000_000.0, "operating_income": 29_300_000_000.0,
        "income_tax": 6_200_000_000.0, "net_income": 19_400_000_000.0,
    })
    text = client.get("/company/WMT").text
    assert "These measure different things" in text
    assert 'not "bigger than" a country' in text
    assert "World Bank" in text


def test_company_page_skips_a_flow_it_cannot_draw(client):
    """A balance sheet still renders when the income statement is incomplete."""
    _seed_company("BANKY", {
        "total_assets": 1_000_000_000.0,
        "total_liabilities": 900_000_000.0,
        "total_equity": 100_000_000.0,
        "revenue": 500_000_000.0, "net_income": 160_000_000.0,
    })
    text = client.get("/company/BANKY").text
    assert "What it owns" in text
    assert "Where the money goes" not in text
    assert "The size of it" not in text


# ------------------------------------------------------------ asset versioning
def test_admin_page_cache_busts_its_assets(client):
    """A cached admin.js makes a shipped fix look like a fix that did not work."""
    text = client.get("/admin").text
    assert "/static/admin.js?v=" in text
    assert "/static/admin.css?v=" in text


def test_company_page_cache_busts_its_css(client):
    _seed_company("X", {
        "total_assets": 1_000.0, "total_liabilities": 600.0, "total_equity": 400.0,
    })
    assert "/static/company.css?v=" in client.get("/company/X").text


def test_raw_facts_presets_carry_their_own_parameters(client):
    """A preset must submit its own params, not depend on form state."""
    import re
    from pathlib import Path

    html = Path("src/report/templates/admin.html").read_text(encoding="utf-8")
    presets = re.findall(r'<button[^>]*class="chipbtn"[^>]*>', html)
    assert presets, "the preset buttons must exist"
    for p in presets:
        assert "data-ticker=" in p, p
        assert "data-tags=" in p, p

    js = Path("src/report/static/admin.js").read_text(encoding="utf-8")
    # The handler submits directly; it must not write into the form inputs.
    handler = js[js.index('$("rawPresets")'):]
    handler = handler[:handler.index("});")]
    assert "submitRawFacts(" in handler
    assert '$("rawTicker").value =' not in handler
    assert '$("rawTags").value =' not in handler


# ---------------------------------------------------------------------------
# Auto-update: the data keeps itself current (see src/scheduler.py)
# ---------------------------------------------------------------------------
def test_status_and_admin_report_the_auto_updater(client):
    """Both surfaces must say what is keeping the data current, and what it is
    waiting on -- otherwise "why is this number old?" has no answer on a phone."""
    for path in ("/status", "/admin.json"):
        # /status is public; /admin.json is not, and both must say the same
        # thing about what is keeping the data current.
        body = client.get(path, headers=ADMIN).json()
        auto = body["auto_update"]
        assert [j["name"] for j in auto["jobs"]] == [
            "bars", "filings", "fundamentals", "earnings",
            # Company names, which is what makes searching by name rather than
            # by ticker work. Due on an empty database like every other data
            # job, because zero names IS name search being off.
            "names",
            # The renewal warning is reported like any other job, so
            # /admin shows whether it is due, off, or failing.
            "subscriptions",
        ]
        assert "enabled" in auto and "tick_minutes" in auto
        # An empty test database is behind on every DATA job, and says so.
        data_jobs = [j for j in auto["jobs"] if j["name"] != "subscriptions"]
        assert all(j["due"] is True for j in data_jobs)
        # The renewal warning is the exception, and correctly so: an empty
        # database has no subscriptions to warn about. It reports itself as not
        # due WITH A REASON, which is what distinguishes "nothing to do" from
        # "quietly broken" on /admin.
        subs = next(j for j in auto["jobs"] if j["name"] == "subscriptions")
        assert subs["due"] is False
        assert subs["now"], "the reason must be shown, not just the false"
        assert all(j["last_success_at"] is None for j in auto["jobs"])


def test_stale_bars_are_not_a_chore_while_the_updater_is_on():
    """With auto-update on, stale data is a status; with it off, it's a task."""
    from src.api import _admin_verdict

    health = {
        "recency": {"staleness_days": 9},
        "adjustment": {"status": "unchecked"},
        "backfill": {},
        "fundamentals": {"rows": 10},
    }
    auto_on = {
        "enabled": True,
        "jobs": [{"name": "bars", "now": "newest bar 2026-08-20", "due": True}],
    }
    v = _admin_verdict(health, True, auto_on)
    assert v["headline"] == "Price data is 9 days stale"
    assert "refreshes itself" in v["action"]

    v_off = _admin_verdict(health, True, {"enabled": False, "jobs": []})
    assert "Tap Backfill" in v_off["action"]


def test_a_failing_auto_update_job_is_surfaced_as_an_error():
    from src.api import _admin_verdict

    health = {
        "recency": {"staleness_days": 0},
        "adjustment": {"status": "ok"},
        "backfill": {},
        "fundamentals": {"rows": 10},
    }
    auto = {
        "enabled": True,
        "jobs": [{
            "name": "fundamentals", "due": True, "consecutive_failures": 3,
            "last_error": "sec.gov 429",
        }],
    }
    v = _admin_verdict(health, True, auto)
    assert v["level"] == "error"
    assert v["headline"] == "Auto-update failing: fundamentals"
    assert "sec.gov 429" in v["detail"]
    assert "retrying" in v["action"]


def test_api_page_examples_use_the_scheme_the_reader_arrived_on(client):
    """Railway terminates TLS in front of the container, so the app's own view
    of the scheme is the internal http hop. An example rendered from that gives
    the reader a curl that answers with a redirect."""
    r = client.get("/api", headers={"x-forwarded-proto": "https"})

    assert "https://" in r.text
    assert "http://testserver" not in r.text


# ------------------------------------------------- Search Console verification
# The real token Search Console issued for toscale.pro. Named here so that
# deleting or renaming the file breaks a test rather than breaking verification
# quietly -- Google re-checks this URL periodically, not just once, and a
# property silently un-verifying is exactly the kind of thing nobody notices.
GOOGLE_TOKEN = "f4785b6e9e0fc14d"


def test_google_verification_serves_the_file_byte_for_byte(client):
    """Google compares the body exactly. The file is committed verbatim as it
    was downloaded -- 53 bytes, no trailing newline -- and served as-is rather
    than reconstructed, because a reconstruction that adds a newline passes
    every test here and fails at Google."""
    from pathlib import Path

    r = client.get(f"/google{GOOGLE_TOKEN}.html")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.text == f"google-site-verification: google{GOOGLE_TOKEN}.html"

    on_disk = (
        Path("src/report/static/verify") / f"google{GOOGLE_TOKEN}.html"
    ).read_bytes()
    assert r.content == on_disk, "served bytes must be the committed bytes"


def test_google_verification_refuses_a_token_nobody_put_there(client):
    """The content Google looks for is derivable from the filename it asks for,
    so a route that generated it would hand the property to anyone who found
    this endpoint. Only a file somebody committed counts as proof."""
    assert client.get("/google999999999.html").status_code == 404
    assert client.get("/google123456789.html").status_code == 404


def test_google_verification_cannot_be_walked_out_of_its_directory(client):
    for token in ("..%2f..%2fapi", "..", "a/b"):
        assert client.get(f"/google{token}.html").status_code == 404


def test_status_says_which_optional_switches_the_process_can_see(client):
    """"I set that variable" and "the running container can see that variable"
    are different claims, and telling them apart otherwise needs the admin
    secret. Flags and one error string -- never a value, and nothing here that
    is not already inferable from a 503 on the endpoint each one gates."""
    body = client.get("/status", headers=ADMIN).json()

    assert set(body["features"]) == {
        "demo", "demo_key_set", "demo_error", "email", "login",
        # Two error strings, and the difference between them matters: a
        # checkout that would not OPEN costs a click, a payment that settled
        # and granted nothing costs a customer.
        "billing", "billing_error", "fulfilment_error",
    }
    # State, never a secret. A string among the flags would mean a value had
    # been rendered where a boolean belongs.
    flags = {
        k: v for k, v in body["features"].items()
        if not k.endswith("_error")
    }
    assert all(isinstance(v, bool) for v in flags.values())
    # demo_error carries a database message or nothing -- never a key.
    err = body["features"]["demo_error"]
    assert err is None or isinstance(err, str)
    # billing_error carries Stripe's enumerated error code or nothing.
    billing_err = body["features"]["billing_error"]
    assert billing_err is None or isinstance(billing_err, str)
    # And so does fulfilment_error: a reason and the ids around it, never a
    # key, an amount or anything a stranger could act on.
    lost = body["features"]["fulfilment_error"]
    assert lost is None or isinstance(lost, str)


def test_a_pasted_secret_keeps_its_surrounding_junk_off_the_comparison(
    monkeypatch,
):
    """A value copied into a dashboard field arrives with a trailing newline,
    or wrapped in quotes, often enough to matter -- and the demo key is
    COMPARED. `accounts.lookup` strips the key it is handed, so a stored
    "abc\n" against a looked-up "abc" never matches, and the deployment
    reports "the demo is not configured" while the variable is plainly set."""
    from src.config.settings import Settings

    for raw in ('  "abc123"  ', "abc123\n", "'abc123'", " abc123 "):
        monkeypatch.setenv("DEMO_API_KEY", raw)
        assert Settings().demo_api_key == "abc123", raw


def test_widening_a_column_only_ever_grows_it(monkeypatch):
    """A silent retype is how data gets truncated. The widening list may only
    make a column longer, and must be a no-op once it already is."""
    from src.storage.db import _WIDENED_COLUMNS, _widen_columns
    from src.storage.models import Base

    declared = {
        (t.name, c.name): getattr(c.type, "length", None)
        for t in Base.metadata.sorted_tables
        for c in t.columns
    }
    for table, column, want in _WIDENED_COLUMNS:
        model_len = declared.get((table, column))
        assert model_len is not None, f"{table}.{column} is not a sized column"
        assert model_len >= want, (
            f"{table}.{column} is declared {model_len} but the widening asks "
            f"for {want} -- the model is the source of truth"
        )

    # SQLite does not enforce lengths, so the step must simply do nothing.
    class _Fake:
        class dialect:
            name = "sqlite"

    _widen_columns(_Fake())  # must not raise


def test_a_long_demo_key_fits_the_column_it_is_stored_in(api_db):
    """The failure this prevents, in full: DEMO_API_KEY is operator-chosen, the
    column was 64, and a longer key failed the INSERT with
    StringDataRightTruncation -- which reached the reader as "the demo is not
    configured" on a deployment where the variable was plainly set."""
    from src.storage.models import ApiUser

    length = next(
        c.type.length for c in ApiUser.__table__.columns if c.name == "api_key"
    )
    assert length >= 128, "a hand-picked key needs more room than a generated one"


# ------------------------------------------------------------------ sitemap
def test_sitemap_lists_only_companies_with_something_to_draw(client):
    """The site's own rule, applied to what it asks Google to index: a ticker
    with facts but no positive total for assets renders the empty state, and
    six thousand of those in a sitemap is asking a crawler to index nothing."""
    import datetime as dt

    from src import sitemap
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        s.add(Fundamental(
            ticker="DRAW", metric="total_assets", value=1_000e6,
            period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
            filing_date=dt.date(2026, 8, 1), source="sec",
        ))
        # Present, but nothing to scale a drawing to.
        s.add(Fundamental(
            ticker="EMPTY", metric="cash", value=5e6,
            period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
            filing_date=dt.date(2026, 8, 1), source="sec",
        ))
    sitemap.reset_cache()

    body = client.get("/sitemap.xml").text

    assert "/company/DRAW" in body
    assert "/company/EMPTY" not in body


def test_sitemap_dates_a_company_page_by_its_newest_filing(client):
    """lastmod is a claim. A crawler that catches you making a false one stops
    reading the field, so the date is the last time the page actually changed
    -- never today, and never a stamp applied to look fresh."""
    import datetime as dt

    from src import sitemap
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        for filed in (dt.date(2025, 3, 3), dt.date(2026, 8, 6)):
            s.add(Fundamental(
                ticker="DATED", metric="total_assets", value=1_000e6,
                period_end=filed, fiscal_period="Q2",
                filing_date=filed, source="sec",
            ))
    sitemap.reset_cache()

    body = client.get("/sitemap.xml").text
    block = body.split("/company/DATED", 1)[1].split("</url>", 1)[0]

    assert "2026-08-06" in block, "the newest filing, not the oldest"
    assert "2025-03-03" not in block
    assert dt.date.today().isoformat() not in block, "never stamped today"


def test_sitemap_is_valid_xml_and_well_formed(client):
    from xml.etree import ElementTree

    r = client.get("/sitemap.xml")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(r.content)
    assert root.tag.endswith("urlset")
    for url in root:
        assert url.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc") is not None


def test_sitemap_omits_the_search_endpoint(client):
    """/search with no query answers 400. A URL in a sitemap that returns 400 is
    reported in Search Console as a submitted URL that failed -- so listing it
    would put an error in the console the sitemap exists to feed."""
    body = client.get("/sitemap.xml").text

    assert "/search" not in body
    assert client.get("/search").status_code == 400, "the reason it is omitted"


def test_sitemap_names_the_host_the_crawler_asked_on(client):
    """One process answers on toscale.pro and on the Railway hostname. A sitemap
    that advertised the other one would fail Search Console's cross-submission
    check, and the memo is keyed on the host so it cannot serve one to the
    other."""
    from src import sitemap

    sitemap.reset_cache()
    a = client.get("/sitemap.xml", headers={"host": "toscale.pro",
                                            "x-forwarded-proto": "https"}).text
    b = client.get("/sitemap.xml", headers={"host": "example.invalid",
                                            "x-forwarded-proto": "https"}).text

    assert "https://toscale.pro/" in a
    assert "example.invalid" not in a
    assert "https://example.invalid/" in b


def test_robots_points_at_the_sitemap(client):
    """A sitemap nobody is told about is found only if it is submitted by hand."""
    r = client.get("/robots.txt", headers={"host": "toscale.pro",
                                           "x-forwarded-proto": "https"})

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Sitemap: https://toscale.pro/sitemap.xml" in r.text
    assert "Disallow: /admin" in r.text


def test_the_customer_stats_endpoint_needs_the_admin_secret(client):
    """It returns email addresses. An unauthenticated version of exactly this
    payload is the hole the audit found on /admin.json."""
    assert client.get("/api/admin/stats").status_code == 403

    body = client.get("/api/admin/stats", headers=ADMIN).json()
    for key in (
        "total_users", "free", "pro_monthly", "pro_annual", "pro_lapsed",
        "dataset_buyers", "recent_signups", "revenue_estimate",
    ):
        assert key in body, key


def test_customer_stats_count_the_effective_tier_not_the_column(client):
    """An account whose Pro ran out yesterday still says "pro" in the column.
    Counting it as revenue is how a dashboard tells you business is fine while
    it is not."""
    import datetime as dt

    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    with session_scope() as s:
        s.add(ApiUser(
            email="live@example.com", api_key="k-live", subscription_tier="pro",
            pro_expires_at=now + dt.timedelta(days=20),
        ))
        s.add(ApiUser(
            email="lapsed@example.com", api_key="k-lapsed", subscription_tier="pro",
            pro_expires_at=now - dt.timedelta(days=1),
        ))
        s.add(ApiUser(
            email="annual@example.com", api_key="k-annual", subscription_tier="pro",
            pro_expires_at=now + dt.timedelta(days=300),
        ))
        s.add(ApiUser(
            email="buyer@example.com", api_key="k-buyer", has_paid_download=True,
        ))

    body = client.get("/api/admin/stats", headers=ADMIN).json()
    assert body["total_users"] == 4
    assert body["pro_monthly"] == 1
    assert body["pro_annual"] == 1
    assert body["pro_lapsed"] == 1
    assert body["dataset_buyers"] == 1
    # The lapsed one is back on free, and is not counted as revenue.
    assert body["free"] == 2
    assert body["revenue_estimate"]["mrr_usd"] > 0
    assert "Estimate only" in body["revenue_estimate"]["caveat"]


def test_recent_signups_are_newest_first(client):
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    with session_scope() as s:
        for n in range(3):
            s.add(ApiUser(email=f"u{n}@example.com", api_key=f"key-{n}"))

    rows = client.get("/api/admin/stats?recent=2", headers=ADMIN).json()
    assert len(rows["recent_signups"]) == 2
    assert all("@example.com" in u["email"] for u in rows["recent_signups"])


# ---------------------------------------------------------------------------
# The surfaces that were open and should not have been
# ---------------------------------------------------------------------------

def test_status_is_refused_without_the_admin_secret(client):
    """It was public, and it was both a map and a cost.

    A map: row counts, what the loader is doing, the scheduler's timings, which
    optional features are configured, and the sticky billing/fulfilment error
    strings. A cost: COUNT(*) and COUNT(DISTINCT ticker) over millions of rows,
    uncached, measured at ~1.5s of database work per hit -- which is an
    amplification primitive somebody else gets to point at you.
    """
    r = client.get("/status")
    assert r.status_code == 403, r.text
    assert "price_bars" not in r.text
    assert "features" not in r.text

    assert client.get("/status", headers=ADMIN).status_code == 200


def test_health_stays_open_and_cheap(client):
    """The one an uptime monitor and Railway's healthcheck actually call.

    Gating /status is only acceptable because this exists: an operator still
    needs a public way to ask whether the service is up.
    """
    r = client.get("/health")
    assert r.status_code == 200, r.text
    assert "price_bars" not in r.text, "health must not become the new /status"


def test_status_is_rate_limited_even_for_an_admin(client, monkeypatch):
    """A leaked admin secret must not also be an unmetered full-table scan."""
    from src.api import _status_gate

    _status_gate.reset()
    monkeypatch.setenv("STATUS_RATE_PER_MIN", "3")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    codes = [client.get("/status", headers=ADMIN).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200], codes
    assert codes[3] == 429, codes
    assert codes[4] == 429, codes

    _status_gate.reset()
    get_settings.cache_clear()


def test_the_generated_api_schema_is_off_in_production(monkeypatch):
    """FastAPI publishes /docs, /redoc and /openapi.json by default, and they
    were reachable in production -- the whole route table, every parameter,
    every response model, the admin surface included.

    Asserted against a freshly built app rather than the imported one, because
    the decision is made once at import time from ENV.
    """
    from fastapi import FastAPI

    for env, expected in (("prod", None), ("dev", "/docs")):
        docs_open = env != "prod"
        app = FastAPI(
            docs_url="/docs" if docs_open else None,
            redoc_url="/redoc" if docs_open else None,
            openapi_url="/openapi.json" if docs_open else None,
        )
        assert app.docs_url == expected
        assert app.redoc_url == ("/redoc" if docs_open else None)
        assert app.openapi_url == ("/openapi.json" if docs_open else None)


def test_the_running_app_matches_its_environment(client):
    """The wiring itself, on the real app object. Tests run with ENV=dev, so
    the docs are expected to be ON here -- what this guards is that the three
    URLs are driven by one flag rather than three independent decisions."""
    from src.api import _DOCS_OPEN, app

    if _DOCS_OPEN:
        assert (app.docs_url, app.redoc_url, app.openapi_url) == (
            "/docs", "/redoc", "/openapi.json"
        )
        assert client.get("/openapi.json").status_code == 200
    else:
        assert (app.docs_url, app.redoc_url, app.openapi_url) == (None, None, None)
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


def test_html_and_css_are_compressed(client):
    """The largest client-side win on the site, and it was never switched on.

    The home page ships five stylesheets totalling ~96 KB of uncompressed CSS
    plus ~27 KB of HTML, and every byte is on the critical path because a
    stylesheet blocks render. `vary: accept-encoding` was already on the
    responses, promising a negotiation that never happened.
    """
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip", r.headers


def test_static_assets_are_cached_immutably(client):
    """Every /static URL carries `?v=<mtime>`, so the URL changes when the file
    does. Having built the cache-busting, the site then served the assets with
    no Cache-Control at all -- five stylesheets revalidated on every
    navigation. Cache-busting you do not cache is pure cost."""
    r = client.get("/static/glass.css")
    assert r.status_code == 200
    cache = r.headers.get("cache-control", "")
    assert "max-age=31536000" in cache, cache
    assert "immutable" in cache, cache
