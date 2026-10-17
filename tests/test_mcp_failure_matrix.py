"""Every MCP tool against every kind of failure, with the `isError` each must give.

The rule (see `_tool_result`): a failure a caller could act on is reported as
`isError: true`, so a model relays it as a failure; an empty-but-honest answer is
not a failure. `None` in the table means the failure cannot happen for that tool.

                            unknown   missing   plan      bad     internal
                            ticker    input     refusal   date    error
get_balance_sheet            T         T         -         -       T
check_balance_sheet          T         T         -         -       T
get_balance_sheet_history    T         T         T (years) -       T
get_balance_sheet_changes    T         T         T         -       T
get_financial_statements     T         T         T (as_of, T       T
                                                 years)
get_exceptions               T         -(optional) T       T       T
search_companies             F (no     T         -         -       T
                              match is
                              an answer)
get_api_key                  -         T (email) -         -       T
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

DEMO_KEY = "demo-key-for-failure-matrix-0123456789"
DETAIL = "SECRET-DETAIL select * from api_users /srv/app/x.py"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "mcp_matrix.db"
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

    # The lookup caches company names for 30 minutes, across tests.
    from src.company.lookup import reset_cache
    reset_cache()

    from src.api import _demo_gate, _demo_ip_gate, _register_gate, app

    for gate in (_demo_ip_gate, _demo_gate, _register_gate):
        gate.reset()
    with TestClient(app) as c:
        yield c

    for gate in (_demo_ip_gate, _demo_gate, _register_gate):
        gate.reset()
    reset_cache()
    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture()
def pro(client):
    from src import accounts

    account, key = accounts.register("matrix-pro@example.com")
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


TICKER_TOOLS = [
    "get_balance_sheet",
    "check_balance_sheet",
    "get_balance_sheet_history",
    "get_balance_sheet_changes",
    "get_financial_statements",
]


# ---------------------------------------------------------------------------
# unknown ticker: an error, and it says retrying will not help
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", TICKER_TOOLS + ["get_exceptions"])
def test_an_unknown_ticker_is_an_error_that_says_retrying_will_not_help(client, pro, tool):
    r = call(client, tool, key=pro, ticker="NOSUCH")
    assert r["isError"] is True
    shown = text(r)
    assert "NOSUCH" in shown
    assert "Retrying the same ticker will not help" in shown
    assert "search_companies" in shown
    assert "structuredContent" not in r


def test_search_with_no_match_is_an_answer_not_an_error(client):
    seed_company("NAMEDCO")      # names must be loaded, or the search cannot run
    r = call(client, "search_companies", query="zzzz no such company qqqq")
    assert r["isError"] is False


def test_search_by_name_when_names_are_not_loaded_is_an_error(client):
    r = call(client, "search_companies", query="some company name")
    assert r["isError"] is True
    assert "Company names are not loaded" in text(r)


def test_a_covered_ticker_with_no_exceptions_is_not_an_error(client, pro):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker="CLEANCO", name="Clean Co"))
        for metric, val in [("total_assets", 1000.0), ("total_liabilities", 600.0),
                            ("total_equity", 400.0)]:
            s.add(Fundamental(ticker="CLEANCO", metric=metric, value=val,
                              period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
                              filing_date=dt.date(2026, 8, 1), source="sec"))

    r = call(client, "get_exceptions", key=pro, ticker="CLEANCO")
    assert r["isError"] is False
    assert "0 event(s)" in text(r)


# ---------------------------------------------------------------------------
# missing input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", TICKER_TOOLS)
def test_a_missing_ticker_is_an_error(client, pro, tool):
    r = call(client, tool, key=pro)
    assert r["isError"] is True
    assert "No ticker given" in text(r)


def test_search_without_a_query_is_an_error(client):
    r = call(client, "search_companies")
    assert r["isError"] is True
    assert "No query given" in text(r)


def test_get_api_key_with_no_email_is_an_error(client):
    r = call(client, "get_api_key", accept_terms=True)
    assert r["isError"] is True
    assert "email" in text(r).lower()


# ---------------------------------------------------------------------------
# plan refusal and bad dates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    ("get_balance_sheet_changes", {"ticker": "AAPL"}),
    ("get_financial_statements", {"ticker": "AAPL", "as_of": "2024-01-02"}),
    ("get_exceptions", {}),
])
def test_a_plan_refusal_is_an_error_that_names_the_plan(client, tool, args):
    r = call(client, tool, **args)          # anonymous: Free's allowances
    assert r["isError"] is True
    assert "plan or above" in text(r)
    assert "https://balanceproof.dev/pricing" in text(r)


YEARS_TOOLS = [
    ("get_balance_sheet_history", "balance sheet history"),
    ("get_financial_statements", "financial statements"),
]


@pytest.mark.parametrize("tool,what", YEARS_TOOLS)
@pytest.mark.parametrize("years,plan_named", [(3, "Starter"), (7, "Pro"), (50, "Business")])
def test_a_years_request_beyond_the_plan_is_a_refusal_naming_the_plan_that_covers_it(
    client, tool, what, years, plan_named
):
    from src import accounts

    account, key = accounts.register("matrix-free@example.com")
    seed_company("CLAMPCO")
    r = call(client, tool, key=key, ticker="CLAMPCO", years=years)
    assert r["isError"] is True
    shown = text(r)
    assert f"{years} years of {what} needs the {plan_named} plan or above" in shown
    assert "this caller is on Free, which reaches back 1 year" in shown
    assert "Leave `years` out" in shown
    assert "https://balanceproof.dev/pricing" in shown
    assert "structuredContent" not in r


@pytest.mark.parametrize("tool,what", YEARS_TOOLS)
def test_a_years_refusal_is_not_billed(client, tool, what):
    from sqlalchemy import text as sql

    from src.storage.db import session_scope

    def used():
        with session_scope() as s:
            return s.execute(sql("SELECT COUNT(*) FROM demo_usage")).scalar() or 0

    seed_company("CLAMPCO")
    before = used()
    r = call(client, tool, ticker="CLAMPCO", years=3)     # anonymous: Free's allowances
    assert r["isError"] is True
    assert used() == before, "a refused request was billed"

    served = call(client, tool, ticker="CLAMPCO")
    assert served["isError"] is False
    assert used() == before + 1, "the control: an answered call is billed once"


@pytest.mark.parametrize("tool,what", YEARS_TOOLS)
def test_a_years_request_within_the_plan_or_none_at_all_still_answers(client, pro, tool, what):
    seed_company("CLAMPCO")
    for args in ({}, {"years": 10}):
        r = call(client, tool, key=pro, ticker="CLAMPCO", **args)
        assert r["isError"] is False, (tool, args, text(r))


@pytest.mark.parametrize("tool,args", [
    ("get_financial_statements", {"ticker": "AAPL", "as_of": "not-a-date"}),
    ("get_exceptions", {"since": "not-a-date"}),
])
def test_a_bad_date_is_an_error(client, pro, tool, args):
    r = call(client, tool, key=pro, **args)
    assert r["isError"] is True
    assert "YYYY-MM-DD" in text(r)


# ---------------------------------------------------------------------------
# get_api_key refusals are errors too: no key was created
# ---------------------------------------------------------------------------

def test_get_api_key_refusals_are_errors(client):
    no_terms = call(client, "get_api_key", email="a@example.com", accept_terms=False)
    assert no_terms["isError"] is True and "terms" in text(no_terms)

    bad = call(client, "get_api_key", email="not an email", accept_terms=True)
    assert bad["isError"] is True

    ok = call(client, "get_api_key", email="dup@example.com", accept_terms=True)
    assert ok["isError"] is False
    again = call(client, "get_api_key", email="dup@example.com", accept_terms=True)
    assert again["isError"] is True and "already registered" in text(again)


# ---------------------------------------------------------------------------
# internal error: an error, generic text, detail only in the log
# ---------------------------------------------------------------------------

METERED = TICKER_TOOLS + ["get_exceptions", "search_companies"]


@pytest.mark.parametrize("tool", METERED)
def test_an_internal_error_is_an_error_without_the_exception_text(client, pro, monkeypatch, tool):
    import src.mcp_server as mcp_server

    def boom(args, m):
        raise RuntimeError(DETAIL)

    monkeypatch.setitem(mcp_server._TOOL_IMPLS, tool, boom)
    r = call(client, tool, key=pro, ticker="AAPL", query="apple")
    assert r["isError"] is True
    shown = text(r)
    assert tool in shown
    assert "SECRET-DETAIL" not in shown
    assert "RuntimeError" not in shown
    assert "api_users" not in shown


def test_get_api_key_internal_error_does_not_return_the_exception_text(client, monkeypatch):
    from src import accounts

    def boom(address):
        raise RuntimeError(DETAIL)

    monkeypatch.setattr(accounts, "register", boom)
    r = call(client, "get_api_key", email="boom@example.com", accept_terms=True)
    assert r["isError"] is True
    shown = text(r)
    assert "SECRET-DETAIL" not in shown
    assert "RuntimeError" not in shown
    assert "api_users" not in shown
    assert "went wrong on our side" in shown


def seed_company(ticker):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name=ticker))
        for metric, val in [("total_assets", 1000.0), ("total_liabilities", 600.0),
                            ("total_equity", 400.0)]:
            s.add(Fundamental(ticker=ticker, metric=metric, value=val,
                              period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
                              filing_date=dt.date(2026, 8, 1), source="sec"))
        # An annual income statement, so get_financial_statements has something
        # to return rather than "not covered".
        for metric, val in [("revenue", 1000e6), ("cogs", 600e6), ("gross_profit", 400e6)]:
            s.add(Fundamental(ticker=ticker, metric=metric, value=val,
                              period_end=dt.date(2026, 6, 30), fiscal_period="FY",
                              filing_date=dt.date(2026, 8, 1), source="sec"))
