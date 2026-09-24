"""/pricing — the plans, side by side, with the one question answered.

This page exists because of a specific confusion: the dataset and the API read
as the same product at two prices, so a buyer looks at $79.99 once against $49
a month and concludes the cheap one is the same thing. It is not. **The dataset
is a photograph and the API is a window.** One is a file that stops being true
the moment the next quarter lands; the other is a call that is true when you
make it. Everything on this page is arranged to make that difference the first
thing read and the last thing remembered, because a buyer who gets it wrong
bought the wrong product and finds out a month later.

The card copy lives here rather than in the templates that show it. The home
page and this page must not be able to disagree about what a plan is -- and
they would, the first time one was edited and the other was not -- so both
render `plan_cards()` and there is exactly one place a price or a promise is
written.

/pricing used to be a 307 to `/#pricing`. It is now a real page for the reason
its own redirect anticipated: it is the `cancel_url` of every Checkout Session,
which makes it the page somebody sees at the exact moment they have decided not
to buy something, and a fragment jump on the home page is the worst possible
answer to "wait, what is the difference?".
"""

from __future__ import annotations

import datetime as dt
from html import escape

from src.report.company_page import asset_version
from src.report.home_page import companies_label, shell


def compact(n: int) -> str:
    """1,237,331 -> "1.2M"; 5,000 -> "5,000".

    The SAME rule as `home_page._compact`, and it has to be: these cards are
    rendered on both pages, and a plan that says "5,000 API calls" on the home
    page and "5k" on /pricing is two different promises about one product. The
    threshold is 10k rather than 1k on purpose -- a monthly allowance is a
    number somebody checks their usage against, and "5k" is a rounding where
    "5,000" is a figure.
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:,}"


def snapshot_date(generated: dt.datetime | dt.date | None) -> str:
    """The date the dataset's contents were last added to, as a reader reads it.

    "as of" with nothing after it is worse than no date at all -- it reads as a
    page that does not know -- so an empty database says so in words instead.
    """
    if generated is None:
        return "the last quarterly load"
    if isinstance(generated, dt.datetime):
        generated = generated.date()
    return generated.strftime("%-d %B %Y") if _supports_dash() else generated.strftime(
        "%d %B %Y"
    ).lstrip("0")


def _supports_dash() -> bool:
    """Whether `%-d` is a real directive here. It is glibc's, not Windows'.

    Checked rather than assumed because this code is developed on Windows and
    runs on Linux, and an unsupported directive is a ValueError on a page that
    is trying to sell something.
    """
    try:
        dt.date(2026, 1, 5).strftime("%-d")
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# The plans, in one place
# ---------------------------------------------------------------------------

def _pro_card(pro_price: str, pro_annual_price: str,
              annual_saving: str, pro_limit: int) -> str:
    """Pro, with the billing period as a toggle instead of a second card.

    Monthly and annual were two cards side by side, which asked a reader to
    compare two nearly identical lists to find the one line that differed --
    and put the cheaper-looking number first, so the annual saving read as a
    more expensive plan rather than a discount.

    One card, one control. Everything the period changes carries a data
    attribute and is swapped by home.js: the price, the unit, the saving line,
    the button's label, and -- the one that matters -- the `data-plan` the
    checkout reads. Without JavaScript it stays exactly as it renders here,
    which is the monthly plan with a working button, so the fallback is a sale
    rather than a dead card.
    """
    saving = escape(annual_saving)
    return f"""
    <div class="plan pro-plan period-plan">
      <span class="plan-name">Pro</span>
      <div class="bill-toggle in-card" role="group" aria-label="Billing period">
        <a class="bill-opt on" href="/dashboard#billing" data-period="month"
          data-plan="pro" aria-current="true">Monthly</a>
        <a class="bill-opt" href="/dashboard#billing" data-period="year"
          data-plan="pro_annual" aria-current="false">Yearly
          <small>save {saving}</small></a>
      </div>
      <p class="plan-price"><span data-price>{escape(pro_price)}</span><small
        data-per>/month</small></p>
      <p class="plan-line" data-line>Live, up-to-date data</p>
      <ul class="plan-bullets">
        <li>Data updates daily, so you always get the latest filings</li>
        <li>Full history, and what changed</li>
        <li>Email alerts on the companies you follow</li>
      </ul>
      <a class="plan-cta" href="/dashboard#billing" data-plan="pro" data-name="Pro"
        data-plan-month="pro" data-plan-year="pro_annual"
        data-price-month="{escape(pro_price)}" data-price-year="{escape(pro_annual_price)}"
        data-line-year="The same Pro access, paid yearly. Save {saving}"
        >Start Pro</a>
    </div>"""


def _tier_card(tier: str, *, line: str, year_line: str,
               bullets: tuple[str, ...]) -> str:
    """Starter or Business -- rendered ONLY once its Stripe Price is set.

    Unset means `billing.price_for(tier)` is empty and checkout would 503, so
    the card is simply absent: setting STRIPE_PRICE_STARTER on Railway is what
    makes the Starter card appear. The yearly option is offered only when the
    annual Price is set too; otherwise the card stays monthly under the toggle.
    """
    from src import billing
    from src.config.settings import get_settings, price_label

    if not billing.price_for(tier):
        return ""
    s = get_settings()
    month = price_label(getattr(s, f"{tier}_price"))
    year = price_label(getattr(s, f"{tier}_annual_price"))
    saved = max(0, getattr(s, f"{tier}_price") * 12 - getattr(s, f"{tier}_annual_price"))
    if saved:
        year_line = f"{year_line}. Save {price_label(saved)}"
    has_year = bool(billing.price_for(f"{tier}_annual"))
    name = tier.capitalize()
    items = "".join(f"<li>{escape(b)}</li>" for b in bullets)
    year_attrs = (
        f' data-plan-year="{tier}_annual" data-price-year="{escape(year)}"'
        f' data-line-year="{escape(year_line)}"' if has_year else ""
    )
    return f"""
    <div class="plan period-plan">
      <span class="plan-name">{name}</span>
      <p class="plan-price"><span data-price>{escape(month)}</span><small
        data-per>/month</small></p>
      <p class="plan-line" data-line>{escape(line)}</p>
      <ul class="plan-bullets">{items}</ul>
      <a class="plan-cta" href="/dashboard#billing" data-plan="{tier}" data-name="{name}"
        data-plan-month="{tier}" data-price-month="{escape(month)}"{year_attrs}
        >Start {name}</a>
    </div>"""


def _starter_card() -> str:
    from src import plans
    from src.accounts import tier_limit

    return _tier_card(
        "starter", line="Follow a few companies",
        year_line="The same Starter plan, paid yearly",
        bullets=(
            # Not "5 years": the database holds about three (2026-09-24).
            "Multi-year history",
            "See what changed, including restatements",
            f"Alerts on {plans.allowance('starter', 'watchlist')} companies",
        ),
    )


def _business_card() -> str:
    from src import plans
    from src.accounts import tier_limit

    return _tier_card(
        "business", line="For products built on the data",
        year_line="The same Business plan, paid yearly",
        bullets=(
            "Everything in Pro",
            "Webhooks into your own systems",
            "A link to the SEC filing behind every figure",
        ),
    )


def plan_cards(
    *,
    free_limit: int,
    pro_limit: int,
    pro_price: str,
    pro_annual_price: str,
    annual_saving: str,
    dataset_price: str,
    dataset_rows: str,
    dataset_as_of: str,
) -> str:
    """The four cards. Rendered identically on the home page and on /pricing.

    Every paid card carries `data-plan`, which home.js turns into a Checkout
    Session, and an href that is a REAL fallback rather than a placeholder:
    with the script blocked the link still reaches a place the purchase can be
    finished, and that place is the billing panel, not /login.

    The free card lands on /dashboard for the same reason. It used to go to
    /login, which is headed "Sign in" and is written for somebody who already
    has an account -- the one thing a reader pressing "Get a key" on the free
    tier does not have. /dashboard opens with a section headed "Get a key" and
    an address field, which is the sentence the button promised, and it is
    already where the same button on /api points.

    The bullets are the differentiator and are not decoration. Each card says
    what the data DOES -- fixed or live, download or call, analysis or
    automation -- because "10,000 API calls a month" tells somebody who has
    never bought market data nothing about whether it is the thing they need.
    """

    from src.report.nav import SUPPORT_EMAIL

    # Present only once their Stripe Prices are configured; see _tier_card.
    starter = _starter_card()
    business = _business_card()
    grid = "plans plans-6" if (starter or business) else "plans"
    # Built as plain strings so their position can follow the layout: with
    # four cards the dataset sits second (as it always has); with six, the
    # subscriptions read left to right by price and the dataset goes last.

    def card(
        name: str,
        price: str,
        per: str,
        line: str,
        bullets: tuple[str, ...],
        cta: str,
        feature: bool = False,
        plan: str = "",
        badge: str = "",
    ) -> str:
        href = "/dashboard#billing" if plan else "/dashboard"
        attr = f' data-plan="{escape(plan)}"' if plan else ""
        tag = f'<span class="badge">{escape(badge)}</span>' if badge else ""
        items = "".join(f"<li>{escape(b)}</li>" for b in bullets)
        return f"""
    <div class="plan{' feature' if feature else ''}">
      <span class="plan-name">{escape(name)}{tag}</span>
      <p class="plan-price">{escape(price)}<small>{escape(per)}</small></p>
      <p class="plan-line">{escape(line)}</p>
      <ul class="plan-bullets">{items}</ul>
      <a class="plan-cta" href="{href}"{attr}>{escape(cta)}</a>
    </div>"""

    enterprise = card(
        "Enterprise", "Let's talk", "",
        "Custom volume and terms",
        (
            "Everything in Business, plus the dataset",
            "A call quota set to what you use",
            "Invoicing and a signed agreement",
        ),
        "Email us",
    ).replace('href="/dashboard"',
              'href="mailto:' + SUPPORT_EMAIL + '?subject=Enterprise%20enquiry"')
    dataset = card(
        "Full dataset", dataset_price, " once",
        f"Static snapshot of all company data as of {dataset_as_of}",
        (
            "One-time download. No updates.",
            f"{dataset_rows} rows as one CSV file",
            "The snapshot never changes",
        ),
        "Buy the dataset", True, "dataset",
    )
    dataset_first, dataset_last = ("", dataset) if grid.endswith("6") else (dataset, "")
    from src.accounts import tier_limit

    shown = ["free"] + (["starter"] if starter else []) + ["pro"] + (["business"] if business else [])
    allowances = ", ".join(f"{t.capitalize()} {compact(tier_limit(t) if t != 'free' else free_limit)}"
                           for t in shown)

    return f"""
    <div class="bill-lead">
      <span class="bill-lead-label">Billing period</span>
      <div class="bill-toggle" role="group" aria-label="Billing period">
        <a class="bill-opt on" href="/dashboard#billing" data-period="month"
          data-plan="pro" aria-current="true">Monthly</a>
        <a class="bill-opt" href="/dashboard#billing" data-period="year"
          data-plan="pro_annual" aria-current="false">Yearly
          <small>save {escape(annual_saving)}</small></a>
      </div>
    </div>
    <div class="{grid}">
      {card(
        "Free", "$0", "", "Unlimited: look up as many companies as you like",
        (
            "Every balance sheet, no account needed",
            f"{free_limit:,} keyed API calls a month",
            "Works with Claude and other AI tools",
        ),
        "Get a free API key",
    )}
      {dataset_first}
      {starter}
      {_pro_card(pro_price, pro_annual_price, annual_saving, pro_limit)}
      {business}
      {dataset_last}
      {enterprise}
    </div>
    <p class="plan-note plan-allowances">API calls a month: {allowances}.
      <a href="/api/plans">Every plan side by side</a></p>"""


# ---------------------------------------------------------------------------
# Dataset vs API, as a table
# ---------------------------------------------------------------------------

def coverage_line() -> str:
    """What the plans are plans FOR, in one line.

    Every other page carries the coverage; /pricing described what a tier
    buys without ever saying what it buys access TO, which leaves the reader
    weighing {FREE_CALLS} calls a month against a universe they have to go and find.

    Counts read live. `identity_breakdown()` for the companies -- the same
    source the home page definition and /methodology use, so the three cannot
    disagree -- and `facts_label` for the row count, which is the one place
    that figure is computed. "" when either is unreadable: a coverage claim
    reading "0 companies" would be worse than no coverage claim.
    """
    from src.company.stats import identity_breakdown
    from src.dataset import facts_label
    from src.report.home_page import plural

    try:
        b = identity_breakdown() or {}
    except Exception:  # noqa: BLE001 - copy must not take a page down
        b = {}
    companies = int(b.get("companies") or 0)
    facts = facts_label()
    if not companies or not facts:
        return ""
    return (
        f'<p class="sec-sub">Coverage: {companies:,} US-listed '
        f"{plural(companies, 'company', 'companies')}, {facts} as-reported "
        "facts, every balance sheet reconciled against A = L + E.</p>"
    )


def comparison_table(
    *, dataset_price: str, pro_price: str, pro_limit: int
) -> str:
    """The five rows that separate a snapshot from a subscription.

    A table rather than two columns of prose because the reader's question is
    comparative -- "which of these two" -- and prose makes them hold one
    product in their head while reading about the other.
    """
    yes = '<span class="yes" aria-label="yes">&#10003;</span>'
    no = '<span class="no" aria-label="no">&#10007;</span>'
    # Same universe whichever way you buy it, which is the point of saying so
    # in a table that otherwise only lists differences. Em dash when the count
    # cannot be read, rather than a zero that reads as a claim.
    from src.company.stats import identity_breakdown

    try:
        n = int((identity_breakdown() or {}).get("companies") or 0)
    except Exception:  # noqa: BLE001 - one row must not take the page
        n = 0
    covered = f"{n:,}" if n else "&#8212;"
    return f"""
    <div class="tablewrap">
      <table class="compare compare-3">
        <caption class="vh">The dataset and the API, compared</caption>
        <thead>
          <tr>
            <th scope="col">Feature</th>
            <th scope="col">Dataset <small>({escape(dataset_price)} once)</small></th>
            <th scope="col">Pro API <small>({escape(pro_price)}/month)</small></th>
          </tr>
        </thead>
        <tbody>
          <tr><th scope="row">Data freshness</th>
              <td>Static (as of download date)</td>
              <td>Live (updates daily)</td></tr>
          <tr><th scope="row">Access method</th>
              <td>One CSV download</td>
              <td>API calls ({compact(pro_limit)}/month)</td></tr>
          <tr><th scope="row">Use case</th>
              <td>One-time analysis</td>
              <td>Ongoing automation</td></tr>
          <tr><th scope="row">Updates</th>
              <td>{no} No (buy again)</td>
              <td>{yes} Yes (monthly)</td></tr>
          <tr><th scope="row">Automation</th>
              <td>{no} Manual</td>
              <td>{yes} Programmatic</td></tr>
          <tr><th scope="row">Companies covered</th>
              <td>{covered}</td>
              <td>{covered}</td></tr>
          <tr><th scope="row">Drawings &amp; search on this site</th>
              <td>{yes} Free, unlimited</td>
              <td>{yes} Free, unlimited</td></tr>
        </tbody>
      </table>
    </div>"""


# ---------------------------------------------------------------------------
# FAQ
# ---------------------------------------------------------------------------

def faq(
    *,
    dataset_price: str,
    pro_price: str,
    pro_limit: int,
    base_url: str,
    contact: str,
) -> str:
    """Four questions, in `<details>` so the answers cost nothing to skip.

    They are the four asked before a purchase and the four asked in email
    afterwards, which is the same list -- answering them here is the difference
    between a sale and a message that gets replied to on Tuesday.

    Open by default is deliberate for the first one: it is the question this
    entire page exists to answer, and a reader should not have to click to
    find out that the two products are different.
    """
    host = escape(base_url.rstrip("/"))
    rows = (
        (
            "What&rsquo;s the difference between the dataset and the API?",
            f"""<p>The dataset is a <strong>photograph</strong>; the API is a
              <strong>window</strong>. For {escape(dataset_price)} you download one
              CSV containing every filed figure in the database at the moment you
              buy it. That file never changes again: when Apple files its next
              10-Q, your copy still ends at the quarter before. The API answers
              from the live database, so the same call made tomorrow returns
              tomorrow&rsquo;s filings.</p>
            <p>Buy the dataset if the analysis has an end: a study, a model you
              are fitting once, a spreadsheet. Buy Pro if the thing you are
              building has to keep being right: a dashboard, a screen that runs
              every morning, a strategy that trades on new filings.</p>""",
            True,
        ),
        (
            "Can I buy the dataset and get updates?",
            f"""<p>No. A dataset purchase is one download of one snapshot, and
              it stays yours forever, but it is never refreshed. To get a later
              snapshot you buy the dataset again at {escape(dataset_price)}.</p>
            <p>If you find yourself wanting a second copy, Pro at
              {escape(pro_price)}/month is almost certainly the cheaper answer, and
              it gives you the data without a re-download: the API is already
              current every time you call it.</p>""",
            False,
        ),
        (
            "How do I use the API key?",
            f"""<p>Send it as an <code>X-API-Key</code> header. There is no SDK,
              no OAuth dance and no token exchange, just one header on an ordinary
              GET:</p>
            <pre class="code"><code>curl -H "X-API-Key: YOUR_KEY" \\
  {host}/api/company/AAPL</code></pre>
            <p>Your key is on the <a href="/dashboard">API tab of the
              dashboard</a>, with the same examples filled in with your real key
              and this deployment&rsquo;s host. Keep it server-side: anyone who has
              the string can spend your monthly allowance.</p>""",
            False,
        ),
        (
            "What can I build with the API?",
            f"""<p>Anything that needs as-reported balance-sheet figures on a
              schedule. In practice that has been screens that rank companies on
              a filed ratio, dashboards that redraw when a 10-Q lands, backtests
              that need the figure as it was reported rather than as it was later
              restated, and research notebooks that pull a peer group in one
              loop.</p>
            <p>Pro is {compact(pro_limit)} calls a month, which is roughly 330 a
              day, comfortably a few hundred companies refreshed daily. If your
              use needs more than that, email {contact} before you build against
              the limit rather than after.</p>""",
            False,
        ),
        (
            "Can I get a refund?",
            """<p>Pro, monthly or annual, carries a <strong>14-day refund
              window</strong>: if it is not what you needed, email within 14 days
              of paying and the payment is refunded in full, no questions asked.
              The refund ends Pro access and your key returns to the free
              allowance; nothing is deleted.</p>
            <p>The dataset is a file, and a file cannot be handed back, so it is
              non-refundable once downloaded. If it is broken or not what the
              page described, say so and it will be made right.</p>""",
            False,
        ),
    )
    items = "".join(
        f"""
      <details class="faq-item"{' open' if is_open else ''}>
        <summary>{question}</summary>
        <div class="faq-a">{answer}</div>
      </details>"""
        for question, answer, is_open in rows
    )
    return f"""
  <section class="sec" id="faq">
    <div class="sec-head"><h2>Questions</h2></div>
    <div class="faq">{items}</div>
  </section>"""


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def render_pricing(
    *,
    nav: str = "",
    base_url: str = "",
    admin_email: str = "",
    free_limit: int = 1000,
    pro_limit: int = 10_000,
    pro_price: str = "$49",
    pro_annual_price: str = "$490",
    annual_saving: str = "$98",
    dataset_price: str = "$79.99",
    dataset_rows: str = "every",
    dataset_as_of: str = "the last quarterly load",
) -> str:
    from src.report.nav import SUPPORT_EMAIL, render_footer

    contact = (
        f'<a class="mail" href="mailto:{escape(admin_email)}">{escape(admin_email)}</a>'
        if admin_email
        else "the site owner"
    )
    cards = plan_cards(
        free_limit=free_limit,
        pro_limit=pro_limit,
        pro_price=pro_price,
        pro_annual_price=pro_annual_price,
        annual_saving=annual_saving,
        dataset_price=dataset_price,
        dataset_rows=dataset_rows,
        dataset_as_of=dataset_as_of,
    )

    body = f"""{nav}
