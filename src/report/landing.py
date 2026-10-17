"""Product pages that answer one search: /xbrl-balance-sheet-api and its siblings.

These are not comparisons. A comparison page (`compare.py`) argues one product
against another; a page here describes ONE thing this site does, shows a real
response from it, and says plainly what it does not do. Each has a self
canonical, a title of 60 characters or fewer, a description of 155 or fewer,
and links to the pages beside it and to /api.

THE RULE FOR THE SAMPLES. Every figure shown is read from the database when
the page is rendered, through the same `get_balance_sheet` the API serves. If
the database has nothing for the sample company the sample section is left
out; it is never replaced with an invented one.

NO COVERAGE CLAIMS BEYOND THE DATA. Periodic balance sheets only. Nothing here
says the site reads event filings, and `tests/test_landing_pages.py` fails the
build if one of these pages does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from html import escape

import structlog

from src.report.company_page import asset_version
from src.report.home_page import SITE_ORIGIN, shell

log = structlog.get_logger(__name__)

SAMPLE_TICKER = "AAPL"


@dataclass(frozen=True)
class Landing:
    slug: str
    h1: str
    seo_title: str
    description: str
    lede: str
    updated: str
    body: Callable[[], str]
    faq: Callable[[], tuple[tuple[str, str], ...]]
    # (href, label, why) for the "Next to this page" list. Only pages that exist.
    related: tuple[tuple[str, str, str], ...] = ()
    # The path is also an API endpoint (`/mcp`). The route then answers a GET
    # that asks for an event stream with 405, as that transport requires, and
    # serves the page to everything else.
    get_conflict: bool = False


# ---------------------------------------------------------------------------
# Real samples
# ---------------------------------------------------------------------------
# The lines worth showing, in the order a reader looks for them. The response
# has many more; the page says so.
_SHOWN = {
    "assets": ("total_assets", "cash", "receivables", "property_plant_equipment"),
    "liabilities": ("total_liabilities", "long_term_debt"),
    "equity": ("shareholders_equity",),
}


def sample_response(ticker: str = SAMPLE_TICKER) -> dict | None:
    """A trimmed copy of the real REST response for `ticker`, or None.

    Trimmed by selecting keys, never by editing values: what is printed is a
    subset of what `/api/company/{ticker}` returns, field for field.
    """
    try:
        from src.company.balancesheet import get_balance_sheet

        sheet = get_balance_sheet(ticker)
    except Exception as exc:  # noqa: BLE001 - a sample must never take a page down
        log.warning("landing_sample_failed", ticker=ticker, error=str(exc)[:200])
        return None
    if sheet is None:
        return None
    full = asdict(sheet)
    out: dict = {
        "ticker": full["ticker"],
        "company_name": full["company_name"],
        "period_end": full["period_end"].isoformat(),
        "filing_date": full["filing_date"].isoformat(),
    }
    for group, keys in _SHOWN.items():
        picked = {}
        for key in keys:
            cell = full[group].get(key)
            if cell is None or cell.get("missing") or cell.get("value") is None:
                continue
            picked[key] = {
                "concept": cell["concept"],
                "value": cell["value"],
                "period_end": cell["period_end"].isoformat(),
                "filing_date": cell["filing_date"].isoformat(),
                "missing": False,
                "restated": cell["restated"],
            }
        if picked:
            out[group] = picked
    if "total_assets" not in out.get("assets", {}):
        return None
    out["missing_concepts"] = full["missing_concepts"]
    out["data_quality_issues"] = full["data_quality_issues"]
    out["source"] = "SEC Financial Statement Data Sets (as reported)"
    return out


def _json_block(sample: dict) -> str:
    """Pretty JSON with the long lists elided, escaped for HTML."""
    shown = dict(sample)
    missing = shown.pop("missing_concepts", [])
    text = json.dumps(shown, indent=2)
    if missing:
        keep = missing[:3]
        tail = ", ".join(json.dumps(m) for m in keep)
        extra = f", ... {len(missing) - 3} more" if len(missing) > 3 else ""
        text = text[:-2] + f',\n  "missing_concepts": [{tail}{extra}]\n}}'
    return escape(text)


def check_line(ticker: str = SAMPLE_TICKER) -> tuple[str, str] | None:
    """(arithmetic line, verdict) for the sample, from the same view the
    company page draws, or None."""
    try:
        from src.company.view1 import build_view1
        from src.report.company_facts import arithmetic_line, balance_verdict

        view = build_view1(ticker)
    except Exception as exc:  # noqa: BLE001
        log.warning("landing_check_failed", ticker=ticker, error=str(exc)[:200])
        return None
    if view is None:
        return None
    line = arithmetic_line(view)
    if not line:
        return None
    return line, balance_verdict({
        "balances": view.reconciles, "basis": view.identity_basis or "",
        "imbalance_pct": view.imbalance_pct,
    })


# ---------------------------------------------------------------------------
# Shared furniture
# ---------------------------------------------------------------------------
def _free_calls() -> str:
    from src.config.settings import get_settings

    return f"{get_settings().free_tier_monthly_calls:,}"


def _cta() -> str:
    return """
  <aside class="sec cta">
    <div class="sec-head"><h2>Try it on a company you know</h2></div>
    <p class="sec-sub">Free key, no card. Pick a company whose balance sheet you
      have read, call the API, and compare the response with the filing on
      EDGAR.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a free API key</a>
      <a class="btn ghost" href="/api">API reference</a>
      <a class="btn ghost" href="/pricing">Pricing</a>
    </div>
  </aside>"""


def _related(page: Landing) -> str:
    items = "".join(
        f'<li><a href="{escape(href)}">{escape(label)}</a> &mdash; '
        f"{escape(why)}</li>"
        for href, label, why in page.related
    )
    return f"""
  <section class="sec" id="related">
    <div class="sec-head"><h2>Next to this page</h2></div>
    <ul class="notelist">{items}</ul>
  </section>"""


def _faq_html(pairs: tuple[tuple[str, str], ...]) -> str:
    items = "".join(f"<h3>{escape(q)}</h3><p>{escape(a)}</p>" for q, a in pairs)
    return f"""
  <section class="sec" id="faq">
    <div class="sec-head"><h2>Common questions</h2></div>
    {items}
  </section>"""


def render(page: Landing, *, nav: str = "") -> str:
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld, faq_ld, webpage_ld

    pairs = page.faq()
    footer = render_footer(
        "Figures are as reported to the SEC. This describes what a company "
        "filed; it is not advice and makes no prediction."
    )
    body = f"""{nav}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <h1 class="htitle">{escape(page.h1)}</h1>
      <p class="hlede">{escape(page.lede)}</p>
      <div class="api-cta hero-cta">
        <a class="btn" href="/dashboard">Get a free API key</a>
        <a class="btn ghost" href="/api">API reference</a>
      </div>
    </header>
    <div class="prose">{page.body()}</div>
  </article>
  {_faq_html(pairs)}
  {_cta()}
  {_related(page)}
{footer}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>"""
    ld = (
        webpage_ld(page.h1, page.description, f"/{page.slug}")
        + breadcrumb_ld([("Home", "/"), (page.h1, f"/{page.slug}")])
        + faq_ld(pairs)
    )
    return shell(
        page.seo_title, body,
        description=page.description, canonical=f"/{page.slug}", ld=ld,
    )


# ---------------------------------------------------------------------------
# /xbrl-balance-sheet-api
# ---------------------------------------------------------------------------
def _xbrl_body() -> str:
    from src.report.compare import NAIVE_ACCURACY, live_counts

    counts = live_counts()
    filers = counts["{FILERS}"]
    host = SITE_ORIGIN
    sample = sample_response()
    check = check_line()

    sample_html = ""
    if sample is not None:
        sample_html = f"""
