"""Income statement and cash flow: derivation from filed figures, and checks.

One fiscal year shaped like a real filer's: a 10-K (FY), three 10-Qs whose
income statements are quarterly but whose cash flow is year-to-date only.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

FYE = dt.date(2025, 12, 31)
Q1, Q2, Q3 = dt.date(2025, 3, 31), dt.date(2025, 6, 30), dt.date(2025, 9, 30)
FILED = {Q1: dt.date(2025, 5, 1), Q2: dt.date(2025, 8, 1), Q3: dt.date(2025, 11, 1),
         FYE: dt.date(2026, 2, 15)}
FP = {Q1: "Q1", Q2: "Q2", Q3: "Q3", FYE: "FY"}
M = 1_000_000.0


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'st.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-st")
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

    rows = []

    def add(metric, period, value):
        # In millions: a filer's numbers are dollars, and the check tolerance
        # ($1k floor) is sized for dollars.
        rows.append(Fundamental(ticker="STM", metric=metric, value=value * M, period_end=period,
                                fiscal_period=FP[period], filing_date=FILED[period],
                                source="sec"))

    # Income: quarterly for Q1-Q3, the year in the 10-K. Q4 revenue = 1300.
    for p, rev in ((Q1, 1000.0), (Q2, 1100.0), (Q3, 1200.0), (FYE, 4600.0)):
        add("revenue", p, rev)
        add("cogs", p, rev * 0.6)
        add("gross_profit", p, rev * 0.4)
    # Cash flow: Q1 is a quarter, Q2/Q3 are year-to-date, FY is the year.
    add("operating_cash_flow", Q1, 200.0)
    add("investing_cash_flow", Q1, -50.0)
    add("financing_cash_flow", Q1, -100.0)
    add("cash_change", Q1, 50.0)
    for p, cfo in ((Q2, 420.0), (Q3, 650.0)):
        add("operating_cash_flow_ytd", p, cfo)
    add("operating_cash_flow", FYE, 900.0)
    add("investing_cash_flow", FYE, -300.0)
    add("financing_cash_flow", FYE, -400.0)
    # Deliberately wrong: 900 - 300 - 400 = 200, filed as 260.
    add("cash_change", FYE, 260.0)
    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker="STM", name="Statement Co"))
        s.add_all(rows)


def _key(client, email, tier=None):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
    assert r.status_code == 201, r.text
    if tier:
        from src import accounts

        accounts.apply_admin_action(email, f"grant_{tier}")
    return {"X-API-Key": r.json()["api_key"]}


def _by_period(body):
    return {p["period_end"]: p for p in body["periods"]}


def test_annual_statement_and_its_checks(client):
    h = _key(client, "a@example.com", "business")
    body = client.get("/api/company/STM/statements?period=annual", headers=h).json()
    fy = _by_period(body)[FYE.isoformat()]
    assert fy["fiscal_period"] == "FY"
    assert fy["income_statement"]["revenue"] == 4600.0 * M
    checks = {c["check"]: c for c in fy["checks"]}
    assert checks["gross_profit"]["status"] == "passed"
    # The filed change in cash does not match its own parts: reported, with the gap.
    assert checks["cash_flow"]["status"] == "failed"
    assert checks["cash_flow"]["gap"] == pytest.approx(-60.0 * M)


def test_quarters_are_derived_from_year_and_year_to_date(client):
    h = _key(client, "q@example.com", "business")
    body = client.get("/api/company/STM/statements?period=quarterly", headers=h).json()
    p = _by_period(body)
    # Q4 revenue: the year minus the three filed quarters.
    q4 = p[FYE.isoformat()]
    assert q4["fiscal_period"] == "Q4"
    assert q4["income_statement"]["revenue"] == pytest.approx(1300.0 * M)
    assert "revenue" in q4["derived"]
    # Cash flow: Q2 = 6M - Q1, Q3 = 9M - 6M, Q4 = FY - 9M.
    assert p[Q2.isoformat()]["cash_flow"]["operating_cash_flow"] == pytest.approx(220.0 * M)
    assert p[Q3.isoformat()]["cash_flow"]["operating_cash_flow"] == pytest.approx(230.0 * M)
    assert q4["cash_flow"]["operating_cash_flow"] == pytest.approx(250.0 * M)
    # Filed quarters are not marked derived.
    assert p[Q1.isoformat()]["derived"] == []
    assert p[Q1.isoformat()]["income_statement"]["revenue"] == 1000.0 * M
    q1_checks = {c["check"]: c["status"] for c in p[Q1.isoformat()]["checks"]}
    assert q1_checks == {"gross_profit": "passed", "cash_flow": "passed"}


def test_a_check_with_a_missing_figure_says_which(client):
    h = _key(client, "m@example.com", "business")
    body = client.get("/api/company/STM/statements?period=quarterly", headers=h).json()
    q2 = _by_period(body)[Q2.isoformat()]
    cf = {c["check"]: c for c in q2["checks"]}["cash_flow"]
    assert cf["status"] == "not_testable"
    assert "cash_change" in cf["missing"]


def test_statements_as_of_before_the_10k(client):
    h = _key(client, "p@example.com", "pro")
    day = (FILED[FYE] - dt.timedelta(days=1)).isoformat()
    body = client.get(f"/api/company/STM/statements?period=annual&as_of={day}",
                      headers=h)
    # The 10-K was not public yet, and nothing annual was.
    assert body.status_code == 404


def test_free_plan_reads_a_year_back(client):
    h = _key(client, "f@example.com")
    r = client.get("/api/company/STM/statements?period=quarterly&years=10", headers=h)
    assert r.status_code == 200
    assert r.json()["years_allowed"] == 1
