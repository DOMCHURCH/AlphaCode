"""The MCP endpoint: BalanceProof as a tool an AI client can call.

WHY THIS EXISTS, in one line: it is the only acquisition channel open to this
deployment that does not need an account, karma, or a moderator's goodwill.
A reader pastes `https://balanceproof.dev/mcp` into Claude, ChatGPT or Cursor
and the verification layer is available inside the place they already work --
and the same URL is what the MCP directories (mcp.so, smithery.ai,
glama.ai/mcp) list. It is a product surface and a distribution surface at once.

It is also the answer to the one objection this product cannot argue with. A
developer can pull SEC `companyfacts` for free and parse it themselves. An
analyst cannot, and has no reason to want to. This endpoint is the shape the
second reader can actually buy.


TWO PROTOCOL ERAS, AND WHY BOTH ARE HERE
----------------------------------------
MCP changed shape on 2026-07-28. That revision removed the `initialize`
handshake, removed protocol-level sessions, and moved the protocol version
into per-request metadata. It is seven weeks old at the time of writing.

Essentially every client deployed in the field still speaks a revision from
the *previous* era (`2025-03-26` through `2025-11-25`), which opens with
`initialize` and carries an `Mcp-Session-Id`. So the legacy path is not a
courtesy fallback here -- for the next several months it is the path almost
every real caller takes. Both are implemented, and the tests cover both.

Era detection is one field, and it is the client's own declaration rather
than a guess:

    params["_meta"]["io.modelcontextprotocol/protocolVersion"]  present
        -> modern (2026-07-28). Header mirroring is REQUIRED and validated.
    absent
        -> legacy. `initialize` is expected, headers are not validated.

Getting that branch wrong in the strict direction is the expensive mistake:
enforcing the modern `Mcp-Method` / `Mcp-Name` header rules on a legacy client
rejects every request it will ever send, with a -32020 it has no code to
handle. So validation is opt-in by era, never by default.


SESSIONS
--------
The modern revision has none, and this server holds no state between requests
either way. But a legacy client sends `Mcp-Session-Id` on everything after
`initialize` and some abort if the server never issued one. So: mint an id on
a legacy `initialize`, echo it, and thereafter accept any value without
looking at it. Nothing is stored. The id exists to satisfy a client that
expects to see one, which is the whole of its job.


METERING -- THE INVARIANT THIS MODULE MUST NOT BREAK
----------------------------------------------------
`/api/demo/{ticker}` carries a docstring explaining that it IS the paid
product, so with nothing per-caller in front of it the product is free to
anybody willing to loop. That reasoning applies here unchanged, and more
sharply: an MCP tool is called in a loop by construction.

So this module does NOT read the database directly. Anonymous calls go
through the *same* gates as the demo, in the same order, and are recorded to
`demo_usage` -- never to `usage_logs`, so anonymous traffic still cannot
appear in a paying customer's usage figures. Keyed calls go through
`accounts.enforce_monthly_limit` and `accounts.record_call` exactly as
`/api/company/{ticker}` does.

The gates are imported from `src.api` at call time rather than reimplemented.
They are private names there, which is ordinarily a reason not to reach for
them; here it is the reason to. A second implementation of a rate limit is a
second set of limits, and the two would drift apart on the first change.
The late import also breaks what would otherwise be an import cycle, since
`api` mounts this router.


A RATE LIMIT IS A SALES PITCH HERE
----------------------------------
When an anonymous caller exhausts the demo window the tool does not fail
silently -- it returns `isError` with the text that names the free tier and
the price of Pro. The model reads that and tells the user where to get a key.
It is the only place in the product where hitting a limit is spoken aloud to
the buyer at the moment they wanted more.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

import structlog

log = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

# The revision this server implements natively.
PROTOCOL_MODERN = "2026-07-28"

# Handshake-era revisions we still answer. Listed newest first because that is
# the order `server/discover` and the unsupported-version error report them in,
# and a client picking from that list should land on the newest it knows.
PROTOCOL_LEGACY: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2025-03-26")

SUPPORTED_PROTOCOLS: tuple[str, ...] = (PROTOCOL_MODERN, *PROTOCOL_LEGACY)

# The `_meta` key carrying the version in the modern era. Spelled once.
_META_VERSION = "io.modelcontextprotocol/protocolVersion"

SERVER_INFO = {
    "name": "balanceproof",
    "title": "BalanceProof — verified SEC balance sheets",
    "version": "1.0.0",
}

# JSON-RPC error codes. -32020 is MCP's own, allocated from the range the
# specification reserves for protocol-defined errors.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_HEADER_MISMATCH = -32020


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
#
# Three tools, deliberately. A directory listing and a model's tool-choice both
# degrade as the list grows, and site-wide aggregates (how many companies are
# covered) are not something a model has any reason to call mid-conversation --
# that belongs on a page, not in a tool.
#
# The descriptions are written for the MODEL, not for a human reading docs.
# Each one says when to reach for it and what it is not, because the failure
# mode that matters is a model calling `get_balance_sheet` when the user asked
# whether the filing can be trusted.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_companies",
        "description": (
            "Find a US public company's ticker symbol from a name or partial "
            "name. Use this FIRST whenever the user names a company in words "
            "rather than giving a ticker. Returns candidate tickers with "
            "company names. Covers companies that file with the SEC."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Company name, partial name, or ticker.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_balance_sheet",
        "description": (
            "The filed balance sheet for one US public company, as reported to "
            "the SEC: assets, liabilities and equity line items with the period "
            "end and filing date. Figures are as-filed, not restated. Use this "
            "when the user wants the numbers. If they want to know whether the "
            "numbers can be TRUSTED, use check_balance_sheet instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. AAPL.",
                },
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "check_balance_sheet",
        "description": (
            "Verify that a company's filed balance sheet actually balances: "
            "does Assets = Liabilities + Equity hold on the filing, and if not, "
            "by how much and why. This is what BalanceProof exists for and it "
            "is not available from raw SEC data without doing the work. "
            "Returns the verdict, the drift as a percentage of assets, and the "
            "basis used — including whether a noncontrolling interest had to be "
            "added to close the identity, which is a different statement from "
            "'it balances'. Use this for any question about data quality, "
            "reliability, accounting irregularities, or whether a figure can be "
            "relied on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Ticker symbol, e.g. AAPL.",
                },
            },
            "required": ["ticker"],
        },
    },
]


# ---------------------------------------------------------------------------
# Caller identification and metering
# ---------------------------------------------------------------------------


class _Metered:
    """One caller's right to make one tool call, and how it is recorded.

    Built per request. `spend()` is called only after the work succeeded, for
    the same reason `/api/company` records after the read: a ticker that does
    not exist must not cost the caller anything.
    """

    def __init__(self, account: Any, ip_hash: str, anonymous: bool) -> None:
        self.account = account
        self.ip_hash = ip_hash
        self.anonymous = anonymous

    def spend(self, ticker: str) -> None:
        from src import accounts, demo

        if self.anonymous:
            demo.record(self.ip_hash, ticker)
        else:
            accounts.record_call(self.account, "/mcp")


def _authorise(request: Request) -> _Metered:
    """Resolve the caller and charge them for the attempt, before any work.

    Raises `fastapi.HTTPException` when the caller is over a limit or the key
    is bad. The caller of this function turns that into an `isError` tool
    result rather than an HTTP error, so the model can relay the reason.

    A key arrives as `Authorization: Bearer <key>`, which is what every MCP
    client's "add a header" box is shaped for. No key is not an error: it is
    the demo, and the demo is the point of this endpoint.
    """
    from fastapi import HTTPException

    from src import accounts, demo
    # Private by name, shared by intent -- see the module docstring. Imported
    # here rather than at module scope because `api` mounts this router.
    from src.api import (
        _caller_ip_hash,
        _demo_gate,
        _demo_ip_gate,
        _enforce_keyed_rate,
        _enforce_rate,
    )
    from src.config.settings import get_settings

    header = request.headers.get("authorization") or ""
    raw_key = header[7:].strip() if header[:7].lower() == "bearer " else ""

    if raw_key:
        account = accounts.lookup(raw_key)
        if account is None:
            raise HTTPException(
                status_code=401,
                detail=(
                    "That API key was not recognised. Get one at "
                    "https://balanceproof.dev/dashboard"
                ),
            )
        accounts.enforce_monthly_limit(account)
        return _Metered(account, "", anonymous=False)

    # Anonymous. Same three windows as the demo, in the same order, because
    # this endpoint serves the same data from the same code path.
    if demo.account() is None:
        raise HTTPException(
            status_code=503,
            detail="The demo is not configured on this deployment.",
        )

    ip_hash = _caller_ip_hash(request)
    settings = get_settings()

    # PER ADDRESS FIRST, then global -- the order is load-bearing and the
    # reasoning is `/api/demo`'s: both gates record the hit when they allow
    # it, so one looping caller must spend its own budget before the shared
    # one, or a single scraper 429s the endpoint for everybody.
    _enforce_keyed_rate(
        _demo_ip_gate,
        ip_hash or "-",
        "MCP lookups",
        detail=(
            "Demo rate limit reached. Get a free API key at "
            f"https://balanceproof.dev/dashboard for "
            f"{settings.free_tier_monthly_calls:,} calls a month, or Pro at "
            f"https://balanceproof.dev/pricing for "
            f"{settings.pro_tier_monthly_calls:,}. Add it to this connector as "
            "an Authorization: Bearer header."
        ),
    )
    _enforce_rate(_demo_gate, "MCP lookups")

    limit = settings.demo_calls_per_ip_per_day
    if limit and demo.calls_today(ip_hash) >= limit:
        raise HTTPException(
            status_code=429,
            detail=(
                "Daily demo limit reached. Get a free API key at "
                "https://balanceproof.dev/dashboard and add it to this "
                "connector as an Authorization: Bearer header."
            ),
        )

    return _Metered(demo.account(), ip_hash, anonymous=True)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
#
# Each returns (summary_text, structured_payload). Both halves are sent: the
# text is what a model reads and quotes, the structured payload is what it
# computes with. Sending only one forces every client to do the other's job.


def _clean(ticker: str) -> str:
    return (ticker or "").strip().lstrip("$").upper()


def _tool_search_companies(args: dict[str, Any], _m: _Metered) -> tuple[str, Any]:
    from src.company import lookup

    query = str(args.get("query") or "").strip()
    if not query:
        return "No query given.", {"matches": []}

    res = lookup.resolve(query)

    if res.kind == "ticker":
        return (
            f"{res.ticker} is a covered ticker.",
            {"resolved": res.ticker, "matches": [{"ticker": res.ticker}]},
        )
    if res.kind == "no_names":
        return (
            "Company names are not loaded on this deployment, so only exact "
            "ticker symbols can be resolved.",
            {"matches": []},
        )

    rows = [
        {"ticker": m.ticker, "name": m.name, "sector": m.sector}
        for m in res.matches
    ]
    if not rows:
        return f"No US public company matched {query!r}.", {"matches": []}

    # `fuzzy` means nothing actually matched -- these are near misses. Saying
    # so matters: a model handed a list with no caveat will pick the first row
    # and report it as the company the user asked about.
    lead = (
        f"No exact match for {query!r}. Closest companies:"
        if res.kind == "fuzzy"
        else f"{len(rows)} match(es) for {query!r}:"
    )
    listed = "\n".join(
        f"  {r['ticker']}  {r['name'] or ''}".rstrip() for r in rows
    )
    return f"{lead}\n{listed}", {"kind": res.kind, "matches": rows}


def _tool_get_balance_sheet(args: dict[str, Any], m: _Metered) -> tuple[str, Any]:
    from src.company.balancesheet import get_balance_sheet

    ticker = _clean(str(args.get("ticker") or ""))
    if not ticker:
        return "No ticker given.", None

    sheet = get_balance_sheet(ticker)
    if sheet is None:
        return (
            f"No filed balance sheet for {ticker}. It may not be a covered "
            "company, or it reports no total for assets.",
            None,
        )

    m.spend(ticker)

    payload = jsonable_encoder(asdict(sheet))
    payload["source"] = "SEC Financial Statement Data Sets (as reported)"

    name = sheet.company_name or ticker
    return (
        f"{name} ({ticker}) balance sheet as filed, period ending "
        f"{sheet.period_end}, filed {sheet.filing_date}. Figures are "
        "as-reported to the SEC, not restated.",
        payload,
    )


def _tool_check_balance_sheet(args: dict[str, Any], m: _Metered) -> tuple[str, Any]:
    from src.company.view1 import build_view1

    ticker = _clean(str(args.get("ticker") or ""))
    if not ticker:
        return "No ticker given.", None

    view = build_view1(ticker)
    if view is None:
        return (
            f"No filed balance sheet for {ticker} to check. It may not be a "
            "covered company, or it reports no total for assets.",
            None,
        )

    m.spend(ticker)

    payload = view.as_dict()
    name = view.company_name or ticker
    period = view.period_end

    # The three outcomes are genuinely different claims and the text says
    # which one it is making. "Balances once you add the minority interest"
    # is not "balances", and a model that flattens the two will tell a user a
    # consolidated filer was fine when the question was whether it was.
    if view.balances and view.identity_basis == "nci":
        verdict = (
            f"{name} ({ticker}) balances for the period ending {period}, but "
            "only once the noncontrolling interest is included: the filer "
            "reports parent equity and the minority interest as separate "
            "lines with no combined total, so the identity for this filing is "
            "A = L + E + NCI. That is a correct filing read on its own terms, "
            "not a discrepancy."
        )
    elif view.balances:
        verdict = (
            f"{name} ({ticker}) balances for the period ending {period}: "
            "assets equal liabilities plus equity within tolerance."
        )
    else:
        verdict = (
            f"{name} ({ticker}) DOES NOT balance for the period ending "
            f"{period}. Assets and liabilities-plus-equity differ by "
            f"{view.imbalance_pct:.2f}% of total assets. This usually means "
            "the filing was tagged in a way a naive read misses rather than "
            "that the company's accounts are wrong — check the notes before "
            "drawing any conclusion about the company."
        )

    if view.missing_components:
        verdict += (
            "\nNot every component was reported: "
            + ", ".join(view.missing_components)
        )
    if view.notes:
        verdict += "\nNotes: " + "; ".join(view.notes)

    return verdict, payload


_TOOL_IMPLS = {
    "search_companies": _tool_search_companies,
    "get_balance_sheet": _tool_get_balance_sheet,
    "check_balance_sheet": _tool_check_balance_sheet,
}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _tool_result(text: str, structured: Any = None, is_error: bool = False) -> dict:
    """An MCP tool result.

    A failed tool call is reported as `isError` inside a successful JSON-RPC
    response, NOT as a JSON-RPC error. The difference is who gets to see it:
    a protocol error is the client's problem and the model never learns of
    it, while `isError` is handed to the model, which can say what went wrong
    and what to do instead. Every failure a user could act on -- an unknown
    ticker, an exhausted rate limit -- belongs on this side of that line.
    """
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if structured is not None:
        out["structuredContent"] = structured
    return out


def _capabilities() -> dict[str, Any]:
    # `listChanged` is false and honest: the tool list is a module constant,
    # so there is no circumstance under which this server would notify.
    return {"tools": {"listChanged": False}}


def _dispatch(
    method: str,
    params: dict[str, Any],
    req_id: Any,
    request: Request,
) -> dict[str, Any]:
    """One JSON-RPC method to one JSON-RPC response object."""
    from fastapi import HTTPException

    if method == "server/discover":
        # Modern era, mandatory: identity, capabilities and supported versions
        # in a single request, so a client can choose a version up front.
        return _result(req_id, {
            "serverInfo": SERVER_INFO,
            "capabilities": _capabilities(),
            "protocolVersions": list(SUPPORTED_PROTOCOLS),
            "instructions": _INSTRUCTIONS,
        })

    if method == "initialize":
        # Legacy era. Echo the version the client asked for when we speak it,
        # so an older client is not forced to downgrade; otherwise answer in
        # the newest legacy revision rather than failing, because a client
        # that sent `initialize` cannot use the modern one anyway.
        asked = str(params.get("protocolVersion") or "")
        spoken = asked if asked in SUPPORTED_PROTOCOLS else PROTOCOL_LEGACY[0]
        return _result(req_id, {
            "protocolVersion": spoken,
            "serverInfo": SERVER_INFO,
            "capabilities": _capabilities(),
            "instructions": _INSTRUCTIONS,
        })

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = str(params.get("name") or "")
        impl = _TOOL_IMPLS.get(name)
        if impl is None:
            return _result(req_id, _tool_result(
                f"No such tool: {name!r}. Available: "
                + ", ".join(_TOOL_IMPLS),
                is_error=True,
            ))

        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _result(req_id, _tool_result(
                "Tool arguments must be an object.", is_error=True,
            ))

        # Authorisation and metering happen here, once, for every tool -- not
        # inside the implementations, where a new tool could forget them.
        try:
            metered = _authorise(request)
        except HTTPException as exc:
            # The rate-limit pitch. See the module docstring: this is the one
            # place a limit is spoken to the buyer at the moment they wanted
            # more, so it goes to the model rather than to the transport.
            return _result(req_id, _tool_result(str(exc.detail), is_error=True))

        try:
            text, structured = impl(args, metered)
        except Exception as exc:  # noqa: BLE001 - a tool error must still say why
            log.exception("mcp_tool_failed", tool=name, error=str(exc))
            return _result(req_id, _tool_result(
                f"Something went wrong running {name}: {exc}", is_error=True,
            ))

        return _result(req_id, _tool_result(text, structured))

    return _error(req_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")


_INSTRUCTIONS = (
    "BalanceProof serves US public company balance sheets taken from SEC "
    "EDGAR, with every filing checked against the accounting identity "
    "Assets = Liabilities + Equity. Its distinguishing feature is that it "
    "reports when a filing does NOT reconcile, and why, rather than returning "
    "a number either way. Resolve a company name with search_companies, read "
    "figures with get_balance_sheet, and use check_balance_sheet for any "
    "question about whether the data can be trusted. Figures are as-filed and "
    "not restated. This is not investment advice."
)


# ---------------------------------------------------------------------------
# Header mirroring (modern era only)
# ---------------------------------------------------------------------------


def _decode_header(value: str) -> str:
    """Undo the Base64 sentinel a client uses for header-unsafe values.

    `=?base64?<payload>?=`. The markers are case-sensitive and lowercase by
    specification, so they are compared exactly.
    """
    import base64

    if value.startswith("=?base64?") and value.endswith("?="):
        raw = value[len("=?base64?"):-len("?=")]
        try:
            return base64.b64decode(raw).decode("utf-8")
        except Exception:  # noqa: BLE001 - a malformed value is a mismatch
            return value
    return value


def _validate_headers(
    request: Request, method: str, params: dict[str, Any], version: str
) -> str | None:
    """Check the mirrored headers against the body. Returns a reason, or None.

    Modern-era only. The rule exists because an intermediary may route on the
    header while the server executes the body, and a disagreement between the
    two is exactly the seam an attacker would aim at -- so a mismatch is
    refused rather than resolved in favour of either side.
    """
    declared = request.headers.get("mcp-protocol-version")
    if not declared:
        return "MCP-Protocol-Version header is missing."
    if declared != version:
        return (
            f"MCP-Protocol-Version header {declared!r} does not match the "
            f"body value {version!r}."
        )

    header_method = request.headers.get("mcp-method")
    if not header_method:
        return "Mcp-Method header is missing."
    if header_method != method:
        return (
            f"Mcp-Method header {header_method!r} does not match the body "
            f"method {method!r}."
        )

    # `Mcp-Name` is required only for the methods that carry a name or a uri.
    if method in ("tools/call", "resources/read", "prompts/get"):
        body_name = str(params.get("name") or params.get("uri") or "")
        header_name = request.headers.get("mcp-name")
        if not header_name:
            return "Mcp-Name header is missing."
        if _decode_header(header_name) != body_name:
            return (
                f"Mcp-Name header does not match the body value "
                f"{body_name!r}."
            )
    return None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _origin_ok(request: Request) -> bool:
    """Reject a cross-site Origin, per the transport's DNS-rebinding rule.

    Only an Origin that is PRESENT and foreign is refused. An absent Origin is
    the ordinary case here -- MCP clients are not browsers and do not send one
    -- so treating absence as hostile would reject every real caller.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    allowed = (
        "https://balanceproof.dev",
        "https://www.balanceproof.dev",
        "http://localhost",
        "http://127.0.0.1",
    )
    return any(origin == a or origin.startswith(a + ":") for a in allowed)