<h2>A real response</h2>
<p>This is the response for {escape(sample["company_name"] or sample["ticker"])}
({escape(sample["ticker"])}), period ended {escape(sample["period_end"])},
filed {escape(sample["filing_date"])}, read from the database when this page
was rendered. It is a subset of the fields: the full response lists every
balance sheet line the filer reported, and names the ones it did not.</p>
<pre class="code api-code"><code>{_json_block(sample)}</code></pre>"""
        if check:
            line, verdict = check
            sample_html += f"""
<p>The check, run on those figures:</p>
<pre class="code api-code"><code>{escape(line)}</code></pre>
<p>Result: <strong>{escape(verdict.lower())}</strong>. The same line is printed
on <a href="/company/{escape(sample["ticker"])}">the company page</a>; over
the API, <code>POST /api/verify</code> (Pro and above) returns the pass or fail,
the gap as a share of assets, and the basis it balanced on, for up to 50 tickers
per request on Pro.</p>"""

    return f"""
<h2>One request, one balance sheet</h2>
<p>Send a ticker, get that company&rsquo;s latest balance sheet from its SEC
XBRL filing as JSON. {escape(filers)} are covered. Each line item comes with
its name, its value in dollars, the period it is for, the date of the
filing it was read from, and flags for whether it is missing or restated.</p>
<pre class="code api-code"><code>curl -H "X-API-Key: YOUR_KEY" \\
  {host}/api/company/{escape(SAMPLE_TICKER)}</code></pre>
