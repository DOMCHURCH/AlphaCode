"""The API reference at /api — a page, not a payload.

/api used to return the JSON index that now lives at /api.json. It was in the
navigation bar, which meant the one link a reader clicked to find out what the
API does answered with a wall of braces. A machine-readable index is a good
thing to have and a bad thing to be linked to from a nav bar, so both exist and
each is at the address that suits it.

Everything on this page is generated from the same settings the endpoints
enforce, so the free-tier number here cannot drift from the number that
actually gates a call. The examples are real: they name the deployment's own
host and the header the code checks, and every one of them can be pasted into
a terminal as-is.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell


def _ep(method: str, path: str, auth: str, blurb: str) -> str:
    """One endpoint row: what to call, whether it needs a key, and why."""
    return f"""
    <div class="ep">
      <div class="ep-sig">
        <span class="ep-m ep-m-{method.lower()}">{method}</span>
        <code class="ep-p">{escape(path)}</code>
      </div>
      <p class="ep-auth">{escape(auth)}</p>
      <p class="ep-b">{blurb}</p>
    </div>"""


def _scale_clause() -> str:
    """"6,201 companies, 1.8M as-reported data points, " -- or "" if unreadable.

    Read live. These two figures were literals here, on the home page, in the
    blog and in llms.txt, and they had already drifted from the ones /dataset
    computes off the same table.
    """
    from src.dataset import facts_label
    from src.report.home_page import companies_label

    facts, companies = facts_label(), companies_label()
    if not facts or not companies:
        return ""
    return f"{companies} companies, {facts} as-reported data points, "


def render_api(
    *,
    nav: str = "",
    base_url: str = "https://alphacode-production.up.railway.app",
    free_calls: int = 1000,
    pro_calls: int = 10000,
    dataset_price: str = "$79.99",
    demo_calls_per_hour: int = 100,
) -> str:
    """The reference. `base_url` is the deployment's own origin so the curl
    examples work from the machine reading them, not from a machine that
    happens to be running the same code somewhere else."""
    from src.report.legal import SHORT_DISCLAIMER
    from src.report.nav import render_footer

    host = escape(base_url.rstrip("/"))

    body = f"""{nav}
<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">SEC Filings API Reference</h1>
    <p class="hlede">Every balance sheet on this site, as JSON. One header, one
      key, no SDK. Every figure is reconciled against A = L + E before it is
      stored. <a href="/blog/sec-xbrl-data-wrong-one-in-five">Here is why that
      matters</a>. Filed figures only: nothing here is derived, scored or
      predicted.</p>
  </header>

  <section class="sec">
    <div class="sec-head"><h2>Getting a key</h2></div>
    <p class="sec-sub">A key is issued the moment you give an address. The free
      tier needs no payment and no card. It is not a trial that stops.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a free API key</a>
      <a class="btn ghost" href="/#pricing">See the plans</a>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Authentication</h2></div>
    <p class="sec-sub">Send the key in an <code>X-API-Key</code> header. It is
      not a bearer token, it does not expire, and there is no OAuth dance.
      Treat it like a password: anyone holding it can spend your calls. Your
      key is stored as a hash. The full value is shown once at issue;
      regenerate from the dashboard if lost.</p>
    <pre class="code api-code"><code>curl -H "X-API-Key: YOUR_KEY" \\
  {host}/api/company/JPM</code></pre>
    <p class="plan-note">Lost it? <a href="/dashboard">Sign in</a>. The
      dashboard shows the first eight characters, so you can tell which key
      an account is holding, and a Regenerate button that issues a new one.
      The old key stops working the moment you press it.</p>
  </section>

  <section class="sec" id="python">
    <div class="sec-head"><h2>Python</h2></div>
    <p class="sec-sub">The official client wraps every endpoint below. It needs
      nothing beyond the standard library; pandas is only for
      <code>panel()</code>, which returns one row per company and period,
      ready for a backtest.</p>
    <pre class="code api-code"><code>pip install "balanceproof[pandas]"

