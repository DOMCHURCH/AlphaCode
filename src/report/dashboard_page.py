"""The dashboard: four tabs over one account.

Server-rendered like every other page here, and it renders the whole thing
before knowing who is looking. Two ways in, and they are not equivalent:

* **Signed in** (a session cookie from an emailed link) -- the page fetches your
  key and shows it. This is the intended path.
* **A pasted key** -- kept working deliberately. Somebody who only wants to make
  API calls should not have to receive an email to read their own quota, and the
  key was always the real credential anyway.

When both are present the session wins, because it is the stronger claim: a
session proves control of the address, a pasted key proves only that somebody
has the string.

Payment instructions stay plain text. Nothing here takes a card; the buyer sends
money out of band and is told, in the same breath, that unlocking is a person
reading an inbox rather than an instant.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell

# Ticker, and the one word that says why it is worth a look. The same five the
# home page leads with, so the quick links are familiar rather than arbitrary.
_QUICK = (
    ("JPM", "bank"), ("MSFT", "software"), ("WMT", "retail"),
    ("FCX", "miner"), ("AAL", "airline"),
)


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


def _js(value: str) -> str:
    """A string safe to drop into a <script> literal.

    `json.dumps` would be the obvious tool and is not enough on its own: it
    leaves `</script>` intact, which closes the block early and turns the rest
    of the page into markup. The slash is escaped so it cannot.
    """
    import json

    return json.dumps(value).replace("</", "<\\/")


def render_dashboard(
    *,
    admin_email: str = "",
    dataset_price: int = 29,
    pro_price: int = 49,
    free_limit: int = 10,
    pro_limit: int = 10_000,
    fact_count: int | None = None,
    login_enabled: bool = True,
    nav: str = "",
) -> str:
    from src.report.nav import render_footer

    contact = _contact(admin_email)
    facts = (
        f"{_compact(fact_count)} as-reported facts"
        if fact_count
        else "every as-reported fact in the database"
    )
    footer = render_footer(
        'Source: <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"'
        f' rel="noopener">SEC Financial Statement Data Sets</a>. Questions: {contact}.'
    )
    quick = "".join(
        f'<a href="/company/{t}">{t} <small>{escape(k)}</small></a>'
        for t, k in _QUICK
    )

    body = f"""
{nav}

