"""/about — the page that answers "what is BalanceProof" and "who built it".

Google's AI Overview was answering both questions by assembling a description
out of /company/IDT, /company/LLPS and /company/OBIO. Company pages are the
site's most numerous indexable pages and every one of them describes the
product in passing, so in the absence of a page that describes it on purpose,
those are what got cited.

This is that page. It is deliberately short and deliberately flat: an engine
quoting it should be able to lift a whole paragraph without landing halfway
through a sales pitch. The definition is the same string the home page leads
with -- imported, not retyped -- because two pages defining the product in
different words give an engine a reason to trust neither.
"""

from __future__ import annotations

from src.report.company_page import asset_version
from src.report.home_page import product_definition, shell

# Everything except the definition, which is read live and injected in `render`.
_BODY = """
  <article>
    <h2>Who builds it</h2>
    <p>BalanceProof is built in Ottawa, Canada, by founder Dominique Church.
      It started as a tool for a personal project and became a public API
      because every free SEC data source we tried disagreed with the filing
      somewhere.</p>

    <h2>Who it is for</h2>
    <p>We build it for data engineers, quants, fintech teams and analysts who
      work with US public companies, in the US and Canada. Coverage is the
      companies that file periodic reports (10-K and 10-Q) with the US SEC.
      Our focus is one thing: checking SEC balance sheets against Assets =
      Liabilities + Equity and publishing the reason when a filing does not
      balance. We write about it in <a href="/blog">our research</a>: bank
      balance sheets, XBRL duplicate tags, restatements and point-in-time
      figures.</p>

    <h2>The method</h2>
    <p>Each filing is checked against Assets = Liabilities + Equity when it reports the lines needed; where it does not, it is marked not testable instead of passing. A filing that reconciles is published with its figures. A
      filing that does not is published with the reason: noncontrolling
      interests, mezzanine equity, rounding, or a genuinely broken filing. It
      is never quietly adjusted until it balances. The counts are public and
      the categories are named: <a href="/methodology">how we check the
      numbers</a>. How many filings fail and why is in
      <a href="/blog/which-sec-balance-sheets-fail-the-identity-check">the report on 38,568 balance sheets</a>.</p>

    <h2>What it does not do</h2>
    <p>No predictions. No scores. No recommendations. Nothing is estimated,
      smoothed or restated. Where a filer does not report a component, the page
      says so rather than showing a zero.</p>

    <h2>Using it</h2>
    <p>Looking companies up on this site is free and needs no account. The
      machine-readable version, the REST API and the bulk CSV, is what costs
      money: <a href="/pricing">what it costs</a>, and
      <a href="/api">the API reference</a>.</p>
  </article>
"""


def render_about(*, nav: str = "") -> str:
    """The About page.

    `product_definition()` rather than a copy of it: the four counts inside it
    are read from `identity_breakdown()`, and a second hand-maintained copy of
    a quarterly-moving figure is the bug this codebase already documents twice.
    """
    from src.report.nav import SUPPORT_EMAIL, render_footer
    from src.report.schema import PRODUCT_CLAUSE, aboutpage_ld, breadcrumb_ld

    footer = render_footer(
        "Something here wrong, or a figure you think does not match the "
        f'filing? <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.'
    )
    head = (
        '\n  <header class="mhead">\n'
        "    <h1>About BalanceProof</h1>\n"
        f'    <p class="lede">{product_definition()}</p>\n'
        "  </header>\n"
    )
    body = (
        f'{nav}\n<main class="wrap post" id="main">\n'
        f"{head}{_BODY}\n{footer}\n</main>\n"
        f'<script src="/static/nav.js?v={asset_version()}" defer></script>'
    )
    return shell(
        "About BalanceProof — Reconciled SEC Balance Sheet API",
        body,
        # Composed from the shared clause, not retyped around it. This page
        # keeps its OWN description rather than reusing SITE_DESCRIPTION
        # wholesale -- eight shell pages sharing one sentence is what the
        # per-page descriptions were introduced to stop, and the author line
        # is the half that makes this one /about's. What it must never do is
        # describe the product differently from the page it sits on, which is
        # exactly what it was doing: the body said "verification layer" while
        # the meta tag still said "financial data API". 155 characters, so it
        # is not truncated. The identity parenthetical is dropped to make room
        # for the attribution, as it was before.
        description=(
            f"{PRODUCT_CLAUSE}, reconciled against the accounting identity. "
            "Founded by Dominique Church in Ottawa."
        ),
        canonical="/about",
        ld=breadcrumb_ld([("Home", "/"), ("About", "/about")]) + aboutpage_ld(),
    )