<p>No key yet? <code>{host}/api/demo/{escape(SAMPLE_TICKER)}</code> returns the
same response with no sign-up, rate limited per address. A free key gives you
{_free_calls()} calls a month with no card.</p>
{sample_html}

<h2>Why a balance sheet from XBRL needs a check</h2>
<p>Open a large filer&rsquo;s 10-Q and search for <code>Assets</code> and you
find many facts, not one: the consolidated total plus a separate fact for every
segment and subsidiary the filer has to break out. All of them are valid XBRL.
Only one is the number on the face of the balance sheet, and the only thing
marking it is that it carries no dimensions. An extractor that takes the first
match, the largest, or the most recent is right most of the time and wrong
without leaving a trace; measured on the filings loaded here, taking the naive
pick was {escape(NAIVE_ACCURACY)}.</p>
<p>This API picks the combination of assets, liabilities and equity that
satisfies <strong>Assets = Liabilities + Equity</strong>, and it publishes the
filings that do not balance with the reason rather than adjusting them until
they do. The method is written out on
<a href="/methodology">how we verify</a>, and the measurement behind the claim
is in <a href="/blog/sec-xbrl-data-wrong-one-in-five">why SEC XBRL data is
wrong one time in five</a>.</p>

<h2>What it returns, and what it does not</h2>
<ul class="notelist">
  <li><strong>As filed.</strong> Figures are what the company reported in its
  periodic filings, as read from the SEC Financial Statement Data Sets. Nothing
  is estimated, smoothed or backfilled.</li>
  <li><strong>Missing is missing.</strong> A line the filer did not report is
  listed under <code>missing_concepts</code> and has <code>missing: true</code>.
  It is never returned as zero.</li>
  <li><strong>Balance sheet first.</strong> Income statement and cash flow are
  at <code>/api/company/{{ticker}}/statements</code>, history at
  <code>/api/company/{{ticker}}/history</code>; depth depends on the plan, and
  <code>/api/plans</code> lists it as data.</li>
  <li><strong>No prices.</strong> This is filing data. It does not return
  quotes, market caps or estimates.</li>
  <li><strong>Not real time.</strong> A balance sheet appears after the company
  files it and the filing is loaded; it is not a feed of the filing as it
  lands.</li>
</ul>

<h2>From Python</h2>
<pre class="code api-code"><code>pip install balanceproof