<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">SEC Filings API Pricing</h1>
    <p class="hlede">The drawings are free and always will be. The
      machine-readable version is what costs money, and it comes two ways,
      which are not the same product.</p>
    {coverage_line()}
  </header>

  <!-- Said once, plainly, above everything priced. A reader who takes only
       this sentence away has taken away the thing that stops them buying the
       wrong one. -->
  <section class="sec" id="which">
    <div class="sec-head"><h2>Which one do you want?</h2></div>
    <div class="split">
      <div class="split-half">
        <span class="plan-name">The dataset</span>
        <p class="split-lede">A photograph.</p>
        <p class="split-body">One CSV, downloaded once, containing every filed
          figure as of {escape(dataset_as_of)}. It is yours forever and it never
          changes: the filings that land next quarter are not in it. No key, no
          calls, no code: open it in Excel, load it into pandas, keep it.</p>
        <p class="split-for"><b>Best for:</b> one-time analysis, research,
          Excel work.</p>
      </div>
      <div class="split-half">
        <span class="plan-name">The API</span>
        <p class="split-lede">A window.</p>
        <p class="split-body">A live query against the same database this site
          draws from. Ask for any company at any time and the answer is current
          when you ask it, including filings that arrived this morning. One
          header, one key, JSON back.</p>
        <p class="split-for"><b>Best for:</b> algorithmic trading, dashboards,
          ongoing research.</p>
      </div>
    </div>
  </section>

  <section class="sec" id="pricing">
    <div class="sec-head"><h2>Plans</h2></div>
    <p class="sec-sub"><b>Looking things up is free and unlimited</b>: every
      drawing, every company, as many as you
      like, no key and no account and no daily counter. The live demo on the
      front page calls the real API, so it is generous rather than unlimited:
      plenty to evaluate with, and a free key if you need more than that. The
      prices below buy the data in machine-readable form, in bulk.
      <a href="/blog/sec-xbrl-data-wrong-one-in-five">Why most SEC filings APIs
      are wrong one time in five</a>. Every one of them is
      what Stripe charges; nothing is quoted here that the checkout does not
      agree with. <strong>All prices are in Canadian dollars (CAD).</strong></p>
    {cards}
    <p class="formnote" id="plan-note" role="status" aria-live="polite"></p>
    <p class="plan-note">Paid plans go through Stripe. Your card details are
      entered on Stripe's page and never reach this site.</p>
    <p class="plan-note">A question before you buy, or a problem after?
      <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.</p>
    <p class="plan-note">Still shortlisting?
      <a href="/best/sec-filings-api-for-quants">The SEC filings APIs worth
      considering for quant work</a> covers the alternatives, this one
      included, and compares them on method rather than on price.</p>
  </section>

  <section class="sec" id="compare">
    <div class="sec-head"><h2>Dataset vs API</h2></div>
    <p class="sec-sub">The same data behind both. What differs is whether it
      keeps arriving.</p>
    {comparison_table(
        dataset_price=dataset_price, pro_price=pro_price, pro_limit=pro_limit
    )}
    <p class="plan-note">Buying the dataset does not include API access, and a
      Pro subscription does not include the CSV export. They are separate
      purchases because they are separate things. A Pro subscriber who also
      wants the file can <a href="/dataset">buy the dataset</a> at any time.</p>
  </section>