<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">API access</h1>
    <p class="hlede">The same filed figures the drawings are made from, as JSON
      — and the whole table as one CSV. Free to try, paid to use in bulk.</p>
  </header>

  <!-- Shown when there is neither a session nor a stored key. -->
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
    <p class="accept">
      <label>
        <input type="checkbox" id="accept-terms">
        I agree to the <a href="/terms">Terms of Service</a> and
        <a href="/privacy">Privacy Policy</a>
      </label>
    </p>
    <p class="formnote" id="reg-note" role="status" aria-live="polite"></p>
    <!-- Shown only after a 409, i.e. only for an address that IS registered.
         A login link rather than a key resend: it lands the person on a working
         dashboard instead of leaving them to paste a string back in. -->
    <div class="resend" id="resend-box" hidden>
      <p>That address already has a key. A sign-in link takes you straight to it.</p>
      <button type="button" class="btn" id="resend-btn">Email me a login link</button>
      <p class="formnote" id="resend-note" role="status" aria-live="polite"></p>
    </div>
    <p class="formnote">Already have a key?
      <button type="button" class="linkish" id="paste-key">Paste it instead</button>
    </p>
  </section>

  <!-- Filled and revealed by the script when the plan changed since this
       browser last looked. A grant happens out of band -- somebody pays, the
       operator runs a curl -- so without this the only sign it worked is a
       number quietly reading differently. -->
  <div class="banner keyed" id="grant-banner" hidden role="status">
    <span class="banner-tick" aria-hidden="true">&#10003;</span>
    <span id="banner-text"></span>
    <button type="button" class="linkish" id="banner-close">Dismiss</button>
  </div>

  <!-- Everything below appears once there is a session or a key. -->
  <div class="tabs keyed" id="tabs" hidden role="tablist" aria-label="Dashboard">
    <button type="button" class="tab" role="tab" data-tab="search">Search</button>
    <button type="button" class="tab" role="tab" data-tab="api">API</button>
    <button type="button" class="tab" role="tab" data-tab="account">Account</button>
    <button type="button" class="tab" role="tab" data-tab="billing">Billing</button>
  </div>

  <section class="sec keyed panel" id="panel-search" data-panel="search" hidden>
    <div class="sec-head"><h2>Draw a balance sheet</h2></div>
    <p class="sec-sub">The same search as the front page — it opens the full
      drawing on that company's own page.</p>
    <form class="search" action="/search" method="get" role="search">
      <label class="slabel" for="dash-q">Ticker or company name</label>
      <div class="sfield">
        <input id="dash-q" name="q" type="text" placeholder="JPM or Walmart"
          autocomplete="off" autocapitalize="none" spellcheck="false"
          maxlength="64" enterkeyhint="go">
        <button type="submit">Draw it</button>
      </div>
    </form>
    <p class="tryline">Or start with one of these</p>
    <div class="sugg">{quick}</div>
  </section>

  <section class="sec keyed panel" id="panel-api" data-panel="api" hidden>
    <div class="sec-head"><h2>Your key</h2></div>
    <p class="sec-sub">Send it as an <code>X-API-Key</code> header. Kept in this
      browser; signing in fetches it for you instead.</p>
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

    <pre class="code"><code id="curl-example">curl -H "X-API-Key: YOUR_KEY" \\
  https://HOST/api/company/JPM</code></pre>

    <!-- Three steps, shown until the first call lands. A key with no worked
         example is a string somebody has to go and read documentation about. -->
    <ol class="quickstart" id="quickstart">
      <li>Copy your key above.</li>
      <li>Run it:
        <pre class="code"><code id="qs-curl">curl -H "X-API-Key: YOUR_KEY"   https://HOST/api/company/JPM</code></pre>
      </li>
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
  </section>

  <section class="sec keyed panel" id="panel-account" data-panel="account" hidden>
    <div class="sec-head"><h2>Account</h2></div>
    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Email</span>
        <b class="tval sm" id="a-email">—</b>
        <span class="tsub">the address this key belongs to</span>
      </div>
      <div class="tile">
        <span class="tlabel">Plan</span>
        <b class="tval sm" id="a-plan">—</b>
        <span class="tsub" id="a-plan-sub"></span>
      </div>
    </div>
    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Password</span>
        <b class="tval sm" id="a-pw">—</b>
        <span class="tsub" id="a-pw-sub"></span>
      </div>
      <div class="tile">
        <span class="tlabel">Pro expires</span>
        <b class="tval sm" id="a-expires">—</b>
        <span class="tsub" id="a-expires-sub"></span>
      </div>
    </div>

    <!-- Session-gated, both of them. Setting a first password needs no old one
         because the session already proves the address; changing an existing
         one does, because a session left open on a shared machine is exactly
         how somebody else would lock the owner out. -->
    <div class="resend" id="pw-box" hidden>
      <p id="pw-box-title">Set a password</p>
      <form id="setpw-form">
        <div class="sfield" id="curpw-field" hidden>
          <input id="curpw" type="password" placeholder="Current password"
            autocomplete="current-password" maxlength="128">
        </div>
        <div class="sfield">
          <input id="newpw" type="password" placeholder="New password (8+ characters)"
            autocomplete="new-password" maxlength="128" required>
          <button type="submit" class="btn" id="setpw-btn">Save</button>
        </div>
      </form>
      <p class="formnote" id="setpw-note" role="status" aria-live="polite"></p>
    </div>
    <p class="formnote" id="pw-hint" hidden>
      <a href="/login">Sign in</a> to set a password for this account.
    </p>

    <p class="plan-note">Changing the address is not self-serve yet — email
      {contact} and it will be moved by hand.</p>
    <p class="formnote">
      <button type="button" class="linkish" id="forget-key">Forget this key on
        this device</button>
    </p>
  </section>

  <section class="sec keyed panel" id="panel-billing" data-panel="billing" hidden>
    <div class="sec-head"><h2>Billing</h2></div>
    <div class="tiles">
      <div class="tile">
        <span class="tlabel">Current plan</span>
        <b class="tval" id="b-plan">—</b>
        <span class="tsub" id="b-plan-sub"></span>
      </div>
      <div class="tile">
        <span class="tlabel">Full dataset</span>
        <b class="tval" id="b-dataset">—</b>
        <span class="tsub" id="b-dataset-sub"></span>
      </div>
    </div>

    <div class="tablewrap">
      <table class="compare">
        <caption class="vh">What each plan includes</caption>
        <thead>
          <tr><th scope="col">Feature</th><th scope="col">Free</th>
              <th scope="col">Pro</th></tr>
        </thead>
        <tbody>
          <tr><th scope="row">API calls / month</th>
              <td>{free_limit}</td><td>{_compact(pro_limit)}</td></tr>
          <tr><th scope="row">Full dataset export</th>
              <td><span class="no" aria-label="no">&#10007;</span></td>
              <td>${dataset_price} once</td></tr>
          <tr><th scope="row">Drawings &amp; search</th>
              <td>Unlimited</td><td>Unlimited</td></tr>
          <tr><th scope="row">Support</th><td>Email</td><td>Priority email</td></tr>
          <tr><th scope="row">Price</th><td>$0</td><td>${pro_price}/month</td></tr>
        </tbody>
      </table>
    </div>

    <div class="buyrow">
      <div class="buy">
        <span class="plan-name">Pro</span>
        <p class="plan-price">${pro_price}<small>/month</small></p>
        <p class="plan-line">{_compact(pro_limit)} API calls a month.</p>
        <button type="button" class="btn" id="buy-pro">Upgrade to Pro</button>
      </div>
      <div class="buy">
        <span class="plan-name">Full dataset</span>
        <p class="plan-price">${dataset_price}<small> once</small></p>
        <p class="plan-line">{facts}, as one CSV.</p>
        <button type="button" class="btn" id="buy-data">Buy full dataset</button>
      </div>
    </div>
    <p class="plan-note">No card is taken on this site. Payment is by e-transfer
      or PayPal to {contact}, and access is unlocked by hand — usually within
      24 hours. Payment history will appear here once there is any.</p>
  </section>

{footer}
</main>

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
    proPrice: {int(pro_price)},
    loginEnabled: {"true" if login_enabled else "false"}
  }};
</script>
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/dashboard.js?v={asset_version()}" defer></script>"""

    return shell("To Scale — API access", body, film="calm")
