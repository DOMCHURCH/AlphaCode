"""The exceptions feed: failed checks and restatements across every company."""

from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient

TODAY = dt.date.today()
RECENT = TODAY - dt.timedelta(days=20)
OLD = TODAY - dt.timedelta(days=400)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ex.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-ex")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    from src.company import exceptions
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    exceptions.reset_cache()
    init_db()
    _seed()
    from src.api import _register_gate, app

    _register_gate.reset()
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    exceptions.reset_cache()
    get_settings.cache_clear()
    reset_engine_cache()


def _seed():
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    def fact(ticker, metric, value, period, filed):
        return Fundamental(ticker=ticker, metric=metric, value=value, period_end=period,
                           fiscal_period="Q2", filing_date=filed, source="sec")

    p_recent = RECENT - dt.timedelta(days=40)
    p_old = OLD - dt.timedelta(days=40)
    with session_scope() as s:
        # OK: balances.
        for t, p, f in (("GOOD", p_recent, RECENT),):
            s.add_all([fact(t, "total_assets", 100e6, p, f),
                       fact(t, "total_liabilities", 60e6, p, f),
                       fact(t, "total_equity", 40e6, p, f)])
        # BRKN: the filer's own stated total disagrees with its assets.
        s.add_all([fact("BRKN", "total_assets", 100e6, p_recent, RECENT),
                   fact("BRKN", "total_liabilities", 50e6, p_recent, RECENT),
                   fact("BRKN", "total_equity", 30e6, p_recent, RECENT),
                   fact("BRKN", "liabilities_and_equity", 80e6, p_recent, RECENT)])
        # OLDB: broken too, but filed 400 days ago.
        s.add_all([fact("OLDB", "total_assets", 100e6, p_old, OLD),
                   fact("OLDB", "total_liabilities", 50e6, p_old, OLD),
                   fact("OLDB", "total_equity", 30e6, p_old, OLD),
                   fact("OLDB", "liabilities_and_equity", 80e6, p_old, OLD)])
        # RSTD: revenue restated from 50m to 45m by a later filing.
        s.add_all([fact("RSTD", "revenue", 50e6, p_old, OLD),
                   fact("RSTD", "revenue", 45e6, p_old, RECENT),
                   # An equal re-report is not a restatement.
                   fact("RSTD", "net_income", 5e6, p_old, OLD),
                   fact("RSTD", "net_income", 5e6, p_old, RECENT)])


def _key(client, email, tier=None):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
    assert r.status_code == 201, r.text
    if tier:
        from src import accounts

        accounts.apply_admin_action(email, f"grant_{tier}")
    return {"X-API-Key": r.json()["api_key"]}


def test_business_sees_every_event(client):
    h = _key(client, "b@example.com", "business")
    body = client.get("/api/exceptions", headers=h).json()
    kinds = {(e["type"], e["ticker"]) for e in body["events"]}
    assert kinds == {("failed_check", "BRKN"), ("failed_check", "OLDB"),
                     ("restatement", "RSTD")}
    broken = next(e for e in body["events"] if e["ticker"] == "BRKN")
    assert broken["category"] == "broken"
    assert broken["attribution"] == "filer"
    r = next(e for e in body["events"] if e["type"] == "restatement")
    assert (r["metric"], r["previous"], r["revised"]) == ("revenue", 50e6, 45e6)
    assert r["change_pct"] == pytest.approx(-10.0)


def test_pro_sees_ninety_days(client):
    h = _key(client, "p@example.com", "pro")
    body = client.get("/api/exceptions?since=2000-01-01", headers=h).json()
    assert body["days_allowed"] == 90
    assert "OLDB" not in {e["ticker"] for e in body["events"]}
    assert "BRKN" in {e["ticker"] for e in body["events"]}


def test_starter_is_refused_with_the_plan_that_unlocks_it(client):
    h = _key(client, "s@example.com", "starter")
    r = client.get("/api/exceptions", headers=h)
    assert r.status_code == 403
    assert r.json()["detail"]["required_plan"] == "Pro"


def test_filters_and_csv(client):
    h = _key(client, "c@example.com", "business")
    only = client.get("/api/exceptions?type=restatement", headers=h).json()
    assert {e["type"] for e in only["events"]} == {"restatement"}
    r = client.get("/api/exceptions?format=csv&type=failed_check", headers=h)
    assert r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {row["ticker"] for row in rows} == {"BRKN", "OLDB"}
    assert rows[0]["attribution"] == "filer"
