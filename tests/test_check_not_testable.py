"""A balance check on a filing that cannot be tested must not pass.

`View1.balances` defaults to True and `_check_identity` returns early when a
term is missing, so a filing that reports total assets and nothing to check
them against came back as "balances ... within tolerance" over MCP and as
`reconciles: true` over REST. The site's own counts already called these
filings not_testable (`stats.classify_identity`); the two surfaces now ask the
same function.

Shapes: total assets only, assets + liabilities only, assets + equity only,
and one fully reported filing that must still pass.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

DEMO_KEY = "demo-key-for-not-testable-0123456789"
PERIOD = dt.date(2026, 6, 30)
FILED = dt.date(2026, 8, 1)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nt.db'}")
    monkeypatch.setenv("DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-testable")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "1000")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "10000")
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "0")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import _demo_gate, _demo_ip_gate, _register_gate, app

    _demo_ip_gate.reset()
    _demo_gate.reset()
    _register_gate.reset()
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    _demo_ip_gate.reset()
    _demo_gate.reset()
    get_settings.cache_clear()
    reset_engine_cache()


def _seed(ticker: str, **metrics: float) -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Co"))
        for metric, value in metrics.items():
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=value, period_end=PERIOD,
                fiscal_period="Q2", filing_date=FILED, source="sec",
            ))


def _mcp(client, ticker: str) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "check_balance_sheet", "arguments": {"ticker": ticker}}}
    r = client.post("/mcp", json=body, headers={"Accept": "application/json, text/event-stream"})
    return r.json()["result"]


def _text(result: dict) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def _pro_headers(client) -> dict:
    from src import accounts

    r = client.post("/api/auth/register", json={"email": "nt@example.com", "accept_terms": True})
    assert r.status_code == 201, r.text
    accounts.apply_admin_action("nt@example.com", "grant_pro")
    return {"X-API-Key": r.json()["api_key"]}


def _verify(client, *tickers: str) -> dict:
    r = client.post("/api/verify", json={"tickers": list(tickers)}, headers=_pro_headers(client))
    assert r.status_code == 200, r.text
    return r.json()


# (ticker, metrics, what the answer must name as missing)
UNTESTABLE = [
    ("ONLYA", {"total_assets": 1000.0}, ["total liabilities", "total equity"]),
    ("ALONLY", {"total_assets": 1000.0, "total_liabilities": 600.0}, ["total equity"]),
    ("AEONLY", {"total_assets": 1000.0, "total_equity": 400.0}, ["total liabilities"]),
]


@pytest.mark.parametrize("ticker,metrics,missing", UNTESTABLE)
def test_mcp_says_not_testable_and_names_what_is_missing(client, ticker, metrics, missing):
    _seed(ticker, **metrics)

    result = _mcp(client, ticker)
    text = _text(result)

    assert result.get("isError") is False
    assert "not_testable" in text
    assert "within tolerance" not in text
    assert "DOES NOT balance" not in text
    for line in missing:
        assert line in text
    assert "liabilities plus equity" in text  # and why: no stated total either
    structured = result["structuredContent"]
    assert structured["identity_status"] == "not_testable"
    assert structured["balances"] is None
    assert structured["missing_for_check"] == missing


@pytest.mark.parametrize("ticker,metrics,missing", UNTESTABLE)
def test_rest_says_not_testable_not_reconciles(client, ticker, metrics, missing):
    _seed(ticker, **metrics)

    body = _verify(client, ticker)
    row = body["results"][0]

    assert row["reconciles"] is None
    assert row["identity_status"] == "not_testable"
    assert row["missing_for_check"] == missing
    assert body["not_testable"] == 1
    assert body["reconcile"] == 0
    assert body["do_not_reconcile"] == 0


@pytest.mark.parametrize("ticker,metrics,missing", UNTESTABLE)
def test_a_not_testable_answer_is_billed_like_any_other_answer(client, ticker, metrics, missing):
    """REST bills every found ticker once, whatever the verdict; so does MCP."""
    _seed(ticker, **metrics)
    h = _pro_headers(client)
    before = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]

    r = client.post("/api/verify", json={"tickers": [ticker]}, headers=h)
    assert r.status_code == 200

    after = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    assert after - before == 1


def test_a_fully_reported_filing_still_balances(client):
    _seed("FULLY", total_assets=1000.0, total_liabilities=600.0, total_equity=400.0)

    result = _mcp(client, "FULLY")
    text = _text(result)
    assert result.get("isError") is False
    assert "balances for the period ending" in text
    assert "within tolerance" in text
    assert "not_testable" not in text
    structured = result["structuredContent"]
    assert structured["balances"] is True
    assert structured["identity_status"] == "balanced"
    assert structured["missing_for_check"] == []

    body = _verify(client, "FULLY")
    row = body["results"][0]
    assert row["reconciles"] is True
    assert row["identity_status"] == "balanced"
    assert body["reconcile"] == 1 and body["not_testable"] == 0


def test_a_stated_total_alone_is_a_test_and_a_disagreeing_one_fails(client):
    """The site tests assets against the filer's own stated total when no
    components are reported (`classify_identity`). That test can fail, and
    the old early return hid the failure behind `balances: true`."""
    _seed("STATOK", total_assets=1000.0, liabilities_and_equity=1000.0)
    _seed("STATBAD", total_assets=1000.0, liabilities_and_equity=800.0)

    ok, bad = _mcp(client, "STATOK"), _mcp(client, "STATBAD")

    assert ok["structuredContent"]["identity_status"] == "balanced"
    assert ok["structuredContent"]["balances"] is True
    assert bad["structuredContent"]["identity_status"] in {"broken", "rounding"}
    assert bad["structuredContent"]["balances"] is False
    assert "within tolerance" not in _text(bad)
    assert "not_testable" not in _text(bad)


def test_mcp_and_rest_use_the_sites_classifier(client):
    """Whatever `classify_identity` says for the stored metrics is what both say."""
    from src.company.stats import classify_identity

    shapes = {
        "ONLYA": {"total_assets": 1000.0},
        "ALONLY": {"total_assets": 1000.0, "total_liabilities": 600.0},
        "AEONLY": {"total_assets": 1000.0, "total_equity": 400.0},
        "FULLY": {"total_assets": 1000.0, "total_liabilities": 600.0, "total_equity": 400.0},
        "STATBAD": {"total_assets": 1000.0, "liabilities_and_equity": 800.0},
    }
    for t, m in shapes.items():
        _seed(t, **m)
    body = _verify(client, *shapes)
    for row in body["results"]:
        expected, _ = classify_identity(shapes[row["ticker"]])
        assert row["identity_status"] == expected, row["ticker"]
        assert _mcp(client, row["ticker"])["structuredContent"]["identity_status"] == expected


# ---------------------------------------------------------------------------
# Wording: nothing a model reads promises a check that cannot happen
# ---------------------------------------------------------------------------

SENTENCE = (
    "Each filing is checked against Assets = Liabilities + Equity when it "
    "reports the lines needed; where it does not, it is marked not testable "
    "instead of passing."
)


def test_server_instructions_carry_the_not_testable_sentence():
    from src.mcp_server import _INSTRUCTIONS

    assert SENTENCE in _INSTRUCTIONS


def test_check_balance_sheet_description_does_not_promise_every_filing_balances():
    from src.mcp_server import TOOLS

    desc = next(t for t in TOOLS if t["name"] == "check_balance_sheet")["description"]
    assert "not_testable" in desc
    assert "neither a pass nor a failure" in desc
    assert not desc.startswith("Verify that")


def test_statements_text_says_not_testable_when_no_check_could_run(client, monkeypatch):
    """`checks 0/0 passed` read as a clean sheet. It must say nothing was tested."""
    _seed("STMT", total_assets=1000.0)
    canned = {
        "ticker": "STMT", "period": "annual", "as_of": None, "years_requested": None,
        "available_from": "2025-12-31",
        "periods": [{
            "period_end": "2025-12-31", "fiscal_period": "FY",
            "income_statement": {"revenue": 10.0}, "cash_flow": {},
            "checks": [{"check": "gross_profit", "rule": "r", "status": "not_testable"}],
            "derived": [],
        }],
        "checks_summary": {"tested": 0, "passed": 0, "failed": 0},
        "source": "x",
    }
    monkeypatch.setattr("src.company.statements.statements", lambda *a, **k: canned)

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_financial_statements", "arguments": {"ticker": "STMT"}}}
    result = client.post("/mcp", json=body, headers={"Accept": "application/json"}).json()["result"]
    text = _text(result)

    assert result.get("isError") is False, text
    assert "0/0 passed" not in text
    assert "not_testable" in text


# ---------------------------------------------------------------------------
# The published schema: reconciles can be null, and the new fields are described
# ---------------------------------------------------------------------------


def test_openapi_marks_reconciles_nullable_and_documents_the_new_fields():
    from src.api import app

    schemas = app.openapi()["components"]["schemas"]
    row = schemas["BulkVerifyRow"]["properties"]

    kinds = {b.get("type") for b in row["reconciles"]["anyOf"]}
    assert kinds == {"boolean", "null"}
    assert "null" in row["reconciles"]["description"]
    for name in ("identity_status", "missing_for_check"):
        assert row[name].get("description"), name
    statuses = next(b for b in row["identity_status"]["anyOf"] if "enum" in b)["enum"]
    assert "not_testable" in statuses

    totals = schemas["BulkVerifyResponse"]["properties"]
    assert "not_testable" in totals
    assert app.openapi()["paths"]["/api/verify"]["post"]["responses"]["200"]["content"][
        "application/json"]["schema"]["$ref"].endswith("BulkVerifyResponse")


def test_the_live_response_validates_against_the_published_schema(client):
    from src.api import BulkVerifyResponse

    _seed("ONLYA", total_assets=1000.0)
    _seed("FULLY", total_assets=1000.0, total_liabilities=600.0, total_equity=400.0)
    body = _verify(client, "ONLYA", "FULLY", "NOPE")

    parsed = BulkVerifyResponse.model_validate(body)

    assert [r.reconciles for r in parsed.results] == [None, True, None]
    assert parsed.results[2].found is False
    assert (parsed.reconcile, parsed.do_not_reconcile, parsed.not_testable, parsed.not_found) == (1, 0, 1, 1)
