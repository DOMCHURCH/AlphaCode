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


def test_an_event_stream_get_and_a_delete_are_405_not_404(client):
    """405 rather than 404 on purpose: a 404 tells a fallback-capable client
    this path is not an MCP endpoint, and it goes looking for the deprecated
    transport instead of reporting the real problem.

    GET is 405 only when it asks for `text/event-stream`, which is the one GET
    an MCP client sends. Any other GET is a person or a crawler and is served
    the page at /mcp (see test_landing_pages)."""
    r = client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"
    assert client.delete("/mcp").status_code == 405


def test_a_browser_get_is_the_page_and_the_post_endpoint_is_untouched(client):
    r = client.get("/mcp", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 200 and "<h1" in r.text
    assert "Accept" in r.headers["vary"].split(", ")
    assert rpc(client, "tools/list").status_code == 200


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


def _seed_stated(ticker: str, stated: float, liabilities: float = 500.0,
                 equity: float = 400.0):
    """Assets 1000; the components we read sum to `liabilities + equity`; the
    filer's own stated liabilities-plus-equity total is `stated`."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name="Stated Co"))
        for metric, val in [("total_assets", 1000.0), ("total_liabilities", liabilities),
                            ("total_equity", equity), ("liabilities_and_equity", stated)]:
            s.add(Fundamental(
                ticker=ticker, metric=metric, value=val,
                period_end=dt.date(2026, 6, 30), fiscal_period="Q2",
                filing_date=dt.date(2026, 8, 1), source="sec",
            ))


def test_check_does_not_open_with_does_not_balance_when_the_filer_balances(client):
    """GYRE's shape: the filer's own totals agree (stated = assets), the
    components we read fall 10% short. The text used to open with "DOES NOT
    balance" and then append a note saying the filing balances."""
    _seed_stated("GYRL", stated=1000.0)
    result = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "GYRL"},
    }).json()["result"]
    text = result["content"][0]["text"]
    assert "DOES NOT balance" not in text
    assert "balances against its own arithmetic" in text
    assert "10.00%" in text
    assert "cannot be told from the figures held here" in text
    structured = result["structuredContent"]
    assert structured["filer_totals_agree"] is True
    assert structured["balances"] is False        # as read here, it does not close


def test_check_keeps_does_not_balance_when_the_filers_own_totals_disagree(client):
    _seed_stated("BRKT", stated=800.0)
    result = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "BRKT"},
    }).json()["result"]
    text = result["content"][0]["text"]
    assert "DOES NOT balance" in text
    assert result["structuredContent"]["filer_totals_agree"] is False


def test_check_reports_a_filing_that_balances(client):
    seed("TSTB", equity=400.0)
    result = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "TSTB"},
    }).json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["balances"] is True


def test_an_unknown_ticker_is_reported_as_an_error_that_says_what_to_do(client):
    """"We do not cover that company" is flagged `isError`, by the owner's
    decision (2026-10-16). It used to be a plain answer, on the argument that
    `isError` sends a model round a retry loop it cannot win. The answer to that
    is the text: it says retrying the same ticker will not help and names the
    tool that will (search_companies). It still arrives as a normal result
    rather than a JSON-RPC error, so the model sees it.
    """
    r = rpc(client, "tools/call", {
        "name": "check_balance_sheet", "arguments": {"ticker": "NOSUCH"},
    })
    assert r.status_code == 200
    assert "error" not in r.json()
    result = r.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "No filed balance sheet" in text
    assert "NOSUCH" in text
    assert "Retrying the same ticker will not help" in text
    assert "search_companies" in text
    assert "structuredContent" not in result
    assert result["_meta"]["balanceproof/plan"]["tier"] == "demo"


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


# ---------------------------------------------------------------------------
# Launch prep (2026-10-16): the numbers are in the text, the labels are honest,
# and a failure says it failed
# ---------------------------------------------------------------------------


def _text(result):
    """What a text-only client shows a model: the content text and nothing else."""
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


def _call(client, name, **arguments):
    return rpc(client, "tools/call", {"name": name, "arguments": arguments}).json()["result"]


def _seed_totals(ticker, assets, liabilities, equity, *, incl=None, nci=None,
                 period=dt.date(2026, 6, 30), filed=dt.date(2026, 7, 31), fp="Q3"):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    rows = [("total_assets", assets), ("total_liabilities", liabilities), ("total_equity", equity)]
    if incl is not None:
        rows.append(("total_equity_incl_nci", incl))
    if nci is not None:
        rows.append(("minority_interest", nci))
    with session_scope() as s:
        if period == dt.date(2026, 6, 30):
            s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Co"))
        for metric, val in rows:
            s.add(Fundamental(ticker=ticker, metric=metric, value=val, period_end=period,
                              fiscal_period=fp, filing_date=filed, source="sec"))


