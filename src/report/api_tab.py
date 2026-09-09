"""The dashboard's API tab: the key, the quota, and how to actually use it.

Split out of `dashboard_page` because it grew past the point where it could be
read inside a four-hundred-line f-string, and because what it needs to do now
is not "show a key" but *teach the API in the place the key is*. A key with no
worked example is a string somebody has to go and read documentation about,
and the documentation is on another page.

Two examples, curl and Python, because those are the two things somebody
actually reaches for and they fail differently: curl is how you find out the
key works, Python is how you find out what the response looks like once it
does.

Both carry `HOST` and `YOUR_KEY` as literal tokens. `dashboard.js` swaps them
for the real host and the reader's own key once it has them, which is the whole
point — a copied example that runs unmodified. They are TOKENS rather than
values baked in server-side on purpose: this page is rendered before anyone is
identified, so a key printed here would be printed for whoever loaded the page,
and a host written into the HTML would be the app's internal origin rather than
the one the reader's browser is using. A preview deployment must never print
production's host into an example.
"""

from __future__ import annotations

# The example ticker. AAPL rather than JPM: the request in the docs and the
# request in a first experiment should be the same one, and Apple is the
# company somebody types when they are testing whether a financial API works.
TICKER = "AAPL"


def render_api_tab() -> str:
    """The panel. No arguments: everything variable in it is filled by script.

    The quota tiles, the key box and both examples are placeholders at render
    time and are populated from `/api/user/status`. That is not laziness — this
    page is served identically to a signed-in reader, a reader with a pasted
    key and a stranger, so anything account-specific baked into the HTML would
    either be wrong or be a leak.
    """
    return f"""
  <section class="sec keyed panel" id="panel-api" data-panel="api" hidden>
    <div class="sec-head"><h2>Your key</h2></div>
    <p class="sec-sub">Send it as an <code>X-API-Key</code> header on an
      ordinary GET. No SDK, no OAuth, no token exchange. Kept in this browser;
      signing in fetches it for you instead.</p>
    <div class="keyrow">
      <code class="keybox" id="key-value">—</code>
      <button type="button" class="btn" id="copy-key">Copy</button>
    </div>
    <p class="formnote" id="copy-note" role="status" aria-live="polite"></p>

    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Plan</span>
        <b class="tval" id="s-tier">—</b>
        <span class="tsub" id="s-tier-sub"></span>
      </div>
      <div class="tile">
        <span class="tlabel">Calls this month</span>
        <b class="tval" id="s-calls">—</b>
        <span class="tsub" id="s-calls-sub"></span>
      </div>
      <div class="tile">
        <span class="tlabel">Dataset download</span>
        <b class="tval" id="s-dl">—</b>
        <span class="tsub" id="s-dl-sub"></span>
      </div>
    </div>
    <div class="meter" aria-hidden="true"><i id="s-bar" style="width:0%"></i></div>
    <p class="formnote" id="status-note" role="status" aria-live="polite"></p>

    <!-- The API is LIVE data, said here rather than only on the pricing page:
         this is the tab somebody is on when they are deciding whether they
         still need the CSV they were about to buy. -->
    <div class="banner live" id="api-live" role="note">
      <span class="banner-tick" aria-hidden="true">&#8635;</span>
      <span>Every call answers from the live database — including filings that
        landed this morning. The <a href="/dataset">full dataset</a> is the same
        data frozen at one moment; this is not.</span>
    </div>

    <!-- ---- how to use the key ---- -->
    <div class="sec-head sub"><h3>Using your key</h3></div>
    <p class="sec-sub">Both examples below are filled in with your real key and
      this deployment's host. Copy either one and it runs as-is.</p>

    <p class="exlabel">curl</p>
    <pre class="code"><code id="curl-example">curl -H "X-API-Key: YOUR_KEY" \\
  https://HOST/api/company/{TICKER}</code></pre>

    <p class="exlabel">Python</p>
    <pre class="code"><code id="py-example">import requests

headers = {{"X-API-Key": "YOUR_KEY"}}
response = requests.get("https://HOST/api/company/{TICKER}", headers=headers)
data = response.json()
print(data["assets"])</code></pre>
    <p class="formnote" id="ex-note" role="status" aria-live="polite"></p>

    <p class="plan-note">Keep the key server-side. Anyone holding the string can
      spend your monthly allowance, and a key in front-end JavaScript is a
      published key. A 401 means the header is missing or wrong; a 429 means the
      month's allowance is spent, and the response says when it resets.
      Full reference: <a href="/api">the API page</a>.</p>

    <!-- Three steps, shown until the first call lands. -->
    <ol class="quickstart" id="quickstart">
      <li>Copy your key above.</li>
      <li>Run one of the examples.</li>
      <li>Watch the count move on this tab.</li>
    </ol>

    <!-- The plan, restated where the key is, because this is the tab somebody
         is on when they discover the allowance is the thing stopping them. -->
    <div class="planline">
      <span id="plan-summary">—</span>
      <a class="linkish" href="#" data-goto="billing" id="to-billing">Upgrade to Pro</a>
      <a class="linkish" href="#" data-goto="billing" id="to-billing-data">Buy full dataset</a>
    </div>

    <button type="button" class="btn wide" id="dl-btn">Download full dataset</button>
    <p class="formnote" id="dl-note" role="status" aria-live="polite"></p>
    <!-- Its own element, not `#dl-note`: the script writes progress and errors
         into that one with textContent, which would eat this link the first
         time the button was pressed. -->
    <p class="plan-note">A one-time static snapshot, fixed at the moment it is
      generated — <a href="/dataset">what is in it</a>.</p>

    <!-- Regeneration is session-only. The reason to press it is that the key
         leaked, and a leaked key able to rotate itself locks out its owner. -->
    <div class="resend" id="regen-box" hidden>
      <p>Key compromised? Replacing it takes effect immediately — anything still
        using the old one stops working.</p>
      <button type="button" class="btn" id="regen-btn">Regenerate key</button>
      <p class="formnote" id="regen-note" role="status" aria-live="polite"></p>
    </div>
    <p class="formnote" id="regen-hint" hidden>
      <a href="/login">Sign in</a> to regenerate this key.
    </p>
  </section>"""
