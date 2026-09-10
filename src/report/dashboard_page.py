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

The billing tab takes a card. Its three buy buttons open a Stripe Checkout
Session and the browser leaves for Stripe; fulfilment happens on the webhook,
never on the return URL. The plain-text "email the owner" path survives only as
the fallback for a deployment with no Stripe configuration at all.

The API tab lives in `api_tab.py`. It was extracted when it stopped being "show
a key" and became "teach the API where the key is", which is more markup than
belongs inside another module's f-string.
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
    dataset_price: str = "$79.99",
    pro_price: str = "$49",
    pro_annual_price: str = "$490",
    annual_saving: str = "$98",
    free_limit: int = 10,
    pro_limit: int = 10_000,
    fact_count: int | None = None,
    dataset_as_of: str = "",
    login_enabled: bool = True,
    nav: str = "",
) -> str:
    """Prices arrive as RENDERED STRINGS ("$79.99"), not as numbers.

    They used to be ints, which is how a $79.99 dataset renders as "$79" next
    to a Stripe page that charges 79.99 -- the one disagreement on this site a
    buyer reads as a bait and switch. Formatting happens once, in
    `settings.price_label`, and every page is handed the result.
    """
    from src.report.api_tab import render_api_tab
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
    # " as of 8 September 2026" -- the STATIC-ness of the dataset is the
    # thing the buy button cannot say on its own, and the date is what makes it
    # concrete rather than a disclaimer.
    as_of_clause = f", as of {escape(dataset_as_of)}" if dataset_as_of else ""
    api_tab = render_api_tab()
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

{api_tab}

  <section class="sec keyed panel" id="panel-account" data-panel="account" hidden>
    <div class="sec-head"><h2>Account</h2></div>

    <!-- Shown only to an account with no password, which is every account that
         arrived by magic link. Those people cannot use the password tab on
         /login and there is nothing on that page that can tell them so without
         confirming to a stranger that their address is registered -- so the
         place it CAN be said plainly is here, behind the session. Ships hidden
         and revealed by dashboard.js, so it is never briefly wrong. -->
    <div class="banner nopw" id="nopw-callout" hidden>
      <p><strong>You have no password yet.</strong> You signed in with a
        link, which will keep working. Set one below if you would rather not
        wait for an email every time.</p>
      <button type="button" class="btn" id="nopw-btn">Set a password</button>
    </div>

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

    <!-- The comparison that decides the purchase, restated where the buttons
         are. A reader on this tab has already decided to spend money and is
         now choosing WHICH -- and the two products differ in the one dimension
         a price list cannot show: whether the data keeps arriving. -->
    <div class="tablewrap">
      <table class="compare compare-3">
        <caption class="vh">The dataset and the API, compared</caption>
        <thead>
          <tr><th scope="col">Feature</th>
              <th scope="col">Dataset <small>({dataset_price} once)</small></th>
              <th scope="col">Pro API <small>({pro_price}/month)</small></th></tr>
        </thead>
        <tbody>
          <tr><th scope="row">Data freshness</th>
              <td>Static (as of download date)</td>
              <td>Live (updates daily)</td></tr>
          <tr><th scope="row">Access method</th>
              <td>One CSV download</td>
              <td>API calls ({_compact(pro_limit)}/month)</td></tr>
          <tr><th scope="row">Use case</th>
              <td>One-time analysis</td>
              <td>Ongoing automation</td></tr>
          <tr><th scope="row">Updates</th>
              <td><span class="no" aria-label="no">&#10007;</span> No (buy again)</td>
              <td><span class="yes" aria-label="yes">&#10003;</span> Yes (monthly)</td></tr>
          <tr><th scope="row">Automation</th>
              <td><span class="no" aria-label="no">&#10007;</span> Manual</td>
              <td><span class="yes" aria-label="yes">&#10003;</span> Programmatic</td></tr>
          <tr><th scope="row">Drawings &amp; search on this site</th>
              <td><span class="yes" aria-label="yes">&#10003;</span> Free, unlimited</td>
              <td><span class="yes" aria-label="yes">&#10003;</span> Free, unlimited</td></tr>
        </tbody>
      </table>
    </div>
    <p class="plan-note">Drawings and search are free and unlimited on every
      tier, with or without a key. Free is {free_limit} API calls a month on
      live data, which is the tier this account starts on.
      <a href="/pricing">The full comparison and the FAQ</a> are on the pricing
      page.</p>

    <div class="buyrow buyrow-3">
      <div class="buy">
        <span class="plan-name">Pro</span>
        <p class="plan-price">{pro_price}<small>/month</small></p>
        <p class="plan-line">Live data, {_compact(pro_limit)} API calls a month.
          Query any company at any time; updates daily.</p>
        <button type="button" class="btn" id="buy-pro">Upgrade to Pro</button>
      </div>
      <div class="buy">
        <span class="plan-name">Pro annual<span class="badge">Save {annual_saving}</span></span>
        <p class="plan-price">{pro_annual_price}<small>/year</small></p>
        <p class="plan-line">The same Pro access, paid yearly — two months free
          against the monthly price.</p>
        <button type="button" class="btn" id="buy-pro-annual">Go Pro annually</button>
      </div>
      <div class="buy">
        <span class="plan-name">Full dataset</span>
        <p class="plan-price">{dataset_price}<small> once</small></p>
        <p class="plan-line">{facts}, as one CSV{as_of_clause}. A snapshot — it
          does not update. <a href="/dataset">What is in it</a>.</p>
        <button type="button" class="btn" id="buy-data">Buy full dataset</button>
      </div>
    </div>
    <p class="plan-note">Paid plans go through Stripe. Your card details are
      entered on Stripe's page and never reach this site. Payment history will
      appear here once there is any.</p>

    <!-- Shown only to accounts Stripe actually knows about. The script unhides
         it after /api/user/status says there is a customer, because a "Manage
         subscription" button that answers 409 for everybody who has never paid
         is a button that teaches people not to trust the page. -->
    <div class="manage" id="manage-billing" hidden>
      <span class="plan-name">Manage your subscription</span>
      <p class="plan-line">Change your card, switch plans, download invoices or
        cancel. All of it happens on Stripe's own page, and anything you change
        there is reflected here within a minute.</p>
      <button type="button" class="btn ghost" id="open-portal">Manage subscription</button>
      <p class="note" id="portal-note" role="status" aria-live="polite"></p>
    </div>
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
  /* Only what the SCRIPT reads. The Pro prices used to be here and were
     removed with the line that printed them: with two Pro plans the status
     payload cannot say which one an account is on, so any price the script
     wrote next to "Pro" was a guess -- and "$49/month" shown to somebody who
     paid $490 for a year is worse than saying nothing. The prices are still on
     the page, rendered by the server onto the cards that charge them. */
  window.TO_SCALE = {{
    adminEmail: {_js(admin_email)},
    datasetPrice: {_js(dataset_price)},
    loginEnabled: {"true" if login_enabled else "false"}
  }};
</script>
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/dashboard.js?v={asset_version()}" defer></script>"""

    return shell("To Scale — API access", body)