import balanceproof as bp
client = bp.Client("YOUR_KEY")        # or set BALANCEPROOF_API_KEY
client.balance_sheet("{escape(SAMPLE_TICKER)}")</code></pre>
<p>The client is on <a href="https://pypi.org/project/balanceproof/"
rel="noopener">PyPI</a> and wraps every endpoint on the
<a href="/api">API reference</a>.</p>
"""


def _xbrl_faq() -> tuple[tuple[str, str], ...]:
    return (
        (
            "What is an XBRL balance sheet API?",
            "It returns a company's balance sheet, taken from the XBRL data "
            "the company files with the SEC, as structured JSON instead of "
            "a document you have to parse. BalanceProof returns each line "
            "item with its concept, value, period and source filing.",
        ),
        (
            "Where does the balance sheet data come from?",
            "From the XBRL financial data companies file with the SEC on "
            "EDGAR, read through the SEC Financial Statement Data Sets. "
            "Figures are as reported, with no estimates or smoothing.",
        ),
        (
            "How do I know a balance sheet from the API is right?",
            "Each one is checked against the accounting identity Assets = "
            "Liabilities + Equity before it is published. A filing that does "
            "not balance is published with the reason, such as a "
            "noncontrolling interest, a component that was not read, or a "
            "filing whose own totals disagree, instead of being adjusted.",
        ),
        (
            "Is there a free XBRL balance sheet API?",
            f"Yes. A free key gives {_free_calls()} calls a month with no "
            "card, and a keyless demo endpoint returns the same response "
            "for one company at a time, rate limited per address.",
        ),
    )


XBRL_BALANCE_SHEET_API = Landing(
    slug="xbrl-balance-sheet-api",
    h1="XBRL balance sheet API for US public companies",
    seo_title="XBRL Balance Sheet API for SEC Filings — BalanceProof",
    description=(
        "A REST API for SEC XBRL balance sheets as JSON, each checked against "
        "Assets = Liabilities + Equity. Free key, no card. Real response below."
    ),
    lede=(
        "Send a ticker, get the latest balance sheet from the company's SEC "
        "XBRL filing as JSON, checked against Assets = Liabilities + Equity "
        "before it is served."
    ),
    updated="2026-10-09",
    body=_xbrl_body,
    faq=_xbrl_faq,
    related=(
        ("/api", "API reference", "every endpoint, with plan limits."),
        ("/methodology", "How we verify", "what the check is and how the exceptions are counted."),
        ("/best/sec-xbrl-api", "How to choose an SEC XBRL API", "four tests to run on any provider."),
        ("/best/balance-sheet-api", "How to choose a balance sheet API", "the buyer's guide, not the product page."),
        ("/alternatives/sec-api", "Alternatives to sec-api.io", "the document or the figures inside it."),
        ("/free-sec-fundamentals-api", "Free SEC fundamentals API", "exactly what the free key includes."),
        ("/pricing", "Pricing", "what each plan includes."),
        (f"/company/{SAMPLE_TICKER}", f"{SAMPLE_TICKER} balance sheet", "the same figures, drawn to scale."),
    ),
)

# ---------------------------------------------------------------------------
# /free-sec-fundamentals-api
# ---------------------------------------------------------------------------
def _cell(feature: str, value) -> str:
    """One plans.MATRIX cell, as a reader would say it."""
    if value is None:
        return "No limit"
    if value is False or value == 0:
        return "Not included"
    if value is True:
        return "Included"
    one = value == 1
    unit = {
        "history_years": "year back" if one else "years back",
        "watchlist": "company watched" if one else "companies watched",
        "bulk_verify": "ticker per request" if one else "tickers per request",
        "webhooks": "endpoint" if one else "endpoints",
        "exceptions_feed": "day" if one else "days",
    }.get(feature)
    return f"{value} {unit}" if unit else str(value)


def free_history_sample(ticker: str = SAMPLE_TICKER) -> dict | None:
    """What `GET /api/company/{ticker}/history` returns on the Free plan.

    Built by the same `history()` the route calls, with the years the plan
    allows, then trimmed to three totals per period by selecting keys. None if
    the database has nothing for the ticker.
    """
    from src import plans

    cap = plans.allowance("free", "history_years")
    try:
        from src.company.history import history

        out = history(ticker, cap)
    except Exception as exc:  # noqa: BLE001 - a sample must never take a page down
        log.warning("landing_history_failed", ticker=ticker, error=str(exc)[:200])
        return None
    if not out or not out.get("balance_sheets"):
        return None

    def total(sheet: dict, group: str, key: str):
        cell = sheet[group].get(key)
        return None if cell is None or cell.get("missing") else cell["value"]

    sheets = []
    for sheet in out["balance_sheets"][:2]:
        sheets.append({
            "period_end": sheet["period_end"].isoformat(),
            "filing_date": sheet["filing_date"].isoformat(),
            "total_assets": total(sheet, "assets", "total_assets"),
            "total_liabilities": total(sheet, "liabilities", "total_liabilities"),
            "shareholders_equity": total(sheet, "equity", "shareholders_equity"),
        })
    return {
        "ticker": out["ticker"],
        "years_requested": out["years_requested"],
        "years_allowed": cap,
        "plan": "free",
        "periods": out["periods"],
        "available_from": out["available_from"],
        "balance_sheets": sheets,
    }


def _free_body() -> str:
    from src import accounts, plans
    from src.report.compare import live_counts

    free_calls = _free_calls()
    filers = live_counts()["{FILERS}"]
    free_years = plans.allowance("free", "history_years")

    included = [
        "<li><code>GET /api/company/{ticker}</code> &mdash; the latest balance "
        "sheet, every line item, with the filing it came from.</li>",
        "<li><code>GET /api/company/{ticker}/history</code> &mdash; "
        f"{_cell('history_years', free_years)}. Ask for more and you are "
        "answered with what the plan allows, and the response says so in "
        "<code>years_allowed</code>.</li>",
        "<li><code>GET /api/company/{ticker}/statements</code> &mdash; income "
        "statement and cash flow, each checked, to the same depth.</li>",
        "<li><code>GET /api/user/status</code> and <code>GET /api/plans</code> "
        "&mdash; your remaining calls and this table as data. Checking "
        "your status never costs a call.</li>",
    ]

    rows = []
    for key in plans.MATRIX:
        free = plans.allowance("free", key)
        has = free is None or bool(free)
        needs = "&mdash;" if has else escape(
            plans.TIER_NAMES[plans.lowest_tier_with(key)] + " and above")
        label = escape(plans.FEATURE_LABELS[key].capitalize())
        rows.append(
            f'<tr><th scope="row">{label}</th>'
            f"<td>{escape(_cell(key, free))}</td><td>{needs}</td></tr>"
        )
    quotas = "".join(
        f'<tr><th scope="row">{escape(plans.TIER_NAMES[t])}</th>'
        f"<td>{accounts.tier_limit(t):,}</td></tr>"
        for t in ("free", "starter", "pro", "business")
    )

    sample = free_history_sample()
    sample_html = ""
    if sample is not None:
        sample_html = f"""
