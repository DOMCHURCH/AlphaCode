"""The sign-up prompt on company pages and blog posts.

Rendered hidden into the page, the same HTML for every reader (company pages
and posts are served from a process-wide cache); `nudge.js` decides whether and
when it shows. Only `company_page.py` and `blog.py` (posts) call this, so it
cannot appear on the home page, /pricing, /dashboard, /login, /dataset, /api,
/about or any admin or checkout page.

The form inside is the home page's own free-key form (`home_page._key_form`),
so the request, the success state, the shown-once key and "Copy key" are the
same code path. No discount is offered here.
"""

from __future__ import annotations

from html import escape

# Where the prompt starts watching: put at the end of the company facts or of
# the post. Reaching it is the trigger.
NUDGE_END = '<div class="nudge-end" data-nudge-end aria-hidden="true"></div>'


def render_signup_nudge(ticker: str | None = None) -> str:
    from src.config.settings import get_settings
    from src.report.company_page import asset_version
    from src.report.home_page import _key_form

    free = get_settings().free_tier_monthly_calls
    heading = (
        f"Get {escape(ticker)} as JSON, free" if ticker else "Get this data as JSON"
    )
    form = _key_form("nudge", ticker or "AAPL")
    return f"""
<aside class="signup-nudge" id="signup-nudge" aria-label="Get a free API key" hidden>
  <button type="button" class="nudge-x" data-nudge-close aria-label="Close">&times;</button>
  <p class="nudge-h">{heading}</p>
  {form}
  <p class="nudge-line">{free:,} calls a month. No card, no password.</p>
  <p class="nudge-fine">By getting a key you accept the <a href="/terms">Terms</a>
    and <a href="/privacy">Privacy Policy</a>.</p>
</aside>
<script src="/static/keyform.js?v={asset_version()}" defer></script>
<script src="/static/nudge.js?v={asset_version()}" defer></script>"""
