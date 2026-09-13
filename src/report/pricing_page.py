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

    The bullets are the differentiator and are not decoration. Each card says
    what the data DOES -- fixed or live, download or call, analysis or
    automation -- because "10,000 API calls a month" tells somebody who has
    never bought market data nothing about whether it is the thing they need.
    """

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
        href = "/dashboard#billing" if plan else "/login"
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

    return f"""
    <div class="plans">
      {card(
        "Free", "$0", "", "Unlimited — look up as many companies as you like",
        (
            "Every company, every balance sheet, no key and no account",
            "The live demo is uncapped too — try the real endpoint on anything",
            f"{free_limit} keyed API calls a month if you want JSON in bulk",
            "Best for: reading the site, and evaluating the API",
        ),
        "Get a key",
    )}
      {card(
        "Full dataset", dataset_price, " once",
        f"Static snapshot of all company data as of {dataset_as_of}",
        (
            "One-time download — no updates",
            f"{dataset_rows} rows as one CSV file",
            "Best for: one-time analysis, research, Excel work",
            "Data is fixed — it does not change",
        ),
        "Buy the dataset", True, "dataset",
    )}
      {card(
        "Pro", pro_price, "/month",
        "Live, up-to-date data",
        (
            "Programmatic access — query any company anytime",
            f"{compact(pro_limit)} API calls per month",
            "Best for: algorithmic trading, dashboards, ongoing research",
            "Data updates daily — you always get the latest filings",
        ),
        "Go Pro", False, "pro",
    )}
      {card(
        "Pro annual", pro_annual_price, "/year",
        f"The same Pro access, paid yearly — save {annual_saving}",
        (
            "Everything in Pro",
            f"{compact(pro_limit)} API calls per month",
            "Two months free against the monthly price",
            "Best for: a workflow you already know you are keeping",
        ),
        "Go Pro annually", False, "pro_annual", f"Save {annual_saving}",
    )}
    </div>"""


# ---------------------------------------------------------------------------
# Dataset vs API, as a table
# ---------------------------------------------------------------------------

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
              buy it. That file never changes again — when Apple files its next
              10-Q, your copy still ends at the quarter before. The API answers
              from the live database, so the same call made tomorrow returns
              tomorrow&rsquo;s filings.</p>
            <p>Buy the dataset if the analysis has an end: a study, a model you
              are fitting once, a spreadsheet. Buy Pro if the thing you are
              building has to keep being right — a dashboard, a screen that runs
              every morning, a strategy that trades on new filings.</p>""",
            True,
        ),
        (
            "Can I buy the dataset and get updates?",
            f"""<p>No. A dataset purchase is one download of one snapshot, and
              it stays yours forever — but it is never refreshed. To get a later
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
              no OAuth dance and no token exchange — one header on an ordinary
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
              day — comfortably a few hundred companies refreshed daily. If your
              use needs more than that, email {contact} before you build against
              the limit rather than after.</p>""",
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
    free_limit: int = 10,
    pro_limit: int = 10_000,
    pro_price: str = "$49",
    pro_annual_price: str = "$490",
    annual_saving: str = "$98",
    dataset_price: str = "$79.99",
    dataset_rows: str = "every",
    dataset_as_of: str = "the last quarterly load",
) -> str:
    from src.report.nav import render_footer

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
      machine-readable version is what costs money — and it comes two ways,
      which are not the same product.</p>
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
          changes — the filings that land next quarter are not in it. No key, no
          calls, no code: open it in Excel, load it into pandas, keep it.</p>
        <p class="split-for"><b>Best for:</b> one-time analysis, research,
          Excel work.</p>
      </div>
      <div class="split-half">
        <span class="plan-name">The API</span>
        <p class="split-lede">A window.</p>
        <p class="split-body">A live query against the same database this site
          draws from. Ask for any company at any time and the answer is current
          when you ask it — including filings that arrived this morning. One
          header, one key, JSON back.</p>
        <p class="split-for"><b>Best for:</b> algorithmic trading, dashboards,
          ongoing research.</p>
      </div>
    </div>
  </section>

  <section class="sec" id="pricing">
    <div class="sec-head"><h2>Plans</h2></div>
    <p class="sec-sub"><b>Looking things up is free and unlimited</b> — every
      drawing, every company, the live demo on the front page, as many as you
      like, no key and no account and no daily counter. The prices below buy
      the data in machine-readable form, in bulk.
      <a href="/blog/sec-xbrl-data-wrong-one-in-five">Why most SEC filings APIs
      are wrong one time in five</a>. Every one of them is
      what Stripe charges; nothing is quoted here that the checkout does not
      agree with.</p>
    {cards}
    <p class="formnote" id="plan-note" role="status" aria-live="polite"></p>
    <p class="plan-note">Paid plans go through Stripe. Your card details are
      entered on Stripe's page and never reach this site.</p>
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
      Pro subscription does not include the CSV export — they are separate
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
            'curl -H "X-API-Key: YOUR_KEY" https://toscale.pro/api/company/AAPL',
        ),
        (
            "What can I build with a balance sheet API?",
            "Anything needing as-reported balance-sheet figures on a schedule "
            "— screens that rank companies on a filed ratio, dashboards that "
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
