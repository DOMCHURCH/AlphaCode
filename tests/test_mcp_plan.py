"""What the MCP endpoint can tell you about WHO is calling it.

Two questions this covers, and they are different.

The first is operational: anonymous MCP traffic and the home page demo box
meter into the same table, so without a source tag the one number nobody could
produce was "is anyone using the MCP server" -- which is the whole question a
directory listing exists to answer.

The second is commercial: a keyed caller should learn their allowance is
running out BEFORE it runs out, and should not be told about it on every
single call, because a model relays what it is given.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

DEMO_KEY = "demo-key-for-plan-tests-0123456789"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "plan.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "10")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "500")
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "0")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import _demo_gate, _demo_ip_gate, app

    # BOTH gates, and in teardown as well as setup. These are module-level and
    # therefore shared by every test in the process: the loop test below spends
    # the per-address window on purpose, and leaving it spent made whichever
    # file pytest happened to run next fail on a limit it never touched.
    # Suite results that depend on file order are worse than no suite.
    _demo_ip_gate.reset()
    _demo_gate.reset()
    with TestClient(app) as c:
        yield c

    _demo_ip_gate.reset()
    _demo_gate.reset()

    get_settings.cache_clear()
    reset_engine_cache()


def seed(ticker: str = "TSTB", equity: float = 400.0):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker=ticker, name="Test Co"
        ))
        for metric, val in [("total_assets", 1000.0),
                            ("total_liabilities", 600.0),
                            ("total_equity", equity)]:
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=val,
                period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
                filing_date=dt.date(2026, 8, 1), source="sec",
            ))


def call(client, ticker="TSTB", key=None):
    headers = {}
    if key:
        headers["Authorization"] = "Bearer " + key
    return client.post("/mcp", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "check_balance_sheet",
                   "arguments": {"ticker": ticker}},
    }).json()["result"]


def make_account(tier="free"):
    """A real keyed account.

    Made through `accounts.register` rather than by inserting a row, so the
    key is hashed and the account is shaped exactly as a real signup leaves
    it -- a hand-built row is a second definition of "account" that drifts.
    """
    from src import accounts

    account, key = accounts.register(f"{tier}-mcp@example.com")
    if tier != account.tier:
        accounts.apply_admin_action(account.email, "grant_pro")
    return key


# ---------------------------------------------------------------------------
# Who called, and through which surface
# ---------------------------------------------------------------------------


def _demo_rows():
    from sqlalchemy import text

    from src.storage.db import session_scope

    with session_scope() as s:
        return s.execute(
            text("SELECT source, COUNT(*) FROM demo_usage GROUP BY source")
        ).all()


def test_an_anonymous_mcp_call_is_tagged_as_mcp(client):
    """The home page demo box and /mcp meter into the same table. The tag is
    the only thing that tells them apart, and 'is anyone using the MCP server'
    is unanswerable without it."""
    seed()
    call(client)
    assert _demo_rows() == [("mcp", 1)]


def test_the_home_page_demo_is_still_tagged_web(client):
    seed()
    client.get("/api/demo/TSTB")
    rows = dict(_demo_rows())
    assert rows.get("web") == 1
    assert "mcp" not in rows


def test_the_two_surfaces_are_separable_in_one_table(client):
    seed()
    call(client)
    client.get("/api/demo/TSTB")
    call(client)
    assert dict(_demo_rows()) == {"mcp": 2, "web": 1}


# ---------------------------------------------------------------------------
# What the caller is told about their own plan
# ---------------------------------------------------------------------------


def test_an_anonymous_caller_is_reported_as_the_demo_tier(client):
    seed()
    plan = call(client)["_meta"]["balanceproof/plan"]
    assert plan["tier"] == "demo"
    assert plan["authenticated"] is False


def test_a_keyed_caller_gets_tier_and_remaining(client):
    seed()
    key = make_account("free")
    plan = call(client, key=key)["_meta"]["balanceproof/plan"]
    assert plan == {
        "tier": "free", "authenticated": True,
        "monthly_limit": 10, "used_this_month": 1, "remaining": 9,
    }


def test_the_allowance_is_not_mentioned_while_it_is_comfortable(client):
    """A model relays what it is given. An allowance line on every call is the
    loudest thing in the conversation and makes the tool feel like a billing
    page, so it stays quiet until it is nearly spent."""
    seed()
    key = make_account("free")
    text = call(client, key=key)["content"][0]["text"]
    assert "calls left" not in text


def test_the_allowance_speaks_up_at_20_percent(client):
    """Late enough to be information, early enough to act on."""
    seed()
    key = make_account("free")           # limit 10, so the note starts at 2 left
    last = None
    for _ in range(9):
        last = call(client, key=key)
    text = last["content"][0]["text"]
    assert "calls left on the free tier" in text
    assert "It resets on the 1st (UTC)" in text
    # And it says where more comes from, since this caller is not on Pro.
    assert "/pricing" in text


def test_pro_is_not_pitched_an_upgrade_it_already_has(client):
    seed()
    key = make_account("pro")
    plan = call(client, key=key)["_meta"]["balanceproof/plan"]
    assert plan["tier"] == "pro"
    assert plan["monthly_limit"] == 500