APPLE = dict(assets=383_266_000_000.0, liabilities=275_746_000_000.0, equity=107_520_000_000.0)
MASTERCARD = dict(assets=57_691_000_000.0, liabilities=52_085_000_000.0, equity=5_611_000_000.0,
                  incl=5_606_000_000.0, nci=-5_000_000.0)


def test_balance_sheet_text_alone_carries_the_three_totals(client):
    _seed_totals("APTT", **APPLE)
    text = _text(_call(client, "get_balance_sheet", ticker="APTT"))
    assert "Total assets $383.27B ($383,266,000,000)" in text
    assert "Total liabilities $275.75B ($275,746,000,000)" in text
    assert "Shareholders' equity $107.52B ($107,520,000,000)" in text
    # The latest filing's value is shown, so the text must not promise the
    # original; it says a revision replaces it, and where as_of lives.
    assert "When a later filing revises a figure, the default view shows the revised value" in text
    assert "under the later filing's date" in text
    assert "as_of (on get_financial_statements, Pro and above)" in text
    for phrase in ("not restated", "as-filed", "as filed", "never revised"):
        assert phrase not in text


def test_check_text_alone_carries_the_verdict_and_the_totals(client):
    _seed_totals("APTT", **APPLE)
    text = _text(_call(client, "check_balance_sheet", ticker="APTT"))
    assert "balances for the period ending" in text
    assert "Total assets $383.27B ($383,266,000,000)" in text
    assert "Total liabilities $275.75B ($275,746,000,000)" in text
    assert "Shareholders' equity $107.52B ($107,520,000,000)" in text


def test_an_equity_figure_that_includes_the_nci_is_labelled_that_way(client):
    _seed_totals("MAXX", **MASTERCARD)
    check = _call(client, "check_balance_sheet", ticker="MAXX")
    text = _text(check)
    assert "Equity incl. noncontrolling interest $5.61B ($5,606,000,000)" in text, text
    assert "Shareholders' equity" not in text
    structured = check["structuredContent"]
    assert structured["total_equity_label"] == "Equity incl. noncontrolling interest"
    equity_block = next(b for b in structured["claims"] if b["key"] == "equity")
    assert equity_block["label"] == "Equity incl. noncontrolling interest"
    assert equity_block["value"] == 5_606_000_000.0
    sheet_text = _text(_call(client, "get_balance_sheet", ticker="MAXX"))
    assert "Equity incl. noncontrolling interest $5.61B ($5,606,000,000)" in sheet_text


def test_a_parent_only_equity_figure_keeps_the_shareholders_equity_label(client):
    _seed_totals("APTT", **APPLE)
    structured = _call(client, "check_balance_sheet", ticker="APTT")["structuredContent"]
    assert structured["total_equity_label"] == "Shareholders' equity"
    assert next(b for b in structured["claims"] if b["key"] == "equity")["label"] == "Shareholders' equity"


def test_check_payload_carries_no_chart_rendering_fields(client):
    _seed_totals("APTT", **APPLE)
    structured = _call(client, "check_balance_sheet", ticker="APTT")["structuredContent"]
    assert "mode" not in structured
    assert "claims_span_pct" not in structured
    for block in structured["assets"] + structured["claims"]:
        assert "tone" not in block
        assert "label_style" not in block
    # what a reader does need is still there
    for key in ("balances", "identity_basis", "imbalance_pct", "total_assets",
                "total_liabilities", "total_equity", "missing_components", "notes"):
        assert key in structured


def test_filer_totals_agree_is_null_when_the_filing_simply_balances(client):
    _seed_totals("APTT", **APPLE)
    structured = _call(client, "check_balance_sheet", ticker="APTT")["structuredContent"]
    assert structured["balances"] is True
    assert structured["filer_totals_agree"] is None
    assert structured["filer_totals_note"].startswith("Not applicable")


@pytest.mark.parametrize("tool", [
    "get_balance_sheet", "check_balance_sheet", "get_balance_sheet_history",
    "get_financial_statements",
])
def test_every_ticker_tool_reports_an_unknown_ticker_as_an_error(client, tool):
    result = _call(client, tool, ticker="NOSUCH")
    assert result["isError"] is True
    text = _text(result)
    assert "NOSUCH" in text and "search_companies" in text
    missing = _call(client, tool)
    assert missing["isError"] is True
    assert "No ticker given" in _text(missing)