<h2>A real response from the free plan</h2>
<p>This is <code>GET /api/company/{escape(sample["ticker"])}/history</code> on the
Free plan, built by the same function the endpoint calls and read from the
database when this page was rendered. Each entry in the real response carries
every line item; three totals per period are shown here, and the first two
periods of {sample["periods"]}.</p>
<pre class="code api-code"><code>{escape(json.dumps(sample, indent=2))}</code></pre>"""

    return f"""
<h2>What the free key is</h2>
<p>A key from the <a href="/dashboard">dashboard</a>, with no card. It reads
SEC balance sheets for {escape(filers)} as JSON, each checked against
Assets = Liabilities + Equity before it is served, and it gives you
<strong>{free_calls} calls a month</strong>. Calls are counted per calendar
month and reset on the first, whatever day you signed up. A call that fails on
our side is not counted. Past the limit the API answers
<code>429</code> with the reset date; nothing is billed, because there is no
card on file.</p>
<pre class="code api-code"><code>curl -H "X-API-Key: YOUR_KEY" \\
  {SITE_ORIGIN}/api/company/{escape(SAMPLE_TICKER)}/history</code></pre>
<p>Want to look first? <code>{SITE_ORIGIN}/api/demo/{escape(SAMPLE_TICKER)}</code>
needs no key and is rate limited per address.</p>
{sample_html}

<h2>What Free includes</h2>
<ul class="notelist">{"".join(included)}</ul>

<h2>What is not in Free</h2>
<p>Read from the same table the API enforces, so this cannot drift from what a
call is actually allowed to do:</p>
<div class="tablewrap">
  <table class="compare">
    <caption class="vh">Features by plan</caption>
    <thead><tr><th scope="col">Feature</th><th scope="col">Free</th>
      <th scope="col">Needs</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>