{faq(
    dataset_price=dataset_price,
    pro_price=pro_price,
    pro_limit=pro_limit,
    base_url=base_url,
    contact=contact,
)}

{render_footer(f"Questions about a plan: {contact}.")}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/home.js?v={asset_version()}" defer></script>"""

    from src.report.schema import breadcrumb_ld, faq_ld, pricing_ld

    # The FAQ on this page is the highest-value block on the site for AI
    # citation: four questions somebody actually types, answered in full
    # sentences that stand alone out of context. Marked up so a model quoting
    # one of them has the question attached to it.
    faq_pairs = [
        (
            "What's the difference between the SEC dataset and the API?",
            "The dataset is a static CSV snapshot of every filed figure at the "
            "moment you buy it, for one-time analysis in Excel or pandas. The "
            "API answers from the live database, so a call made tomorrow "
            "returns tomorrow's filings. Buy the dataset if the analysis has "
            "an end; buy the API if what you are building has to keep being "
            "right.",
        ),
        (
            "Can I buy the SEC dataset and get updates?",
            "No. A dataset purchase is one download of one snapshot and it is "
            "never refreshed. To get a later snapshot you buy it again. If you "
            "find yourself wanting a second copy, the Pro API is cheaper and "
            "is current every time you call it.",
        ),
        (
            "How do I use the BalanceProof API key?",
            "Send it as an X-API-Key header on an ordinary GET. There is no "
            "SDK, no OAuth and no token exchange: "
            'curl -H "X-API-Key: YOUR_KEY" https://balanceproof.dev/api/company/AAPL',
        ),
        (
            "What can I build with a balance sheet API?",
            "Anything needing as-reported balance-sheet figures on a schedule: "
            "screens that rank companies on a filed ratio, dashboards that "
            "redraw when a 10-Q lands, backtests that need the figure as it "
            "was reported rather than as it was later restated, and research "
            "notebooks that pull a peer group in one loop.",
        ),
    ]
    return shell(
        "Pricing — BalanceProof",
        body,
        description=(
            "Free tier, $49/mo Pro, or $79.99 once for the dataset. Every "
            "figure traced to its source, every exception named. "
            f"{companies_label()} companies."
        ),
        canonical="/pricing",
        ld=pricing_ld()
        + faq_ld(faq_pairs)
        + breadcrumb_ld([("Home", "/"), ("Pricing", "/pricing")]),
    )
