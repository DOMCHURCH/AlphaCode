"""The API dashboard: get a key, read your tier, take the download.

Server-rendered like every other page here, and it deliberately renders the
whole page before knowing who is looking. There is no login: the API key IS the
identity, it lives in this browser's localStorage, and the script fills in the
live figures from `/api/user/status` once it has one. That means the page is
readable -- prices, limits, what the dataset is -- before anybody registers, and
it means no session, no cookie and no password to reset.

Payment instructions are plain text on purpose. Nothing here takes a card. The
buyer sends money out of band and is told, in the same breath, that unlocking is
a human being reading an inbox, not an instant.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell


def _compact(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M".replace(".00M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _contact(admin_email: str) -> str:
    """A real mailto when the address is configured, plain words when it is not.

    An unset ADMIN_EMAIL must never render as `mailto:` with nothing after it --
    that is a dead link on the one screen where the buyer is trying to pay.
    """
    if not admin_email:
        return "the site owner"
    a = escape(admin_email)
    return f'<a class="mail" href="mailto:{a}">{a}</a>'


def render_dashboard(
    *,
    admin_email: str = "",
    dataset_price: int = 29,
    pro_price: int = 49,
    free_limit: int = 10,
    pro_limit: int = 10_000,
    fact_count: int | None = None,
) -> str:
    contact = _contact(admin_email)
    facts = (
        f"{_compact(fact_count)} as-reported facts"
        if fact_count
        else "every as-reported fact in the database"
    )

    body = f"""
<nav><div class="wrap nav">
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
  <span class="spacer"></span>
  <a class="navlink" href="/api">API reference</a>
</div></nav>

<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">API access</h1>
    <p class="hlede">The same filed figures the drawings are made from, as JSON
      — and the whole table as one CSV. Free to try, paid to use in bulk.</p>
  </header>

  <!-- Registration. Replaced by the key panel once a key exists in this
       browser, so the page has exactly one obvious next step at any moment. -->
  <section class="sec" id="get-key">
    <div class="sec-head"><h2>Get a key</h2></div>
    <p class="sec-sub">One address, one key, no confirmation email. The free
      tier is {free_limit} calls a calendar month.</p>
    <form class="search" id="reg-form" autocomplete="on">
      <label class="slabel" for="email">Email address</label>
      <div class="sfield">
        <input id="email" name="email" type="email" inputmode="email"
          placeholder="you@example.com" autocomplete="email" required
          maxlength="254" spellcheck="false" enterkeyhint="go">
        <button type="submit" id="reg-btn">Get API key</button>
      </div>
    </form>
    <p class="formnote" id="reg-note" role="status" aria-live="polite"></p>
    <p class="formnote">Already have one?
      <button type="button" class="linkish" id="paste-key">Paste an existing key</button>
    </p>
  </section>

  <!-- Everything below is hidden until a key is present. -->
  <section class="sec keyed" id="key-panel" hidden>
    <div class="sec-head"><h2>Your key</h2></div>
    <p class="sec-sub">Stored in this browser only. It is never re-issued, so
      keep a copy somewhere you will still have it next month.</p>
    <div class="keyrow">
      <code class="keybox" id="key-value">—</code>
      <button type="button" class="btn" id="copy-key">Copy</button>
    </div>
    <p class="formnote" id="copy-note" role="status" aria-live="polite"></p>
    <p class="formnote">
      <button type="button" class="linkish" id="forget-key">Forget this key on
        this device</button>
    </p>
  </section>

  <section class="sec keyed" id="status-panel" hidden>
    <div class="sec-head"><h2>Status</h2></div>
    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Tier</span>
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
    <!-- A meter you can only read as a fraction is hard to act on, so the bar
         is the same number in a shape the eye reads without arithmetic. -->
    <div class="meter" aria-hidden="true"><i id="s-bar" style="width:0%"></i></div>
    <p class="formnote" id="status-note" role="status" aria-live="polite"></p>
  </section>

  <section class="sec keyed" id="upgrade-panel" hidden>
    <div class="sec-head"><h2>Upgrade to Pro</h2></div>
    <p class="sec-sub">Need more calls? Pro is
      ${pro_price}/month for {_compact(pro_limit)} calls a month.
      Contact {contact} to arrange payment.</p>
  </section>

  <section class="sec keyed" id="download-panel" hidden>
    <div class="sec-head"><h2>Full dataset</h2></div>
    <p class="sec-sub">{facts} — every company, every metric, every filed
      period — as one CSV. One-time ${dataset_price}.</p>
    <p class="sec-sub">These are the resolved figures, not the raw filing dump:
      the duplicate-tag problem (one company reporting Total&nbsp;Assets 23&times;
      in a single filing, once per segment) is already settled to the
      consolidated row.</p>
    <button type="button" class="btn wide" id="dl-btn">Download CSV</button>
    <p class="formnote" id="dl-note" role="status" aria-live="polite"></p>
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Calling it</h2></div>
    <!-- The host and the real key are filled in by the script, so this is
         something to copy rather than something to edit first. -->
    <pre class="code"><code id="curl-example">curl -H "X-API-Key: YOUR_KEY" \\
  https://HOST/api/company/JPM</code></pre>
    <p class="sec-sub">Endpoints: <code>GET /api/company/{{ticker}}</code>,
      <code>GET /api/user/status</code>,
      <code>GET /api/download-dataset</code>. Full list at
      <a href="/api">/api</a>.</p>
  </section>

  <footer>
    Source: <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"
      rel="noopener">SEC Financial Statement Data Sets</a>.
    Questions: {contact}.
  </footer>
</main>

<!-- The paywall. A dialog rather than a redirect: the buyer is mid-task and
     the instructions are four lines, so taking them off the page would lose
     the key they need to quote. -->
<div class="modal" id="pay-modal" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-labelledby="pay-title">
    <h2 id="pay-title">This one is paid</h2>
    <p id="pay-body"></p>
    <p class="paykey">Your API key:<br><code id="pay-key">—</code></p>
    <button type="button" class="btn wide" id="pay-close">Close</button>
  </div>
</div>

<script>
  window.TO_SCALE = {{
    adminEmail: {_js(admin_email)},
    datasetPrice: {int(dataset_price)},
    proPrice: {int(pro_price)}
  }};
</script>
<script src="/static/dashboard.js?v={asset_version()}" defer></script>"""

    return shell("To Scale — API access", body)


def _js(value: str) -> str:
    """A string safe to drop into a <script> literal.

    `json.dumps` would be the obvious tool and is not enough on its own: it
    leaves `</script>` intact, which closes the block early and turns the rest
    of the page into markup. The slash is escaped so it cannot.
    """
    import json

    return json.dumps(value).replace("</", "<\\/")
