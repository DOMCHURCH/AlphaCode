"""The MCP endpoint: both protocol eras, and the metering it must not leak.

Two things are worth testing hard here and they are not the tool outputs.

The first is that BOTH protocol eras work. MCP dropped the `initialize`
handshake in `2026-07-28`, but essentially every client deployed today still
opens with it -- so a server that implements only the current revision is a
server almost nobody can connect to, and a regression that breaks the legacy
path would look perfectly healthy in a spec-conformance check. Each era gets
its own end-to-end flow below.

The second is the metering. `/mcp` serves the same data as the paid API, and
an MCP tool is called in a loop by construction, so the endpoint is exactly
the shape of hole that gives the product away. The tests assert the edge that
must NOT be crossed: an anonymous call is recorded to `demo_usage` and never
to `usage_logs`, and the demo gate stops the loop.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

DEMO_KEY = "demo-key-for-mcp-tests-0123456789"
MODERN = "2026-07-28"
LEGACY = "2025-06-18"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "mcp.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "1000")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "10000")
    # Off: the per-day cap is a separate gate with its own default, and
    # leaving it on makes the hourly-gate assertions depend on which fires
    # first.
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


def seed(ticker: str = "TSTB", equity: float = 400.0, name: str = "Test Co"):
    """One company. `equity` is the knob: 400 balances, 300 does not."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker=ticker, name=name
        ))
        for metric, val in [
            ("total_assets", 1000.0),
            ("total_liabilities", 600.0),
            ("total_equity", equity),
        ]:
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=val,
                period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
                filing_date=dt.date(2026, 8, 1), source="sec",
            ))


def rpc(client, method, params=None, req_id=1, modern=False, headers=None):
    """One JSON-RPC POST, with the header mirroring each era requires."""
    params = dict(params or {})
    hdrs = {"Accept": "application/json, text/event-stream"}
    if modern:
        params.setdefault("_meta", {})[
            "io.modelcontextprotocol/protocolVersion"
        ] = MODERN
        hdrs["MCP-Protocol-Version"] = MODERN
        hdrs["Mcp-Method"] = method
        if method == "tools/call":
            hdrs["Mcp-Name"] = params.get("name", "")
    hdrs.update(headers or {})
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    return client.post("/mcp", json=body, headers=hdrs)


# ---------------------------------------------------------------------------
# The legacy era -- the one real clients currently speak
# ---------------------------------------------------------------------------


def test_legacy_handshake_echoes_the_version_and_mints_a_session(client):
    r = rpc(client, "initialize", {
        "protocolVersion": LEGACY, "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    })
    assert r.status_code == 200
    body = r.json()["result"]
    # Echoed, not downgraded: a client that asked for a version we speak must
    # not be told to use a different one.
    assert body["protocolVersion"] == LEGACY
    assert body["serverInfo"]["name"] == "balanceproof"
    # A legacy client expects to be issued a session id even though this
    # server keeps no state behind it.
    assert r.headers.get("mcp-session-id")


def test_legacy_initialized_notification_is_accepted(client):
    r = client.post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert r.status_code == 202
    assert r.content == b""


def test_legacy_requests_are_not_header_validated(client):
    """The expensive mistake, guarded.

    A legacy client sends no `Mcp-Method` header because its revision did not
    define one. Validating anyway would reject every request it will ever
    make, so this asserts the absence of that strictness rather than a
    feature.
    """
    r = rpc(client, "tools/list")
    assert r.status_code == 200
    assert "error" not in r.json()


# ---------------------------------------------------------------------------
# The modern era
# ---------------------------------------------------------------------------


def test_modern_discover_lists_every_supported_version(client):
    r = rpc(client, "server/discover", modern=True)
    assert r.status_code == 200
    from src.mcp_server import SUPPORTED_PROTOCOLS

    assert r.json()["result"]["protocolVersions"] == list(SUPPORTED_PROTOCOLS)


def test_modern_request_missing_mirrored_header_is_refused(client):
    """Header and body must agree, because an intermediary may route on one
    and this server executes the other."""
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN
            }},
        },
        headers={"MCP-Protocol-Version": MODERN},  # no Mcp-Method
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_modern_header_body_mismatch_is_refused(client):
    r = rpc(client, "tools/list", modern=True, headers={"Mcp-Method": "tools/call"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32020


def test_unsupported_protocol_version_names_what_is_supported(client):
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {
                "io.modelcontextprotocol/protocolVersion": "1999-01-01"
            }},
        },
        headers={
            "MCP-Protocol-Version": "1999-01-01", "Mcp-Method": "tools/list",
        },
    )
    assert r.status_code == 400
    assert MODERN in r.json()["error"]["data"]["supported"]


# ---------------------------------------------------------------------------
# Transport rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["get", "delete"])
def test_get_and_delete_are_405_not_404(client, verb):
    """405 rather than 404 on purpose: a 404 tells a fallback-capable client
    this path is not an MCP endpoint, and it goes looking for the deprecated
    transport instead of reporting the real problem."""
    r = getattr(client, verb)("/mcp")
    assert r.status_code == 405