def test_an_unknown_ticker_is_still_not_billed(client):
    _call(client, "get_balance_sheet", ticker="NOSUCH")
    assert _count("demo_usage") == 0


def test_a_longer_history_than_the_plan_allows_is_a_plan_refusal(client):
    """Asking for more years than the plan reaches is refused, like as_of, and
    is not billed. Leaving `years` out still returns what the plan includes."""
    _seed_totals("HSTT", **APPLE)
    before = _count("demo_usage")
    result = _call(client, "get_balance_sheet_history", ticker="HSTT", years=3)
    text = _text(result)
    assert result["isError"] is True
    assert "3 years of balance sheet history needs the Starter plan or above" in text
    assert "this caller is on Free, which reaches back 1 year" in text
    assert "Leave `years` out" in text
    assert "https://balanceproof.dev/pricing" in text
    assert "structuredContent" not in result
    assert _count("demo_usage") == before, "a refused request was billed"

    # The same caller, without `years` (or within the plan), gets data.
    for args in ({}, {"years": 1}):
        ok = _call(client, "get_balance_sheet_history", ticker="HSTT", **args)
        assert ok["isError"] is False
        assert ok["structuredContent"]["years_allowed"] == 1
        assert "- 2026-06-30" in _text(ok) and "assets $383.27B" in _text(ok)


def test_history_lists_each_period_with_its_totals_in_the_text(client):
    _seed_totals("HSTT", **APPLE)
    _seed_totals("HSTT", assets=371_082_000_000.0, liabilities=264_591_000_000.0,
                 equity=106_491_000_000.0, period=dt.date(2026, 3, 31),
                 filed=dt.date(2026, 5, 1), fp="Q2")
    text = _text(_call(client, "get_balance_sheet_history", ticker="HSTT"))
    assert "2 balance sheet(s) for HSTT" in text
    assert "- 2026-06-30 (filed 2026-07-31): assets $383.27B, liabilities $275.75B" in text
    assert "- 2026-03-31 (filed 2026-05-01): assets $371.08B, liabilities $264.59B" in text


def _seed_old_annual(ticker="OLDA"):
    """A filer whose only annual period ended more than a year ago."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    fy, filed = dt.date(2024, 12, 31), dt.date(2025, 2, 15)
    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker=ticker, name="Old Annual Co"))
        for metric, val in [("revenue", 1000e6), ("cogs", 600e6), ("gross_profit", 400e6)]:
            s.add(Fundamental(ticker=ticker, metric=metric, value=val, period_end=fy,
                              fiscal_period="FY", filing_date=filed, source="sec"))


def test_an_empty_annual_window_says_why_and_where_the_data_is(client):
    _seed_old_annual()
    result = _call(client, "get_financial_statements", ticker="OLDA", period="annual")
    assert result["isError"] is False
    text = _text(result)
    assert "No annual period for OLDA ended in the last 1 year" in text
    assert "the newest annual period ended 2024-12-31, outside that window" in text
    assert "period=quarterly" in text
    assert result["structuredContent"]["periods"] == []


def test_statements_text_lists_each_period_with_its_numbers(client):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    q, filed = dt.date(2026, 3, 31), dt.date(2026, 5, 1)
    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=dt.date.today(), ticker="STQQ", name="Statement Q Co"))
        for metric, val in [("revenue", 96_221e6), ("cogs", 24_079e6), ("gross_profit", 72_142e6),
                            ("net_income", 59_688e6), ("operating_cash_flow", 24_077e6),
                            ("investing_cash_flow", -8_695e6), ("financing_cash_flow", -6_176e6),
                            ("cash_change", 9_206e6)]:
            s.add(Fundamental(ticker="STQQ", metric=metric, value=val, period_end=q,
                              fiscal_period="Q1", filing_date=filed, source="sec"))
    with session_scope() as s:   # a fiscal year on file, so the quarter can be placed in it
        for metric, val in [("revenue", 380_000e6), ("cogs", 95_000e6), ("gross_profit", 285_000e6)]:
            s.add(Fundamental(ticker="STQQ", metric=metric, value=val, period_end=dt.date(2025, 12, 31),
                              fiscal_period="FY", filing_date=dt.date(2026, 2, 15), source="sec"))
    text = _text(_call(client, "get_financial_statements", ticker="STQQ", period="quarterly"))
    assert "- 2026-03-31 Q1: revenue $96.22B, gross profit $72.14B, net income $59.69B, operating cash flow $24.08B; checks 2/2 passed" in text, text
    assert "checks" in text
