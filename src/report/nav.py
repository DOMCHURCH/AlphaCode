"""The navigation bar, in one place.

It was copied into four page modules, which is three copies too many for
something every page shows and every page must agree about. This is the one
definition; the pages pass what differs.

Two things it has to get right that a nav usually does not:

* **It renders what the SERVER knows about the session.** Not a placeholder that
  JavaScript later corrects -- a bar that says "Sign in" for half a second to
  somebody who is signed in is worse than no bar, and it would be wrong
  permanently for a reader with scripting off.
* **Collapsing is an enhancement, never a requirement.** The toggle button is
  rendered hidden and only revealed by script. With no JavaScript there is no
  button and the links simply wrap -- rather than the usual arrangement, where
  the menu is collapsed by CSS and unreachable forever if the script fails.
"""

from __future__ import annotations

from html import escape

# (label, href, key). `key` matches the `active` argument.
_LINKS = (
    ("About", "/about", "about"),
    ("How we verify", "/methodology", "methodology"),
    ("Pricing", "/pricing", "pricing"),
    ("Research", "/blog", "blog"),
    ("API", "/api", "api"),
)


def render_nav(
    *,
    active: str = "",
    signed_in: bool = False,
    email: str = "",
    tier: str = "free",
    login_enabled: bool = True,
) -> str:
    """One bar for every public page.

    `active` is one of "home", "pricing", "api", "dashboard", "login" -- the
    page marks itself, rather than the bar guessing from a URL it cannot see.
    """
    links = "".join(
        f'<a class="navlink{" on" if active == key else ""}" href="{href}">'
        f"{escape(label)}</a>"
        for label, href, key in _LINKS
    )

    if signed_in:
        dash = (
            f'<a class="navlink{" on" if active == "dashboard" else ""}"'
            ' href="/dashboard">Dashboard</a>'
        )
    else:
        # A disabled anchor is not a thing: <a> without href is not focusable
        # and role=link with aria-disabled is announced as a link that lies. A
        # button carrying aria-disabled is the honest shape -- reachable by
        # keyboard, announced as unavailable, and able to say why when pressed.
        # The reason is carried in the markup, not behind an interaction:
        # `title` is a desktop hover and nothing at all on a phone, and a
        # control marked aria-disabled should not need to be operated to find
        # out why it is disabled. The visually-hidden span means a screen
        # reader hears "Dashboard, log in to access your dashboard, dimmed"
        # without clicking anything. The click handler in nav.js is a bonus for
        # sighted touch users, not the only way to the explanation.
        dash = (
            '<button type="button" class="navlink off" id="nav-dash-off"'
            ' aria-disabled="true"'
            ' title="Log in to access your dashboard">Dashboard'
            '<span class="vh">, log in to access your dashboard</span>'
            "</button>"
        )

    if signed_in:
        # IDs kept as dashboard.js expects them: it also fills this in for a
        # visitor working from a pasted key, where there is no session and the
        # server therefore cannot know who they are.
        right = f"""<span class="whoami" id="whoami">
      <span class="who-email" id="who-email">{escape(email)}</span>
      <span class="badge" id="who-plan">{"Pro" if tier == "pro" else "Free"}</span>
      <form class="logout" method="post" action="/logout">
        <button type="submit" class="linkish" id="logout-btn">Log out</button>
      </form>
    </span>
    <span id="signed-out" hidden></span>"""
    else:
        sign_in = (
            f'<a class="navlink{" on" if active == "login" else ""}"'
            ' href="/login">Sign in</a>'
            if login_enabled
            else ""
        )
        right = f"""<span class="whoami" id="whoami" hidden>
      <span class="who-email" id="who-email"></span>
      <span class="badge" id="who-plan"></span>
      <form class="logout" method="post" action="/logout">
        <button type="submit" class="linkish" id="logout-btn">Log out</button>
      </form>
    </span>
    <span id="signed-out">{sign_in}</span>"""

    home_on = " on" if active == "home" else ""
    return f"""
<nav><div class="wrap nav">
  <a class="brand{home_on}" href="/"><span class="dot"></span>BalanceProof</a>
  <span class="spacer"></span>
  <!-- Revealed by nav.js. Rendered hidden so that with no script there is no
       button and the links below stay visible, rather than a collapsed menu
       nothing can open. -->
  <button type="button" class="navtoggle" id="nav-toggle" hidden
    aria-expanded="false" aria-controls="nav-links">
    <span class="bars" aria-hidden="true"></span>
    <span class="vh">Menu</span>
  </button>
  <div class="navlinks" id="nav-links">
    {links}
    {dash}
    {right}
  </div>
</div>
<p class="navnote" id="nav-note" role="status" aria-live="polite" hidden></p>
</nav>
{render_signin_prompt(active=active, signed_in=signed_in, login_enabled=login_enabled)}"""


