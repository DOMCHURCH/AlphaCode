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
def test_root_is_the_home_page(client):
    """/ used to bounce to /admin. The front door is the product now; the admin
    tool is reachable from it rather than standing in for it."""
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Filed financial statements, drawn at true proportion" in r.text
    assert 'action="/search"' in r.text
    assert 'href="/admin"' in r.text


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
    body = client.get("/admin/universe-check").json()

    assert body["identity"]["checkable"] == 2
    assert body["identity"]["buckets"]["within_1pct"] == 1
    assert body["identity"]["buckets"]["over_10pct"] == 1
    assert body["worst"][0]["ticker"] == "BAD1"
    assert "by_sector" in body


def test_universe_check_survives_an_empty_table(client):
    body = client.get("/admin/universe-check").json()
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
    msft = client.get("/admin/verify").json()["companies"]["MSFT"]

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
    body = client.get("/admin/verify").json()

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
    col = cols.index('<div class="bs-col">')

    assert cols.count('<div class="bs-cap">') == 2
    assert cols.count('<div class="bs-col">') == 2
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

    html = Path("src/report/templates/admin.html").read_text()
    presets = re.findall(r'<button[^>]*class="chipbtn"[^>]*>', html)
    assert presets, "the preset buttons must exist"
    for p in presets:
        assert "data-ticker=" in p, p
        assert "data-tags=" in p, p

    js = Path("src/report/static/admin.js").read_text()
    # The handler submits directly; it must not write into the form inputs.
    handler = js[js.index('$("rawPresets")'):]
    handler = handler[:handler.index("});")]
    assert "submitRawFacts(" in handler
    assert '$("rawTicker").value =' not in handler
    assert '$("rawTags").value =' not in handler
