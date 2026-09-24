"""`as_of=`: the figures exactly as they were public on a past date.

The product claim a backtest rests on. Two filings for one period -- the
original and a restatement -- must come back as the original before the
restatement was filed and the revision after it. Before 2026-09-24
`get_balance_sheet(as_of=)` filtered on period end only, so a later filing
leaked into an earlier answer.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

TODAY = dt.date.today()
PERIOD = TODAY - dt.timedelta(days=300)
ORIGINAL_FILED = PERIOD + dt.timedelta(days=40)
RESTATED_FILED = TODAY - dt.timedelta(days=30)
NEWER_PERIOD = TODAY - dt.timedelta(days=90)
NEWER_FILED = TODAY - dt.timedelta(days=45)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pit.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-pit")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
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
        return Fundamental(ticker="PIT", metric=metric, value=value,
                           period_end=period, fiscal_period="Q3",
                           filing_date=filed, source="sec")

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=TODAY, ticker="PIT", name="Point In Time Co"))
        s.add(fact("total_assets", 1000.0, PERIOD, ORIGINAL_FILED))
        s.add(fact("total_liabilities", 600.0, PERIOD, ORIGINAL_FILED))
        s.add(fact("total_equity", 400.0, PERIOD, ORIGINAL_FILED))
        # Restated a year later: liabilities 600 -> 650, equity 400 -> 350.
        s.add(fact("total_liabilities", 650.0, PERIOD, RESTATED_FILED))
        s.add(fact("total_equity", 350.0, PERIOD, RESTATED_FILED))
        # A newer quarter, filed between the two.
        s.add(fact("total_assets", 1100.0, NEWER_PERIOD, NEWER_FILED))
        s.add(fact("total_liabilities", 660.0, NEWER_PERIOD, NEWER_FILED))
        s.add(fact("total_equity", 440.0, NEWER_PERIOD, NEWER_FILED))


def _key(client, email, tier=None):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
    assert r.status_code == 201, r.text
    if tier:
        from src import accounts

        accounts.apply_admin_action(email, f"grant_{tier}")
    return {"X-API-Key": r.json()["api_key"]}


def _liabilities(sheet):
    return sheet["liabilities"]["total_liabilities"]["value"]


def test_the_original_figure_before_the_restatement_was_filed(client):
    h = _key(client, "pro@example.com", "pro")
    day = (ORIGINAL_FILED + dt.timedelta(days=5)).isoformat()
    r = client.get(f"/api/company/PIT?as_of={day}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period_end"] == PERIOD.isoformat()
    assert _liabilities(body) == 600.0
    assert body["as_of"] == day
    # The company name is still known for a date before the universe snapshot.
    assert body["company_name"] == "Point In Time Co"


def test_a_newer_quarter_appears_only_once_it_was_filed(client):
    h = _key(client, "pro2@example.com", "pro")
    before = (NEWER_FILED - dt.timedelta(days=1)).isoformat()
    after = (NEWER_FILED + dt.timedelta(days=1)).isoformat()
    assert client.get(f"/api/company/PIT?as_of={before}", headers=h).json()["period_end"] == PERIOD.isoformat()
    assert client.get(f"/api/company/PIT?as_of={after}", headers=h).json()["period_end"] == NEWER_PERIOD.isoformat()


def test_history_as_of_shows_the_figures_known_then(client):
    h = _key(client, "pro3@example.com", "pro")
    before = (RESTATED_FILED - dt.timedelta(days=1)).isoformat()
    body = client.get(f"/api/company/PIT/history?as_of={before}", headers=h).json()
    by_period = {s["period_end"]: s for s in body["balance_sheets"]}
    assert _liabilities(by_period[PERIOD.isoformat()]) == 600.0
    now = client.get("/api/company/PIT/history", headers=h).json()
    by_period = {s["period_end"]: s for s in now["balance_sheets"]}
    assert _liabilities(by_period[PERIOD.isoformat()]) == 650.0


def test_before_any_filing_says_where_the_data_starts(client):
    h = _key(client, "pro4@example.com", "pro")
    r = client.get("/api/company/PIT?as_of=2001-01-01", headers=h)
    assert r.status_code == 404
    assert ORIGINAL_FILED.isoformat() in r.json()["detail"]


def test_as_of_is_a_pro_feature(client):
    h = _key(client, "starter@example.com", "starter")
    r = client.get(f"/api/company/PIT?as_of={ORIGINAL_FILED.isoformat()}", headers=h)
    assert r.status_code == 403
    assert r.json()["detail"]["required_plan"] == "Pro"
    # Without as_of the same account still reads today's figures.
    assert client.get("/api/company/PIT", headers=h).status_code == 200


def test_a_future_as_of_is_refused(client):
    h = _key(client, "pro5@example.com", "pro")
    tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
    assert client.get(f"/api/company/PIT?as_of={tomorrow}", headers=h).status_code == 422
