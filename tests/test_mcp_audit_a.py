"""Audit batch A, MCP: a tool that could not answer says `isError`, and never
hands a caller the text of an internal exception.

`_tool_result`'s own contract is that every failure a user could act on is
reported as `isError: true`, so a model relays it as a failure. Three tools
(changes, statements, exceptions) returned their plan refusals, bad dates and
not-found cases as ordinary results, which a model reads as a successful answer.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

DEMO_KEY = "demo-key-for-audit-a-tests-0123456789"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "mcp_audit_a.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "1000")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "10000")
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "0")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import _demo_gate, _demo_ip_gate, app

    _demo_ip_gate.reset()
    _demo_gate.reset()
    with TestClient(app) as c:
        yield c

    _demo_ip_gate.reset()
    _demo_gate.reset()
    get_settings.cache_clear()
    reset_engine_cache()


def pro_key() -> str:
    from src import accounts

    account, key = accounts.register("pro-audit-a@example.com")
    accounts.apply_admin_action(account.email, "grant_pro")
    return key


def call(client, name, key=None, **arguments):
    headers = {"Authorization": "Bearer " + key} if key else {}
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).json()["result"]


def text(result) -> str:
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


# ---------------------------------------------------------------------------
# get_balance_sheet_changes
# ---------------------------------------------------------------------------

def test_changes_without_a_ticker_is_an_error(client):
    r = call(client, "get_balance_sheet_changes")
    assert r["isError"] is True
    assert "No ticker given" in text(r)


def test_changes_refused_by_the_plan_is_an_error_that_names_the_plan(client):
    r = call(client, "get_balance_sheet_changes", ticker="AAPL")
    assert r["isError"] is True
    assert "What changed" in text(r)
    assert "Starter" in text(r)
    assert "https://balanceproof.dev/pricing" in text(r)
    assert "structuredContent" not in r


def test_changes_for_a_company_with_no_filings_is_an_error_that_says_what_to_do(client):
    r = call(client, "get_balance_sheet_changes", key=pro_key(), ticker="NOSUCH")
    assert r["isError"] is True
    assert "NOSUCH" in text(r)
    assert "search_companies" in text(r)


# ---------------------------------------------------------------------------
# get_financial_statements
# ---------------------------------------------------------------------------

def test_statements_with_a_malformed_as_of_is_an_error(client):
    r = call(client, "get_financial_statements", key=pro_key(), ticker="AAPL", as_of="not-a-date")
    assert r["isError"] is True
    assert "YYYY-MM-DD" in text(r)


def test_statements_as_of_refused_by_the_plan_is_an_error(client):
    r = call(client, "get_financial_statements", ticker="AAPL", as_of="2024-01-02")
    assert r["isError"] is True
    assert "as_of" in text(r)
    assert "Pro" in text(r)


def test_statements_as_of_in_the_future_is_an_error(client):
    future = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    r = call(client, "get_financial_statements", key=pro_key(), ticker="AAPL", as_of=future)
    assert r["isError"] is True
    assert "future" in text(r)


# ---------------------------------------------------------------------------
# get_exceptions
# ---------------------------------------------------------------------------

def test_exceptions_refused_by_the_plan_is_an_error(client):
    r = call(client, "get_exceptions")
    assert r["isError"] is True
    assert "exceptions feed" in text(r).lower()


def test_exceptions_with_a_malformed_since_is_an_error(client):
    r = call(client, "get_exceptions", key=pro_key(), since="yesterday")
    assert r["isError"] is True
    assert "YYYY-MM-DD" in text(r)


def test_exceptions_still_answers_a_caller_whose_plan_allows_it(client):
    """The error paths above must not have swallowed the working one."""
    r = call(client, "get_exceptions", key=pro_key())
    assert r["isError"] is False
    assert "event(s)" in text(r)


# ---------------------------------------------------------------------------
# The catch-all
# ---------------------------------------------------------------------------

def test_an_unexpected_exception_is_logged_but_not_shown_to_the_caller(client, monkeypatch):
    secret = "SELECT password FROM api_users WHERE email='x' -- /srv/app/secret.py"

    def boom(ticker):
        raise RuntimeError(secret)

    import src.company.history as history
    import src.mcp_server as mcp_server

    monkeypatch.setattr(history, "changes", boom)
    spy = MagicMock()
    monkeypatch.setattr(mcp_server, "log", spy)

    r = call(client, "get_balance_sheet_changes", key=pro_key(), ticker="AAPL")

    assert r["isError"] is True
    shown = text(r)
    assert "get_balance_sheet_changes" in shown
    assert "RuntimeError" not in shown
    assert "SELECT" not in shown
    assert "secret.py" not in shown
    # The detail is not lost: it is in the log, where an operator reads it.
    spy.exception.assert_called()
    assert secret in str(spy.exception.call_args)
