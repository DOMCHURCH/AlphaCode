"""API surface tests.

The api is a deploy surface (Railway `api` service), so its guard and its
DB-backed report serving get real coverage here. Uses a file-backed DB shared
between the seeding and the app, and FastAPI's TestClient.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

AS_OF = dt.date(2025, 6, 2)


@pytest.fixture
def api_db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
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

    from src.api import app

    with TestClient(app) as c:
        yield c


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] == "ok"


def test_run_is_open_when_api_key_unset(client):
    # skip_llm keeps it cheap; the background task may fail on missing data keys,
    # but the endpoint itself must accept the request when API_KEY is unset.
    r = client.post("/run?skip_llm=true")
    assert r.status_code == 200
    assert r.json()["accepted"] in (True, False)  # accepted, or "already running"


def test_run_requires_key_when_api_key_set(client, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("API_KEY", "s3cret")
    get_settings.cache_clear()

    assert client.post("/run?skip_llm=true").status_code == 401
    assert client.post(
        "/run?skip_llm=true", headers={"X-API-Key": "wrong"}
    ).status_code == 401
    assert client.post(
        "/run?skip_llm=true", headers={"X-API-Key": "s3cret"}
    ).status_code == 200

    # Reads stay open regardless of the key.
    assert client.get("/validation").status_code == 200


def test_report_served_from_db(client, tmp_path):
    """A report rendered by the (worker's) build_report is served by the api out
    of the DB, even with no report file on the api's disk."""
    import shutil

    from src.catalysts.macro import MacroState
    from src.llm.schemas import DeepDive
    from src.report.builder import build_report
    from src.storage.db import session_scope

    dive = DeepDive.model_validate(
        {
            "ticker": "NVDA", "total_score": 0,
            "subscores": {"trend": 20, "fundamental": 15, "catalyst": 12,
                          "news": 10, "macro": 7, "risk": 8},
            "thesis": "A" * 100,
            "bull_case": "Structural datacenter demand keeps compounding here.",
            "bear_case": "A rich multiple leaves no room for a growth wobble.",
            "invalidation": "a daily close below $142 (the SMA200)",
            "time_horizon_days": 60, "conviction": "high",
            "key_risks": ["concentration"], "catalysts_ahead": [],
        }
    )
    with session_scope() as s:
        build_report(
            s, as_of=AS_OF, run_id="api-r", dives=[dive],
            scores=pd.DataFrame(
                {"sector": ["Technology"], "factor_composite": [1.4],
                 "data_completeness": [0.9], "completeness_momentum": [1.0],
                 "completeness_quality": [0.8], "completeness_revisions": [1.0],
                 "completeness_pead": [1.0], "completeness_value": [0.6]},
                index=["NVDA"]),
            trend_features=pd.DataFrame(
                {"high_52w": [204.0], "low_52w": [100.0], "close": [200.0]},
                index=["NVDA"]),
            detail={"NVDA": {}}, macro=MacroState(regime="RISK_ON", score=2.0),
            funnel_counts={"Stage 0 universe": 6000, "Stage 5 final": 1},
            funnel_rejects={}, stage_sectors={"Stage 0": {"Technology": 6000}},
            near_misses=[], api_calls={},
            cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "by_stage": {}},
            duration_s=1.0, output_dir=str(tmp_path / "reports"),
        )

    # Wipe the disk: the api must serve from the DB.
    shutil.rmtree(tmp_path / "reports", ignore_errors=True)

    h = client.get(f"/report/{AS_OF.isoformat()}/html")
    assert h.status_code == 200
    assert "NVDA" in h.text
    p = client.get(f"/report/{AS_OF.isoformat()}/pdf")
    # PDF present only if weasyprint is installed; either a valid PDF or a 404.
    if p.status_code == 200:
        assert p.headers["content-type"] == "application/pdf"
        assert p.content[:5] == b"%PDF-"
    else:
        assert p.status_code == 404


def test_unknown_date_is_404(client):
    assert client.get("/report/2019-01-01").status_code == 404
    assert client.get("/report/not-a-date").status_code == 400