def test_unknown_method_is_404_with_a_jsonrpc_body(client):
    r = rpc(client, "nope/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == -32601


def test_malformed_json_is_a_parse_error_not_a_500(client):
    r = client.post(
        "/mcp", content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == -32700


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_tools_list_is_the_eight_tools(client):
    """Kept as an exact list rather than a count. The order and the names are
    what a directory listing and a model's tool-choice both see, so a tool
    appearing or being renamed should be a decision, not a diff nobody
    noticed. History and changes were added on purpose on 2026-09-23 (paid
    tiers, Stage A); statements and exceptions on 2026-09-24 (quant features)."""
    names = [t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]]
    assert names == [
        "search_companies", "get_balance_sheet", "get_api_key",
        "get_balance_sheet_history", "get_balance_sheet_changes",
        "get_financial_statements", "get_exceptions",
        "check_balance_sheet",
    ]


def test_exceptions_tool_names_the_plan_it_needs(client):
    r = rpc(client, "tools/call", {"name": "get_exceptions", "arguments": {}})
    text = r.json()["result"]["content"][0]["text"]
    assert "Pro plan" in text


def test_check_reports_a_filing_that_does_not_balance(client):
    seed("TSTF", equity=300.0, name="Testfail Holdings")
    r = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "TSTF"},
    })
    result = r.json()["result"]
    assert result["isError"] is False
    text = result["content"][0]["text"]
    assert "DOES NOT balance" in text
    # The drift is named, not merely asserted -- "by 10%" is the useful half.
    assert "10.00%" in text
    assert result["structuredContent"]["balances"] is False


def test_check_reports_a_filing_that_balances(client):
    seed("TSTB", equity=400.0)
    result = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "TSTB"},
    }).json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["balances"] is True


def test_an_unknown_ticker_is_an_answer_not_an_error(client):
    """"We do not cover that company" is a successful query with a negative
    result, so it is NOT flagged `isError`.

    The distinction is not pedantry. `isError` tells a model the call failed
    and is worth correcting and retrying; a coverage miss is neither, and
    flagging it sends the model round a loop it cannot win. What matters is
    that the text says plainly that nothing was found, and that it arrives as
    a normal result rather than a JSON-RPC error the model never sees.
    """
    r = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "NOSUCH"},
    })
    assert r.status_code == 200
    assert "error" not in r.json()
    result = r.json()["result"]
    assert result["isError"] is False
    assert "No filed balance sheet" in result["content"][0]["text"]


def test_unknown_tool_name_is_reported_to_the_model(client):
    result = rpc(client, "tools/call", {
        "name": "not_a_tool", "arguments": {},
    }).json()["result"]
    assert result["isError"] is True
    assert "not_a_tool" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Metering -- the invariant
# ---------------------------------------------------------------------------


def _count(table: str) -> int:
    from sqlalchemy import text

    from src.storage.db import session_scope

    with session_scope() as s:
        return s.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0


def test_anonymous_calls_are_recorded_as_demo_usage_only(client):
    """Anonymous traffic must never appear in a paying customer's figures.

    `/api/demo` keeps this line by writing to `demo_usage` rather than
    `usage_logs`; `/mcp` serves the same data and so inherits the same rule.
    """
    seed()
    before = _count("usage_logs")
    rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "TSTB"},
    })
    assert _count("demo_usage") == 1
    assert _count("usage_logs") == before


def test_a_miss_is_not_billed(client):
    """A ticker that does not exist costs the caller nothing -- the same rule
    `/api/company` follows by recording after the read rather than before."""
    rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "NOSUCH"},
    })
    assert _count("demo_usage") == 0


def test_the_demo_gate_stops_a_loop_and_says_where_to_get_a_key(client):
    """The rate limit is the only place the product is pitched at the moment
    the caller wanted more, so the text matters as much as the refusal."""
    from src.api import _demo_ip_gate

    seed()
    _demo_ip_gate.reset()

    last = None
    for _ in range(105):
        last = rpc(client, "tools/call", {
            "name": "check_balance_sheet", "arguments": {"ticker": "TSTB"},
        })

    result = last.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "rate limit" in text.lower()
    assert "/dashboard" in text
    # Still no leakage into paid usage, even under a loop.
    assert _count("usage_logs") == 0


def test_a_bad_api_key_is_refused_rather_than_silently_demoted(client):
    """A caller who supplied a key meant to use it. Falling back to the demo
    would meter them somewhere they cannot see and hide the typo."""
    seed()
    result = rpc(
        client, "tools/call",
        {"name": "check_balance_sheet", "arguments": {"ticker": "TSTB"}},
        headers={"Authorization": "Bearer not-a-real-key"},
    ).json()["result"]
    assert result["isError"] is True
    assert "not recognised" in result["content"][0]["text"]
