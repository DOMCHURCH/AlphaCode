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
    ("Pricing", "/pricing", "pricing"),
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
            '<span class="vh"> — log in to access your dashboard</span>'
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
  <a class="brand{home_on}" href="/"><span class="dot"></span>To&nbsp;Scale</a>
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
</nav>"""


# The repository the site is built from. A link rather than a claim: "open
# source" is checkable, and this is where it is checked.
SOURCE_URL = "https://github.com/domchurch/alphacode"


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