<p>Calls per month by plan:</p>
<div class="tablewrap">
  <table class="compare">
    <caption class="vh">Monthly calls by plan</caption>
    <thead><tr><th scope="col">Plan</th><th scope="col">Calls a month</th></tr></thead>
    <tbody>{quotas}</tbody>
  </table>
</div>
<p>What each paid plan costs is on <a href="/pricing">the pricing page</a>,
which is the only copy that is kept current.</p>

<h2>What free does not mean here</h2>
<ul class="notelist">
  <li><strong>Not a trial.</strong> The key does not expire. A paid plan that
  lapses falls back to the free allowance and the key does not change.</li>
  <li><strong>Not market data.</strong> This is filing data: balance sheets,
  income statements and cash flows as reported to the SEC. No prices, no
  estimates.</li>
  <li><strong>Filed, not live.</strong> A figure appears after the company files
  and the filing is loaded.</li>
</ul>
"""


def _free_faq() -> tuple[tuple[str, str], ...]:
    from src import plans

    years = plans.allowance("free", "history_years")
    return (
        (
            "Is there a free SEC fundamentals API?",
            f"Yes. A free BalanceProof key gives {_free_calls()} calls a month "
            "of SEC balance sheets as JSON, with no card, and each balance "
            "sheet is checked against Assets = Liabilities + Equity.",
        ),
        (
            "What does the free plan include?",
            "The latest balance sheet for any covered company, balance sheet "
            f"history going back {years} year{'s' if years != 1 else ''}, and "
            "income statement and cash flow to the same depth. Change tracking, "
            "watchlists, bulk verification, filing links, webhooks, "
            "point-in-time queries and the exceptions feed need a paid plan.",
        ),
        (
            "What happens when I use all my free calls?",
            "The API answers with a 429 and the date the allowance resets. "
            "Calls are counted per calendar month and reset on the first. "
            "Nothing is charged, because no card is on file.",
        ),
        (
            "Do I need a credit card for the free API key?",
            "No. You sign up with an email address and get a key from the "
            "dashboard.",
        ),
    )


FREE_SEC_FUNDAMENTALS_API = Landing(
    slug="free-sec-fundamentals-api",
    h1="Free SEC fundamentals API: what the free key includes",
    seo_title="Free SEC Fundamentals API, No Card — BalanceProof",
    description=(
        "A free key for SEC balance sheets as JSON, each checked against "
        "A = L + E. No card. Exactly what Free includes and what needs a paid plan."
    ),
    lede=(
        "A free key reads SEC balance sheets as JSON with no card. This page "
        "lists what it includes and what it does not, from the same table the "
        "API enforces."
    ),
    updated="2026-10-09",
    body=_free_body,
    faq=_free_faq,
    related=(
        ("/xbrl-balance-sheet-api", "XBRL balance sheet API", "the endpoint in detail, with a real response."),
        ("/api", "API reference", "every endpoint, with plan limits."),
        ("/pricing", "Pricing", "what each paid plan includes and costs."),
        ("/best/free-financial-statement-api", "How to choose a free financial statement API", "what “free” means once you read how the quota is metered."),
        ("/alternatives/alpha-vantage", "Alternatives to Alpha Vantage", "if you ran out of free calls."),
        ("/dataset", "The full dataset", "every figure as one CSV, bought once."),
        ("/methodology", "How we verify", "what the check is."),
    ),
)

# ---------------------------------------------------------------------------
# /mcp
# ---------------------------------------------------------------------------
# One line per tool, for a person. The descriptions in `mcp_server.TOOLS` are
# written for the model; a test fails if a tool is added there without a line
# here, so the page cannot list fewer tools than the server serves.
_TOOL_BLURB = {
    "search_companies": "find a ticker from a company name",
    "get_balance_sheet": "the latest filed balance sheet, line by line",
    "check_balance_sheet": "does Assets = Liabilities + Equity hold on the filing, and if not, by how much and why",
    "get_balance_sheet_history": "past balance sheets, newest first",
    "get_balance_sheet_changes": "what moved since the previous period, and figures that differ between filings",
    "get_financial_statements": "income statement and cash flow; each period is checked when the filing reports the lines the check needs, otherwise not_testable",
    "get_exceptions": "filings that fail the check and figures that differ between filings, across every company",
    "get_api_key": "create a free key from inside the chat, after asking for your email and your agreement to the terms",
}


def mcp_check_sample(ticker: str = SAMPLE_TICKER) -> tuple[str, dict] | None:
    """The real `check_balance_sheet` tool result for `ticker`: the text a
    model is handed, and a few of the structured fields beside it.

    Produced by calling the tool function itself, not a copy of its wording.
    """
    try:
        from src.mcp_server import _tool_check_balance_sheet

        class _NoMeter:
            def spend(self, _ticker: str) -> None:
                return None

        text, payload = _tool_check_balance_sheet({"ticker": ticker}, _NoMeter())
    except Exception as exc:  # noqa: BLE001 - a sample must never take a page down
        log.warning("landing_mcp_sample_failed", ticker=ticker, error=str(exc)[:200])
        return None
    if not payload:
        return None
    keep = ("ticker", "period_end", "balances", "identity_basis",
            "imbalance_pct", "total_assets", "total_liabilities", "total_equity")
    return text, {k: payload[k] for k in keep if k in payload}


def _mcp_body() -> str:
    from src import plans
    from src.mcp_server import TOOLS

    free_calls = _free_calls()
    url = f"{SITE_ORIGIN}/mcp"
    tools = "".join(
        f"<li><code>{escape(t['name'])}</code> &mdash; "
        f"{escape(_TOOL_BLURB.get(t['name'], t['description'].split('. ')[0]))}.</li>"
        for t in TOOLS
    )
    changes = plans.TIER_NAMES[plans.lowest_tier_with("changes")]
    exceptions = plans.TIER_NAMES[plans.lowest_tier_with("exceptions_feed")]
    pit = plans.TIER_NAMES[plans.lowest_tier_with("point_in_time")]

    sample_html = ""
    sample = mcp_check_sample()
    if sample is not None:
        text, fields = sample
        sample_html = f"""
