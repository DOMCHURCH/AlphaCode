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


def render_api(
    *,
    nav: str = "",
    base_url: str = "https://alphacode-production.up.railway.app",
    free_calls: int = 10,
    pro_calls: int = 10000,
    dataset_price: str = "$79.99",
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
    <h1 class="htitle">The API</h1>
    <p class="hlede">Every balance sheet on this site, as JSON. One header, one
      key, no SDK. Filed figures only — nothing here is derived, scored or
      predicted.</p>
  </header>

  <section class="sec">
    <div class="sec-head"><h2>Getting a key</h2></div>
    <p class="sec-sub">A key is issued the moment you give an address. The free
      tier needs no payment and no card — it is not a trial that stops.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a key</a>
      <a class="btn ghost" href="/#pricing">See the plans</a>
    </div>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Authentication</h2></div>
    <p class="sec-sub">Send the key in an <code>X-API-Key</code> header. It is
      not a bearer token, it does not expire, and there is no OAuth dance.
      Treat it like a password: it is stored as issued, so anyone holding it
      can spend your calls.</p>
    <pre class="code api-code"><code>curl -H "X-API-Key: YOUR_KEY" \\
  {host}/api/company/JPM</code></pre>
    <p class="plan-note">Lost it? <a href="/dashboard">Sign in</a> and it is on
      the dashboard, or have it emailed to you from the same page.</p>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Endpoints</h2></div>
    <div class="eps">
{_ep("GET", "/api/company/{ticker}", "X-API-Key required · counts as one call",
     "The balance sheet as filed: every line item, its period end, and the "
     "filing it came from. The same numbers the drawing on "
     "<code>/company/{ticker}</code> is built from.")}
{_ep("GET", "/api/demo/{ticker}", "No key · no per-person limit",
     "The same response, ungated, so you can see the shape before deciding "
     "whether to register.")}
{_ep("GET", "/api/user/status", "X-API-Key required · free",
     "Your tier, the calls you have spent this calendar month, and what is "
     "left. Checking never costs a call.")}
{_ep("GET", "/api/download-dataset", f"X-API-Key required · {escape(dataset_price)} one-off",
     "Every fundamental figure this site holds, streamed as CSV. One purchase, "
     "no subscription, re-downloadable whenever you like.")}
{_ep("GET", "/api.json", "Public",
     "This page's machine-readable index — the endpoint list, as JSON.")}
{_ep("GET", "/status", "Public",
     "Row counts and what the loader is doing. How you tell a quiet day from "
     "a stopped pipeline.")}
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
    <p class="sec-sub">JSON on every path, including the errors — a failure
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
          <tr><td>429</td><td>Month spent, or the demo's daily cap.</td></tr>
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
    <pre class="code api-code"><code>curl -sS {host}/api/demo/JPM | head -40</code></pre>
  </section>

  <p class="disclaim">{escape(SHORT_DISCLAIMER)}</p>
{render_footer()}
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>"""
    return shell("To Scale — API reference", body)
