"""One page for the things that are not a page: an error, a dead end, a 404.

Everything else on this site renders something it has: a drawing, a dashboard,
a document. This module renders the cases where there is nothing to render, and
it has one job -- say what happened and offer the next step, in the site's own
clothes rather than in the browser's default serif on white.

A 404 that looks like a different website is the moment a reader concludes the
site is broken rather than that they mistyped a URL. So these go through the
same shell as everything else, backdrop included, and they carry `noindex`:
an error page is never a search result worth having.
"""

from __future__ import annotations

from html import escape

from src.report._shell import NOINDEX
from src.report.company_page import asset_version
from src.report.home_page import shell


def render_message(
    *,
    title: str,
    message: str,
    nav: str = "",
    action: tuple[str, str] | None = None,
    status_title: str | None = None,
) -> str:
    """A heading, a sentence, and at most one way forward.

    `action` is (label, href). One, or none -- a page that has gone wrong is the
    worst place to offer somebody a choice of five things.
    """
    link = ""
    if action is not None:
        label, href = action
        link = (
            f'<p class="msg-act"><a class="plan-cta" '
            f'href="{escape(href, quote=True)}">{escape(label)}</a></p>'
        )
    body = f"""{nav}
<main class="wrap" id="main">
  <div class="empty">
    <h1>{escape(title)}</h1>
    <p>{escape(message)}</p>
    {link}
  </div>
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>"""
    return shell(
        f"To Scale — {status_title or title}",
        body,
        # The still, veiled hard. Something has gone wrong; this is not the
        # moment for a sunset behind the explanation.
        film="calm",
        robots=NOINDEX,
    )


def render_404(path: str = "", nav: str = "") -> str:
    """The page for a URL that is not one of ours.

    It offers the search box rather than a list of links, because the commonest
    way to land here is a mistyped or stale ticker URL, and the search box is
    the thing that fixes that in one move.
    """
    from src.report.home_page import search_form

    body = f"""{nav}
<main class="wrap" id="main">
  <div class="empty">
    <h1>There is nothing at this address</h1>
    <p>The page you asked for does not exist. If you were looking for a
      company, search for it by ticker or by name.</p>
    {search_form()}
    <p class="tryline"><a href="/">Back to the front page</a> ·
      <a href="/api">API reference</a></p>
  </div>
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>"""
    return shell(
        "To Scale — page not found", body, film="calm", robots=NOINDEX
    )