<h2>A real tool result</h2>
<p>This is what <code>check_balance_sheet</code> returns for
{escape(SAMPLE_TICKER)}, produced by calling the tool function against the
database when this page was rendered. The first block is the text the model is
handed; the second is some of the structured data beside it.</p>
<pre class="code api-code"><code>{escape(text)}</code></pre>
<pre class="code api-code"><code>{escape(json.dumps(fields, indent=2))}</code></pre>"""

    return f"""
<h2>Connect it</h2>
<p>BalanceProof runs a remote MCP server at <code>{url}</code>. There is
nothing to install and no package to run: you give your client the URL.</p>
<pre class="code api-code"><code>claude mcp add --transport http balanceproof {url}</code></pre>
<p>That is Claude Code. Most other clients take the same two things in their
connector settings, a URL and an optional headers map:</p>
<pre class="code api-code"><code>{{
  "mcpServers": {{
    "balanceproof": {{
      "type": "http",
      "url": "{url}"
    }}
  }}
}}</code></pre>
<p>Client settings differ and change; if yours names the fields differently,
the URL is the part that matters.</p>

<h2>With and without a key</h2>
<p>With no key the server runs on a shared demo allowance, rate limited per
address, which is enough to try it. A free key raises that to
<strong>{free_calls} calls a month</strong> tied to your account rather than
to a machine. Send it as an <code>Authorization: Bearer YOUR_KEY</code>
header (the REST API uses <code>X-API-Key</code> instead). Each tool result
reports your current plan and remaining calls in its metadata. Get a key from
the <a href="/dashboard">dashboard</a>, or ask the model to run
<code>get_api_key</code> and it will ask for your email address and your
agreement to the <a href="/terms">terms</a> first.</p>
<pre class="code api-code"><code>claude mcp add --transport http balanceproof {url} \\
  --header "Authorization: Bearer YOUR_KEY"</code></pre>

<h2>The tools</h2>
<ul class="notelist">{tools}</ul>
<p>A tool call counts as one call on your plan. Three need a paid plan:
<code>get_balance_sheet_changes</code> from {escape(changes)},
<code>get_exceptions</code> from {escape(exceptions)}, and the
<code>as_of</code> argument of <code>get_financial_statements</code> from
{escape(pit)}. History depth follows the plan, and the result says how far back
the data goes. The full table is on
<a href="/free-sec-fundamentals-api">what the free key includes</a>.</p>
{sample_html}

