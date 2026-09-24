"""Stage A of the paid tiers (2026-09-23): history depth and "what changed".

The plan matrix lives in `src/plans.py`; these tests pin that the gates read
it -- Free gets one year and no changes, Starter gets five years and changes
-- and that a restatement already sitting in `fundamentals` is reported.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

TODAY = dt.date.today()


def _q(years_ago: float) -> dt.date:
    return TODAY - dt.timedelta(days=int(years_ago * 365.25))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-plans")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "100")
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()
    _seed()
    from src.api import _register_gate, app

    _register_gate.reset()
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()
    reset_engine_cache()


def _seed():
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    def fact(metric, value, period, filed):
        return Fundamental(ticker="TST", metric=metric, value=value,
                           period_end=period, fiscal_period="Q",
                           filing_date=filed, source="sec")

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=TODAY, ticker="TST", name="Test Co"))
        # Four periods: 0.2, 0.7, 2.5 and 4.5 years ago. A = L + E on each.
        for years, assets in ((0.2, 1000.0), (0.7, 900.0), (2.5, 700.0), (4.5, 500.0)):
            p = _q(years)
            filed = p + dt.timedelta(days=40)
            s.add(fact("total_assets", assets, p, filed))
            s.add(fact("total_liabilities", assets * 0.6, p, filed))
            s.add(fact("total_equity", assets * 0.4, p, filed))
        # The 0.7-year period's liabilities were later RESTATED in the newest
        # filing: 540 originally, 560 revised (equity 360 -> 340).
        p, newest = _q(0.7), _q(0.2) + dt.timedelta(days=40)
        s.add(fact("total_liabilities", 560.0, p, newest))
        s.add(fact("total_equity", 340.0, p, newest))


def _key(client, email, tier=None):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
    assert r.status_code == 201, r.text
    if tier:
        from src import accounts

        accounts.apply_admin_action(email, f"grant_{tier}")
    return {"X-API-Key": r.json()["api_key"]}


def test_the_plan_list_is_public(client):
    body = client.get("/api/plans").json()
    features = {f["feature"]: f for f in body["features"]}
    assert features["history_years"]["free"] == 1
    assert features["history_years"]["starter"] == 5
    assert features["changes"]["free"] is False
    assert body["monthly_calls"]["free"] == 100


def test_free_history_is_one_year_whatever_is_asked(client):
    h = _key(client, "free@example.com")
    body = client.get("/api/company/TST/history?years=10", headers=h).json()
    assert body["years_allowed"] == 1
    assert body["periods"] == 2  # 0.2 and 0.7 years ago
    assert body["available_from"] == _q(4.5).isoformat()


def test_starter_history_reaches_five_years(client):
    h = _key(client, "starter@example.com", "starter")
    body = client.get("/api/company/TST/history", headers=h).json()
    assert body["years_allowed"] == 5 and body["periods"] == 4
    # Newest first, and each period is the reconciled latest-filing figure.
    ends = [b["period_end"] for b in body["balance_sheets"]]
    assert ends == sorted(ends, reverse=True)


def test_changes_are_refused_on_free_with_the_plan_that_unlocks_them(client):
    h = _key(client, "free2@example.com")
    r = client.get("/api/company/TST/changes", headers=h)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "plan_required" and detail["required_plan"] == "Starter"


def test_changes_report_the_restatement_and_the_period_move(client):
    h = _key(client, "starter2@example.com", "starter")
    body = client.get("/api/company/TST/changes", headers=h).json()
    move = body["since_previous_period"]["total_assets"]
    assert move["previous"] == 900.0 and move["current"] == 1000.0
    assert move["change_pct"] == pytest.approx(11.11, abs=0.01)
    restated = {(r["metric"], r["period_end"]): r for r in body["restatements"]}
    liab = restated[("total_liabilities", _q(0.7).isoformat())]
    assert liab["previous"] == 540.0 and liab["current"] == 560.0
    # Equal comparatives are not restatements.
    assert ("total_assets", _q(0.7).isoformat()) not in restated


def test_the_new_endpoints_spend_one_call_each(client):
    h = _key(client, "meter@example.com", "starter")
    before = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    client.get("/api/company/TST/history", headers=h)
    client.get("/api/company/TST/changes", headers=h)
    after = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    assert after - before == 2


def test_mcp_lists_the_new_tools(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"get_balance_sheet_history", "get_balance_sheet_changes"} <= names
