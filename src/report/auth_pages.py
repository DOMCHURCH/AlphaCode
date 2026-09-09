"""The two pages either side of an emailed link: /login and /auth/verify.

Rendered in Python like every other page here rather than from a template file
-- this codebase has exactly one HTML file on disk (the admin console) and its
reader-facing pages are all functions. A template engine for two forms would be
a dependency and a second way of doing things.

The verify page is the interesting one. It receives the token in a URL that mail
scanners and link prefetchers will fetch before the recipient ever clicks, so it
must not spend the token merely by being looked at. It looks, reports, and only
POSTs -- which a prefetcher does not do.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell


def _footer() -> str:
    from src.report.nav import render_footer

    return render_footer()


def _nav(active: str = "login") -> str:
    """The shared bar. Nobody reaching these pages has a session -- /login is
    where you go without one, and /auth/verify runs before the cookie is set."""
    from src.report.nav import render_nav

    return render_nav(active=active, signed_in=False)


def render_login(
    *, admin_email: str = "", enabled: bool = True, nav: str = ""
) -> str:
    """Ask for an address, promise nothing about whether it is registered."""
    if not enabled:
        where = escape(admin_email) if admin_email else "the site owner"
        body = f"""{nav or _nav()}
<main class="wrap" id="main">
  <header class="hero glass">
    <h1 class="htitle">Sign in</h1>
    <p class="hlede">Login is not configured on this deployment. Your API key
      still works on every /api route — contact {where} if you have lost it.</p>
  </header>
{_footer()}
</main>"""
        return shell("To Scale — sign in", body)

    body = f"""{nav or _nav()}
<main class="wrap" id="main">
  <header class="hero glass">
    <h1 class="htitle">Sign in</h1>
    <p class="hlede">A one-time link, or a password if you have set one.
      The link needs nothing set up in advance: it signs you in and, if you are
      new, makes the account.</p>
  </header>

  <!-- Two ways in, both landing on the same session. The link tab is first
       because it needs nothing set up; the password tab exists because going
       to another application and back is a real cost every single time. -->
  <!-- One checkbox above both panes: it is the same consent whichever way you
       come in, and two of them would be two things to keep in step. Only read
       when an account is actually created -- signing in does not re-ask. -->
  <p class="accept">
    <label>
      <input type="checkbox" id="accept-terms">
      I agree to the <a href="/terms">Terms of Service</a> and
      <a href="/privacy">Privacy Policy</a>
    </label>
  </p>

  <div class="tabs" id="login-tabs" role="tablist" aria-label="Sign in">
    <button type="button" class="tab on" role="tab" data-logintab="link"
      aria-selected="true">Email me a link</button>
    <button type="button" class="tab" role="tab" data-logintab="password"
      aria-selected="false">Use a password</button>
  </div>

  <section class="sec" id="pane-link">
    <form class="search" id="login-form">
      <label class="slabel" for="login-email">Email address</label>
      <div class="sfield">
        <input id="login-email" name="email" type="email" inputmode="email"
          placeholder="you@example.com" autocomplete="email" required
          maxlength="254" spellcheck="false" enterkeyhint="go" autofocus>
        <button type="submit" id="login-btn">Send magic link</button>
      </div>
    </form>
    <p class="formnote" id="login-note" role="status" aria-live="polite"></p>
    <p class="plan-note">The link is good for 15 minutes and one use. If you
      already have an API key and only want to make calls, you do not need to
      sign in at all — <a href="/dashboard">paste it on the dashboard</a>.</p>
  </section>

  <section class="sec" id="pane-password" hidden>
    <form id="pw-form" autocomplete="on">
      <label class="slabel" for="pw-email">Email address</label>
      <div class="sfield">
        <input id="pw-email" name="email" type="email" inputmode="email"
          placeholder="you@example.com" autocomplete="email" required
          maxlength="254" spellcheck="false">
      </div>
      <label class="slabel" for="pw-pass">Password</label>
      <div class="sfield">
        <input id="pw-pass" name="password" type="password"
          placeholder="Your password" autocomplete="current-password" required
          maxlength="128" enterkeyhint="go">
        <button type="submit" id="pw-btn">Sign in</button>
      </div>
    </form>
    <p class="formnote" id="pw-note" role="status" aria-live="polite"></p>
    <p class="plan-note">
      <button type="button" class="linkish" id="forgot-btn">Forgot your
        password?</button> — a sign-in link goes to your inbox, and you can set
      a new one from the Account tab. <strong>Signed up with a link and never
      set a password?</strong> Same answer: use the link tab, then set one
      from Account.
      No account yet?
      <button type="button" class="linkish" id="pw-signup">Create one with a
        password</button>.
    </p>
  </section>
  </section>
{_footer()}
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/auth.js?v={asset_version()}" defer></script>"""
    return shell("To Scale — sign in", body)


def render_verify(*, token: str, state: str) -> str:
    """The page the emailed link lands on.

    `state` is "ready" when the token is currently good and "dead" when it is
    not, decided by LOOKING at the token rather than spending it. The page then
    spends it with a POST, so a scanner that fetched this URL on the way to the
    inbox has not used up the recipient's one click.
    """
    if state != "ready":
        body = f"""{_nav()}
<main class="wrap" id="main">
  <section class="sec">
    <div class="empty">
      <h1>That link has expired</h1>
      <p>Login links last 15 minutes and work once. This one has been used
        already, or it is older than that.</p>
      <div class="sugg"><a href="/login">Send a new one</a></div>
    </div>
  </section>
{_footer()}
</main>"""
        return shell("To Scale — link expired", body)

    body = f"""{_nav()}
<main class="wrap" id="main">
  <section class="sec">
    <div class="empty" id="verify-box">
      <h1 id="verify-title">Signing you in…</h1>
      <p id="verify-msg">One moment.</p>
      <!-- The fallback for a browser with no JS, and the reason the token is
           not spent by a GET: this is a POST, which link prefetchers do not
           issue. -->
      <form method="post" action="/auth/verify" id="verify-form">
        <input type="hidden" name="token" value="{escape(token)}">
        <button type="submit" class="btn" id="verify-btn">Continue to dashboard</button>
      </form>
    </div>
  </section>
{_footer()}
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/auth.js?v={asset_version()}" defer></script>"""
    return shell("To Scale — signing you in", body)