<h2>What to ask it</h2>
<ul class="notelist">
  <li>&ldquo;Does Apple&rsquo;s latest balance sheet actually balance?&rdquo;
  &mdash; the model reaches for <code>check_balance_sheet</code>.</li>
  <li>&ldquo;Show me Microsoft&rsquo;s liabilities and equity.&rdquo; &mdash;
  <code>get_balance_sheet</code>, after <code>search_companies</code> if you
  gave a name instead of a ticker.</li>
  <li>&ldquo;How has JPMorgan&rsquo;s balance sheet changed over the last
  year?&rdquo; &mdash; <code>get_balance_sheet_history</code>.</li>
</ul>

<h2>What it is not</h2>
<ul class="notelist">
  <li><strong>As filed.</strong> Figures are what companies reported to the SEC,
  not adjusted or restated by this server.</li>
  <li><strong>Filing data only.</strong> No prices, no estimates, no
  recommendations. It is not investment advice.</li>
  <li><strong>The same data as the API.</strong> The REST
  <a href="/xbrl-balance-sheet-api">balance sheet API</a> and this server read
  the same database; use REST when you are writing code and MCP when you are
  talking to a model.</li>
</ul>
"""


def _mcp_faq() -> tuple[tuple[str, str], ...]:
    return (
        (
            "What is the BalanceProof MCP server?",
            "A remote Model Context Protocol server at https://balanceproof.dev/mcp "
            "that lets an AI client read SEC balance sheets and check them "
            "against Assets = Liabilities + Equity.",
        ),
        (
            "How do I connect it to Claude?",
            "In Claude Code, run: claude mcp add --transport http balanceproof "
            "https://balanceproof.dev/mcp. Other clients take the same URL in "
            "their connector settings.",
        ),
        (
            "Do I need an API key to use the MCP server?",
            "No. Without a key it runs on a shared demo allowance, rate limited "
            f"per address. A free key gives {_free_calls()} calls a month; send "
            "it as an Authorization: Bearer header.",
        ),
        (
            "What can the BalanceProof MCP server do?",
            "Find a company's ticker, return its filed balance sheet, history, "
            "income statement and cash flow, and check whether the balance "
            "sheet balances and why not if it does not. Some tools need a "
            "paid plan.",
        ),
    )


MCP_SERVER = Landing(
    slug="mcp",
    h1="BalanceProof MCP server: SEC balance sheets for AI clients",
    seo_title="MCP Server for SEC Balance Sheets — BalanceProof",
    description=(
        "Connect Claude or any MCP client to SEC balance sheets checked against "
        "A = L + E. Remote server, free key, one command to set up."
    ),
    lede=(
        "A remote MCP server that gives an AI client SEC balance sheets and a "
        "way to check them against Assets = Liabilities + Equity. Paste the "
        "URL; there is nothing to install."
    ),
    updated="2026-10-09",
    body=_mcp_body,
    faq=_mcp_faq,
    # /mcp is also the JSON-RPC endpoint. A GET asking for an event stream is
    # an MCP client opening the old SSE channel and gets the 405 the transport
    # requires; every other GET is a person or a crawler and gets this page.
    get_conflict=True,
    related=(
        ("/xbrl-balance-sheet-api", "XBRL balance sheet API", "the same data over REST, with a real response."),
        ("/free-sec-fundamentals-api", "Free SEC fundamentals API", "what the free key includes."),
        ("/api", "API reference", "every REST endpoint."),
        ("/methodology", "How we verify", "what the check is and how the exceptions are counted."),
        ("/pricing", "Pricing", "what each plan includes."),
        ("/terms", "Terms", "what you agree to when you create a key."),
    ),
)

PAGES: tuple[Landing, ...] = (XBRL_BALANCE_SHEET_API, FREE_SEC_FUNDAMENTALS_API, MCP_SERVER)
BY_SLUG: dict[str, Landing] = {p.slug: p for p in PAGES}
