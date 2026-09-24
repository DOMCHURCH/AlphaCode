"""The plan features on the dashboard: Data and Alerts tabs, plus buy cards.

Everything a paid plan includes is usable here, not only through the API:

- Data: balance sheet history (depth by plan), what changed / restatements
  (Starter+), and bulk verification (Pro+).
- Alerts: the watchlist that drives the filing emails (Starter+), and
  webhooks (Business+).

The panels render the same for everyone; `features.js` reads the caller's
tier from /api/user/status and the plan matrix embedded below, and either
enables a section or replaces it with a short note naming the plan that
unlocks it. The server enforces the same rules on every call regardless.
"""

from __future__ import annotations

import json
from html import escape


def _lock(feature: str) -> str:
    return (f'<p class="plan-note locknote" data-lock="{feature}" hidden></p>')


def render_feature_panels() -> str:
    return f"""
  <section class="sec keyed panel" id="panel-data" data-panel="data" hidden>
    <div class="sec-head"><h2>Company data</h2></div>
    <p class="sec-sub">History and what changed for one company, or a quick
      check of many. What you can see depends on your plan.</p>

    <form class="search" id="hist-form" autocomplete="off">
      <label class="slabel" for="hist-q">Ticker</label>
      <div class="sfield">
        <input id="hist-q" type="text" placeholder="AAPL" maxlength="16"
          autocapitalize="characters" spellcheck="false">
        <button type="submit" class="btn" id="hist-btn">Show history</button>
      </div>
    </form>
    <p class="formnote" id="hist-note" role="status" aria-live="polite"></p>
    <div id="hist-out"></div>

    <h3 class="feat-h">What changed</h3>
    {_lock("changes")}
    <div id="chg-out" class="feat-body" data-feature="changes"></div>

    <h3 class="feat-h">Check many companies</h3>
    {_lock("bulk_verify")}
    <form id="verify-form" class="feat-body" data-feature="bulk_verify" autocomplete="off">
      <label class="slabel" for="verify-q">Tickers, separated by spaces or commas</label>
      <textarea id="verify-q" rows="3" placeholder="AAPL, JPM, BRK.B"></textarea>
      <button type="submit" class="btn" id="verify-btn">Check them</button>
      <p class="formnote" id="verify-note" role="status" aria-live="polite"></p>
      <div id="verify-out"></div>
    </form>
  </section>

  <section class="sec keyed panel" id="panel-alerts" data-panel="alerts" hidden>
    <div class="sec-head"><h2>Alerts</h2></div>
    <p class="sec-sub">Follow companies and get an email when they file: whether
      the new balance sheet reconciles, what moved, and anything restated.</p>

    <h3 class="feat-h">Watchlist <small id="watch-count"></small></h3>
    {_lock("watchlist")}
    <div class="feat-body" data-feature="watchlist">
      <form id="watch-form" autocomplete="off">
        <div class="sfield">
          <input id="watch-q" type="text" placeholder="Ticker to follow" maxlength="16"
            autocapitalize="characters" spellcheck="false">
          <button type="submit" class="btn" id="watch-btn">Follow</button>
        </div>
      </form>
      <p class="formnote" id="watch-note" role="status" aria-live="polite"></p>
      <ul class="notelist" id="watch-list"></ul>
    </div>

    <h3 class="feat-h">Webhooks <small id="hook-count"></small></h3>
    {_lock("webhooks")}
    <div class="feat-body" data-feature="webhooks">
      <p class="plan-note">Alerts can also be sent to your own system as a signed
        POST. Details are on the <a href="/api">API page</a>.</p>
      <form id="hook-form" autocomplete="off">
        <div class="sfield">
          <input id="hook-q" type="url" placeholder="https://your-app.example.com/hooks" maxlength="500">
          <button type="submit" class="btn" id="hook-btn">Add</button>
        </div>
      </form>
      <p class="formnote" id="hook-note" role="status" aria-live="polite"></p>
      <div class="banner" id="hook-secret" hidden>
        <p><strong>Your signing secret.</strong> Copy it now; it is not shown again.</p>
        <p><code id="hook-secret-text"></code></p>
      </div>
      <ul class="notelist" id="hook-list"></ul>
    </div>
  </section>"""


def render_buy_cards(tiers: tuple[str, ...] = ("starter", "business")) -> str:
    """Starter and/or Business as compact plan buttons, only if on sale."""
    from src import billing
    from src.config.settings import get_settings, price_label

    s = get_settings()
    out = []
    for tier in tiers:
        if not billing.price_for(tier):
            continue
        name = tier.capitalize()
        month = price_label(getattr(s, f"{tier}_price"))
        out.append(f'<button type="button" class="btn ghost pick" data-buy-plan="{tier}" '
                   f'data-buy-name="{name}">{name} {escape(month)}/mo</button>')
        if billing.price_for(f"{tier}_annual"):
            year = price_label(getattr(s, f"{tier}_annual_price"))
            out.append(f'<button type="button" class="btn ghost pick" data-buy-plan="{tier}_annual" '
                       f'data-buy-name="{name} annual">{name} {escape(year)}/yr</button>')
    return "".join(out)


def plans_json() -> str:
    """The plan matrix for features.js, as a JSON literal safe inside <script>."""
    from src import plans

    return json.dumps({"matrix": plans.MATRIX, "names": plans.TIER_NAMES,
                       "order": list(plans.TIER_ORDER)}).replace("</", "<\\/")
