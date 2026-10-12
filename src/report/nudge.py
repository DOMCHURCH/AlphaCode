"""The sign-up prompt on company pages and blog posts.

Rendered hidden into the page, the same HTML for every reader (company pages
and posts are served from a process-wide cache); `nudge.js` decides whether and
when it shows. Only `company_page.py` and `blog.py` (posts) call this, so it
cannot appear on the home page, /pricing, /dashboard, /login, /dataset, /api,
/about or any admin or checkout page.

The form inside is the home page's own free-key form (`home_page._key_form`),
so the request, the success state, the shown-once key and "Copy key" are the
same code path.

The one secondary line is the sign-in sheet's existing offer, offered through
its own endpoints (/api/signin-prompt, /api/signin-offer/show, /claim,
/forfeit) and therefore its own rules: shown at most once per address, applied
at checkout from the claim cookie, monthly Pro only, ended by leaving the page.
Its wording is built from the same settings the sheet and the Stripe coupon
use. It stays hidden unless the server says an offer is available.
"""

from __future__ import annotations

from html import escape

# Where the prompt starts watching: put at the end of the company facts or of
# the post. Reaching it is the trigger.
NUDGE_END = '<div class="nudge-end" data-nudge-end aria-hidden="true"></div>'


def render_signup_nudge(ticker: str | None = None) -> str:
    from src.config.settings import get_settings, price_label
    from src.report.company_page import asset_version
    from src.report.home_page import _key_form

    s = get_settings()
    free = s.free_tier_monthly_calls
    pct = s.signin_offer_percent
    off = "half off" if pct == 50 else f"{pct}% off"
    pro = price_label(s.pro_price_usd)
    first = price_label(round(s.pro_price_usd * (100 - pct) / 100, 2))
    offer = f"""
  <p class="nudge-offer" data-nudge-offer hidden>Or
    <a href="/login?next=%2Fdashboard%3Fplan%3Dpro%23billing" data-nudge-claim>claim
    {escape(off)} your first month of Pro</a>: {escape(first)} for the first month,
    then {escape(pro)}/month, monthly Pro only. Once claimed it is applied at
    checkout for {s.signin_offer_claim_minutes} minutes; it is offered once, and
    leaving this page without claiming it ends it.</p>"""
    heading = (
        f"Get {escape(ticker)} as JSON, free" if ticker else "Get this data as JSON"
    )
    form = _key_form("nudge", ticker or "AAPL")
    return f"""
<aside class="signup-nudge" id="signup-nudge" aria-label="Get a free API key" hidden>
  <button type="button" class="nudge-x" data-nudge-close aria-label="Close">&times;</button>
  <p class="nudge-h">{heading}</p>
  {form}
  <p class="nudge-line">{free:,} calls a month. No card, no password.</p>{offer}
  <p class="nudge-fine">By getting a key you accept the <a href="/terms">Terms</a>
    and <a href="/privacy">Privacy Policy</a>.</p>
</aside>
<script src="/static/keyform.js?v={asset_version()}" defer></script>
<script src="/static/nudge.js?v={asset_version()}" defer></script>"""