def render_signin_prompt(
    *, active: str = "", signed_in: bool = False, login_enabled: bool = True
) -> str:
    """The offer to sign in, rendered hidden on every page the nav appears on.

    HERE, rather than in each page module, for the reason the nav itself is
    here: seventeen page modules already load `nav.js`, and a panel added to
    each of them is sixteen chances to add it to fifteen. It is
    `position: fixed`, so where it sits in the document does not matter.

    THE MARKUP IS THE SAME FOR EVERY READER. Whether it is shown is decided in
    the browser by `nav.js` from `/api/signin-prompt`, because these pages come
    out of a process-wide cache -- a panel rendered visible for one visitor
    would be served visible to the next. What varies per reader is visibility,
    never HTML.

    Three pages do not get it, and each for its own reason: `/login` and
    `/auth/verify` (`active` is "login") because offering a sign-in to
    somebody already signing in is absurd, `/dashboard` because reaching it
    means being signed in already, and any deployment with login switched off,
    where the panel would advertise a door that is not there.
    """
    if signed_in or not login_enabled or active in {"login", "dashboard"}:
        return ""
    from src.config.settings import get_settings, price_label

    s = get_settings()
    calls = f"{s.free_tier_monthly_calls:,}"
    pro = escape(price_label(s.pro_price_usd))
    half = escape(price_label(
        round(s.pro_price_usd * (100 - s.signin_offer_percent) / 100, 2)
    ))
    # Two views in one sheet. `sp-ask` is the offer of a free account;
    # `sp-deal` is shown once, in its place, when a reader says no -- and only
    # if `/api/signin-prompt` reports an offer, since the code and its label
    # are read at runtime (these pages are cached, a code rendered into them
    # would outlive a change to it). The mini drawing is the site's own mark:
    # owns on the left, owed and owned on the right, in the data colours.
    return f"""
<div class="sp-wrap" id="signin-prompt" hidden>
  <div class="sp-sheet" role="dialog" aria-modal="true"
    aria-labelledby="sp-title" aria-describedby="sp-lede">
    <button type="button" class="sp-x" id="sp-close" aria-label="Close">
      <span aria-hidden="true">&times;</span>
    </button>

    <div class="sp-view" id="sp-ask">
      <div class="sp-mark" aria-hidden="true">
        <i class="sp-a"></i><i class="sp-l"></i><i class="sp-e"></i>
      </div>
      <h2 id="sp-title">Take these numbers with you</h2>
      <p id="sp-lede" class="sp-lede">A free account turns every balance sheet
        on this site into data you can use.</p>
      <ul class="sp-perks">
        <li><b>{calls} API calls a month</b>, free, for all 6,000+ companies</li>
        <li><b>Every figure as JSON</b>, already checked against A&nbsp;=&nbsp;L&nbsp;+&nbsp;E</li>
        <li><b>Works with Claude</b> and other AI tools through MCP</li>
      </ul>
      <form class="search sp-form" id="sp-form" novalidate>
        <label class="vh" for="sp-email">Email address</label>
        <div class="sfield">
          <input id="sp-email" name="email" type="email" required
            placeholder="you@company.com" autocomplete="email"
            spellcheck="false" enterkeyhint="send">
          <button type="submit" id="sp-send">Get my free key</button>
        </div>
      </form>
      <p class="sp-note" id="sp-note" role="status" aria-live="polite" hidden></p>
      <p class="sp-fine">No card and no password: we email you a sign-in link.
        Reading the site stays free either way.</p>
      <div class="sp-foot">
        <span>Have an account? <a href="/login">Sign in</a></span>
        <button type="button" class="sp-no" id="sp-no">Not now</button>
      </div>
    </div>

    <div class="sp-view" id="sp-deal" hidden>
      <p class="sp-eyebrow">Before you go</p>
      <h2 id="sp-deal-title">{escape(s.signin_offer_label)}</h2>
      <p class="sp-lede">Pro is 10,000 calls a month for building on the
        data: dashboards, screens, anything that has to stay current.</p>
      <p class="sp-price"><s>{pro}</s> <b>{half}</b> <span>first month,
        then {pro}/month. Cancel any time, 14-day refund.</span></p>
      <a class="btn sp-claim" id="sp-claim"
        href="/login?next=%2Fdashboard%3Fplan%3Dpro%23billing">Claim the
        discount</a>
      <p class="sp-fine">This offer is only on this page and is shown once.
        Leave and it is gone. Claim it and it is applied for you at checkout
        for the next hour.</p>
      <div class="sp-foot"><span></span>
        <button type="button" class="sp-no" id="sp-no-deal">No thanks</button>
      </div>
    </div>
  </div>
</div>"""


# The repository the site is built from. A link rather than a claim: "open
# source" is checkable, and this is where it is checked.
SOURCE_URL = "https://github.com/domchurch/alphacode"

# The address a customer writes to. Spelled once, here, so the footer, the
# pricing page and the revoked-key 401 cannot drift apart -- an address that
# appears differently in three places is one nobody trusts.
SUPPORT_EMAIL = "support@balanceproof.dev"


def render_footer(extra: str = "") -> str:
    """The same footer on every page, legal links included.

    One definition for the same reason the nav has one: these links have to be
    reachable from everywhere, and four hand-written copies is four places for
    one of them to quietly go missing.

    `extra` is whatever that page already said in its footer, kept rather than
    replaced -- the source attribution on the reader-facing pages is part of the
    argument the site is making, not decoration.
    """
    from src.report.legal import SHORT_DISCLAIMER

    lead = f"{extra} " if extra else ""
    return f"""
  <footer>
    {lead}<span class="foot-links">
      <a href="/terms">Terms</a>
      <a href="/privacy">Privacy</a>
      <a href="{SOURCE_URL}" rel="noopener">Source</a>
      <a href="mailto:{SUPPORT_EMAIL}">Support</a>
    </span>
    <span class="foot-disc">{SHORT_DISCLAIMER}</span>
  </footer>"""


def render_disclaimer() -> str:
    """The one-line warning above the drawings.

    Stated where the numbers are, not only in a document nobody opens: this is
    a site full of financial figures, and the single most important thing to
    know about them is that they are not advice.
    """
    from src.report.legal import SHORT_DISCLAIMER

    return f'  <p class="disclaim" role="note">{SHORT_DISCLAIMER}</p>'