@router.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    """The single MCP endpoint. One JSON-RPC message per POST."""
    if not _origin_ok(request):
        return JSONResponse(
            _error(None, ERR_INVALID_REQUEST, "Origin not allowed."),
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON is a parse error, not a 500
        return JSONResponse(
            _error(None, ERR_PARSE, "Request body is not valid JSON."),
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            _error(None, ERR_INVALID_REQUEST, "Body must be a JSON-RPC object."),
            status_code=400,
        )

    req_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # A notification -- no `id`. The transport wants 202 and an empty body,
    # and the only one that reaches a server in practice is the legacy
    # `notifications/initialized`, which needs no action because this server
    # keeps no per-connection state to initialise.
    if req_id is None and method.startswith("notifications/"):
        return Response(status_code=202)

    if not method:
        return JSONResponse(
            _error(req_id, ERR_INVALID_REQUEST, "No method given."),
            status_code=400,
        )

    # --- era detection -----------------------------------------------------
    meta = params.get("_meta") or {}
    declared = meta.get(_META_VERSION) if isinstance(meta, dict) else None
    modern = bool(declared)

    headers: dict[str, str] = {}

    if modern:
        version = str(declared)
        if version not in SUPPORTED_PROTOCOLS:
            return JSONResponse(
                _error(
                    req_id,
                    ERR_INVALID_REQUEST,
                    f"Unsupported protocol version {version!r}.",
                    {"supported": list(SUPPORTED_PROTOCOLS)},
                ),
                status_code=400,
            )
        reason = _validate_headers(request, method, params, version)
        if reason:
            return JSONResponse(
                _error(req_id, ERR_HEADER_MISMATCH, f"Header mismatch: {reason}"),
                status_code=400,
            )
    else:
        # Legacy. Mint a session id on the handshake so a client that expects
        # one sees one; nothing is stored, and any value is accepted later.
        if method == "initialize":
            headers["Mcp-Session-Id"] = uuid.uuid4().hex

    try:
        payload = _dispatch(method, params, req_id, request)
    except Exception as exc:  # noqa: BLE001 - the endpoint must not 500 silently
        log.exception("mcp_dispatch_failed", method=method, error=str(exc))
        payload = _error(req_id, ERR_INTERNAL, f"Internal error: {exc}")

    # An unknown method is 404 with a JSON-RPC body by the transport's rule:
    # the body is what distinguishes it from a legacy server that simply does
    # not host this path.
    status = 200
    if "error" in payload and payload["error"]["code"] == ERR_METHOD_NOT_FOUND:
        status = 404

    return JSONResponse(payload, status_code=status, headers=headers)


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
async def mcp_method_not_allowed() -> Response:
    """GET opened the old standalone SSE stream and DELETE ended a session.

    The current revision has neither, and the transport says a server that
    implements only it answers both with 405. Answering rather than 404ing
    matters: a 404 tells a fallback-capable client this path is not an MCP
    endpoint at all, and it would go looking for the deprecated transport.
    """
    return Response(status_code=405, headers={"Allow": "POST"})


def health() -> dict[str, Any]:
    """What `/status` reports about this endpoint. Cheap and side-effect free."""
    return {
        "tools": [t["name"] for t in TOOLS],
        "protocols": list(SUPPORTED_PROTOCOLS),
        "checked": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