import balanceproof as bp
client = bp.Client("YOUR_KEY")          # or set BALANCEPROOF_API_KEY
client.statements("AAPL", period="quarterly")
client.balance_sheet("AAPL", as_of="2025-03-01")      # Pro and above
df = client.panel(["AAPL", "MSFT"], ["revenue", "net_income"])</code></pre>
    <p class="plan-note">On <a href="https://pypi.org/project/balanceproof/"
      rel="noopener">PyPI</a>. A call your plan does not include raises
      <code>balanceproof.PlanRequired</code>, naming the plan that does.</p>
    <p class="plan-note">Choosing between providers? The four tests worth
      running on any of them, this one included:
      <a href="/best/sec-filings-api-for-quants">SEC filings API for quants</a>
      and <a href="/best/balance-sheet-api">balance sheet API</a>.</p>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Endpoints</h2></div>
    <div class="eps">
{_ep("GET", "/api/company/{ticker}", "X-API-Key required · counts as one call",
     "The balance sheet as filed: every line item, its period end, and the "
     "filing it came from. The same numbers the drawing on "
     "<code>/company/{ticker}</code> is built from. Add "
     "<code>?as_of=YYYY-MM-DD</code> (Pro and above) for the balance sheet "
     "exactly as it was public that day: only filings made by then, the "
     "original figure before a restatement and the revision after.")}
{_ep("GET", "/api/company/{ticker}/history?years=N",
     "X-API-Key required · counts as one call",
     "Every filed balance sheet over past periods, newest first. How far back "
     "depends on your plan: Free 1 year, Starter 5, Pro 10, Business all that "
     "is loaded. The response says how far back the data actually goes.")}
{_ep("GET", "/api/company/{ticker}/statements?period=annual|quarterly",
     "X-API-Key required · counts as one call · as_of Pro and above",
     "Income statement and cash flow per period, each checked: revenue minus "
     "cost of revenue against gross profit, and operating + investing + "
     "financing + FX against the change in cash, with the gap when it fails. "
     "Q4, and Q2/Q3 cash flow, are worked out from the filed year and "
     "year-to-date figures and named in <code>derived</code>. Depth as for "
     "history.")}
{_ep("GET", "/api/exceptions?since=&type=&ticker=&attribution=&format=json|csv",
     "X-API-Key required · Pro (last 90 days) and Business (all) · one call",
     "Every filing whose balance sheet fails the check, with whose problem it "
     "is (<code>filer</code>, <code>extraction</code> or <code>unknown</code>), "
     "and every figure a later filing restated, across all companies, newest "
     "first. <code>format=csv</code> for a download. The last 90 days are free "
     "to browse at <a href=\"/restatements\">/restatements</a>.")}
{_ep("GET", "/api/company/{ticker}/changes", "X-API-Key required · Starter and above · one call",
     "What moved since the previous period, figure by figure, and every "
     "figure a later filing restated: the original value, the revised one, "
     "and which filings said each.")}
{_ep("POST", "/api/verify", "X-API-Key required · Pro (50 per request) and Business (500) · one call per ticker",
     "Check many balance sheets at once: body <code>{&quot;tickers&quot;: [&quot;AAPL&quot;, &quot;JPM&quot;]}</code>. "
     "For each: whether Assets = Liabilities + Equity holds, the gap as a "
     "percentage of assets, and why it balances when it needed the minority "
     "interest. On Business, every result and every balance sheet also carries "
     "<code>provenance</code>: the filing it came from, with a link to it on SEC.gov.")}
{_ep("POST", "/api/watchlist", "X-API-Key required · Starter and above · free",
     "Watch a company: body <code>{&quot;ticker&quot;: &quot;AAPL&quot;}</code>. When it files, you get "
     "one email saying whether the new balance sheet reconciles, what moved, and "
     "anything restated. Starter watches 3, Pro 50, Business 500. "
     "<code>GET /api/watchlist</code> lists them, "
     "<code>DELETE /api/watchlist/{ticker}</code> removes one.")}
{_ep("POST", "/api/webhooks", "X-API-Key required · Business (5 endpoints) · free",
     "Register an https URL: body <code>{&quot;url&quot;: &quot;https://...&quot;}</code>. When a "
     "watched company files, it receives a <code>watchlist.filed</code> POST with the same "
     "facts as the email. Each request carries <code>BalanceProof-Signature: t=..,v1=..</code>, "
     "an HMAC-SHA256 of <code>t.body</code> with the secret returned once at creation. "
     "<code>POST /api/webhooks/{id}/test</code> sends a signed ping.")}
{_ep("GET", "/api/plans", "Public",
     "What each plan includes, as data: call quotas, history depth, "
     "watchlist size and the rest.")}
{_ep("GET", "/api/demo/{ticker}",
     (f"No key · {demo_calls_per_hour}/hour per address"
      if demo_calls_per_hour else "No key · rate limited"),
     "The same response, so you can see the shape before deciding whether to "
     "register. It is the keyed endpoint underneath, so it is metered -- "
     "generously, and not per key.")}
{_ep("GET", "/api/user/status", "X-API-Key required · free",
     "Your tier, the calls you have spent this calendar month, and what is "
     "left. Checking never costs a call.")}
{_ep("GET", "/api/download-dataset", f"X-API-Key required · {escape(dataset_price)} one-off",
     "Every fundamental figure this site holds, streamed as CSV. One purchase, "
     "no subscription, re-downloadable whenever you like.")}
{_ep("GET", "/api.json", "Public",
     "This page's machine-readable index: the endpoint list, as JSON.")}
{_ep("GET", "/health", "Public",
     "Whether the service is up and can reach its database. Cheap, and the "
     "one to point an uptime monitor at.")}
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Limits</h2></div>
    <p class="sec-sub">Counted per calendar month and reset on the first,
      whatever day you registered. A call that fails on our side is not
      counted.</p>
    <div class="tablewrap">
      <table class="compare">
        <thead>
          <tr><th scope="col">Plan</th><th scope="col">Calls / month</th>
            <th scope="col">Over the limit</th></tr>
        </thead>
        <tbody>
          <tr><td>Free</td><td>{free_calls:,}</td>
            <td>429, with the reset date</td></tr>
          <tr><td>Pro</td><td>{pro_calls:,}</td>
            <td>429, with the reset date</td></tr>
        </tbody>
      </table>
    </div>
    <p class="plan-note">An expired Pro subscription falls back to the free
      allowance. It does not lock you out, and the key does not change.</p>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Responses</h2></div>
    <p class="sec-sub">JSON on every path, including the errors: a failure
      never returns an HTML page to something that asked for data.</p>
    <div class="tablewrap">
      <table class="compare">
        <thead>
          <tr><th scope="col">Code</th><th scope="col">Means</th></tr>
        </thead>
        <tbody>
          <tr><td>200</td><td>Here it is.</td></tr>
          <tr><td>401</td><td>No key, or a key that is not ours.</td></tr>
          <tr><td>404</td><td>Nothing filed for that ticker.</td></tr>
          <tr><td>429</td><td>Month spent, or the demo's hourly cap.</td></tr>
        </tbody>
      </table>
    </div>
    <pre class="code api-code"><code># what a spent month looks like
