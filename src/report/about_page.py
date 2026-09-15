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
    <p>BalanceProof is built solo by Dominique Church, 17, in Ottawa. It
      started as a tool for a personal project and became a public API because
      every free SEC data source I tried disagreed with the filing
      somewhere.</p>

    <h2>The method</h2>
    <p>Every balance sheet is tested against the accounting identity before it
      is stored. A filing that reconciles is published with its figures. A
      filing that does not is published with the reason — noncontrolling
      interests, mezzanine equity, rounding, or a genuinely broken filing —
      rather than quietly adjusted until it balances. The counts are public and
      the categories are named: <a href="/methodology">how we verify every
      number</a>.</p>

    <h2>What it does not do</h2>
    <p>No predictions. No scores. No recommendations. Nothing is estimated,
      smoothed or restated. Where a filer does not report a component, the page
      says so rather than showing a zero.</p>

    <h2>Using it</h2>
    <p>Looking companies up on this site is free and needs no account. The
      machine-readable version — the REST API and the bulk CSV — is what costs
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
    from src.report.schema import aboutpage_ld, breadcrumb_ld

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
        description=(
            "BalanceProof is a financial data API that reconciles SEC EDGAR "
            "balance sheets against the accounting identity. Built solo by "
            "Dominique Church in Ottawa."
        ),
        canonical="/about",
        ld=breadcrumb_ld([("Home", "/"), ("About", "/about")]) + aboutpage_ld(),
    )