curl -sS -o /dev/null -w "%{{http_code}}\\n" \\
  -H "X-API-Key: YOUR_KEY" {host}/api/company/AAPL
429</code></pre>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Try it without a key</h2></div>
    <p class="sec-sub">Five a day from any one address, no registration.</p>
    <!-- LABELLED, and the Windows one is not a footnote.
         This block first carried `| head -40`: two bugs in one pipe, since
         `head` does not exist on Windows and the response is one line of
         compact JSON so it cut nothing on Unix either. Dropping the pipe was
         not enough. PowerShell aliases `curl` to Invoke-WebRequest, which
         rejects -sS, so the bare curl line still failed there -- and the
         explanation sat UNDER the command, which is not where anybody looks
         before pasting the first code block they see. A reader hit exactly
         that twice. Each platform now gets its own labelled line, so the
         choice is made before the paste rather than after the error.

         `curl.exe` rather than `curl`: the .exe is what reaches the real
         curl Windows has shipped since 1803, past the alias. Bare `curl`
         with no flags is not a fix either -- Invoke-WebRequest uses the IE
         engine in PowerShell 5.1 and fails where it has never been run. -->
    <p class="plan-note">macOS and Linux:</p>
    <pre class="code api-code"><code>curl -sS {host}/api/demo/JPM</code></pre>
    <p class="plan-note">Windows PowerShell, where <code>curl</code> is an
      alias for <code>Invoke-WebRequest</code> and will not take those
      flags:</p>
    <pre class="code api-code"><code>curl.exe -sS {host}/api/demo/JPM</code></pre>
    <p class="plan-note">Or PowerShell's own client, which parses the
      response instead of printing it:</p>
    <pre class="code api-code"><code>Invoke-RestMethod {host}/api/demo/JPM</code></pre>
  </section>

  <p class="disclaim">{escape(SHORT_DISCLAIMER)}</p>
{render_footer()}
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>"""
    from src.report.schema import breadcrumb_ld, software_ld

    return shell(
        "SEC Filings API Reference — BalanceProof",
        body,
        description=(
            "SEC XBRL balance sheets over REST. One X-API-Key header, JSON "
            f"back, no SDK. Checked against A = L + E. {_scale_clause()}"
        ),
        canonical="/api",
        ld=software_ld() + breadcrumb_ld([("Home", "/"), ("API", "/api")]),
    )
