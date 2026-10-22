"""/security and /changelog: the two pages a company's reviewer looks for
before an API goes into their pipeline (2026-10-21).

/security says how the service handles keys, accounts, payments and data,
in the same words as the Privacy Policy and no stronger. Every line is a
fact about the code as it stands: keys are stored as a SHA-256 hash
(`accounts.hash_api_key`), passwords with bcrypt (`auth.hash_password`),
webhooks are signed with HMAC-SHA256 (`webhooks`), card details go to Stripe
and never here. It names the certifications we do not hold, because a
reviewer will ask and a page that leaves it out reads as hiding it.

/changelog lists what changed for someone using the API, newest first. It is
written by hand from the push log, not generated from commit messages:
commit messages are for us, and half of them are internal.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell

_SECURITY = """
  <article>
    <h2>The data</h2>
    <p>Every figure the API serves comes from public SEC filings: the SEC's
      Financial Statement Data Sets and its XBRL frames API. The dataset holds
      no customer data, and nothing you send us is mixed into it.</p>

    <h2>Your account</h2>
    <ul>
      <li><strong>API keys are stored as a SHA-256 hash.</strong> The full key
        is shown once, when it is issued. After that we hold the hash and the
        first eight characters, enough to tell you which key an account is on
        and not enough to use it. Regenerating a key stops the old one
        immediately.</li>
      <li><strong>No password is required.</strong> Sign-in is by a link sent
        to your email. If you set a password, it is hashed with bcrypt and we
        cannot read it back.</li>
      <li><strong>Sessions</strong> are one signed cookie: HttpOnly, Secure,
        SameSite=Lax.</li>
      <li><strong>Deletion is self-serve.</strong> Deleting an account from the
        dashboard removes it with its key, usage logs, watchlist and webhooks,
        and cancels any subscription.</li>
    </ul>

    <h2>Payments</h2>
    <p>Paying takes you to Stripe's own checkout page. Card details are given
      to Stripe and never pass through this service or reach its database.</p>

    <h2>Webhooks</h2>
    <p>Alerts sent to your own URL are signed: each request carries a
      <code>BalanceProof-Signature</code> header with a timestamp and an
      HMAC-SHA256 of the timestamp and the body, under a secret only you and
      we hold. Check it before you trust the payload; the
      <a href="/api">API reference</a> shows how.</p>

    <h2>Where it runs, and who else sees what</h2>
    <ul>
      <li><strong>Railway</strong> hosts the application and its database.
        Every page and API call is served over HTTPS.</li>
      <li><strong>Stripe</strong> handles payments, only if you buy
        something.</li>
      <li><strong>Email delivery</strong> (Resend or AgentMail) receives your
        address when we send a sign-in link or an account email.</li>
      <li><strong>OpenRouter</strong> receives a question only if you use the
        optional question box on a company page.</li>
    </ul>
    <p>No third-party analytics, no advertising identifiers, no cross-site
      tracking. IP addresses are not stored; rate limiting uses a salted
      digest that cannot be turned back into an address. The full list is in
      the <a href="/privacy">Privacy Policy</a>.</p>

    <h2>Certifications</h2>
    <p>We do not hold SOC 2 or ISO 27001 certification. If your review needs
      something this page does not answer, ask us and we will tell you what
      we can.</p>

    <h2>Reporting a problem</h2>
    <p>If you find a security issue, email
      <a href="mailto:{support}">{support}</a> before sharing it anywhere else.
      We read every report.</p>
    <p>No system is perfectly secure, and we do not claim otherwise.</p>
  </article>
"""

# Newest first. (date, [lines]). Customer-visible changes only.
CHANGELOG: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Dated for the commit; move it to the day it deploys if that is later.
    ("2026-10-21", (
        "The site has one look: every page is on the same light theme as the "
        "home page, and company pages lead with the drawing.",
        "The home page opens with a layered scene that plays as you scroll, "
        "on phones too; it is a still image with reduced motion, and a "
        "footer switch turns motion on or off.",
        "A <a href=\"/verification\">verification page</a>: how the balance "
        "check works, and what it found, with counts by reason.",
        "This page, and a <a href=\"/security\">security page</a>.",
        "The company directory has a filter box.",
    )),
    ("2026-10-17", (
        "A filing that does not report the lines a check needs is now "
        "<code>not_testable</code>, never a pass. <code>/api/verify</code> "
        "returns <code>reconciles: null</code> with <code>identity_status</code> "
        "for those, and watchlist alerts say Not testable.",
        "When both SEC routes loaded the same filing, the month-end copy from "
        "the bulk data sets is the one served.",
        "Revenue reported under <code>RevenueFromContractWithCustomerIncludingAssessedTax</code> "
        "is now read, and EPS from the bulk files is read in the unit the SEC uses.",
        "The dataset CSV never pairs a full-year figure for a flow metric with "
        "a quarter row.",
        "MCP: asking for more history than your plan covers is a clear refusal "
        "rather than a shorter answer, and actionable failures are reported as "
        "errors.",
    )),
    ("2026-10-07", (
        "Restatements compare a figure only with the same XBRL tag in the "
        "earlier filing, so a change of tag is no longer reported as a "
        "restatement.",
    )),
    ("2026-09-30", (
        "Replies to account email go to support@balanceproof.dev.",
    )),
    ("2026-09-26", (
        "Only periodic reports (10-K and 10-Q) are read from the SEC data sets.",
        "A public <a href=\"/restatements\">restatements page</a> lists recent "
        "revisions.",
    )),
    ("2026-09-24", (
        "<code>/api/company/{ticker}/statements</code>: income statement and "
        "cash flow, each period checked when the filing reports the lines.",
        "Point-in-time <code>as_of</code> on <code>/api/company</code> and "
        "<code>/history</code>: the value as it was known on a past date.",
        "<code>/api/exceptions</code>: a feed of failed checks and "
        "restatements, with CSV.",
        "The Python client is on PyPI: <code>pip install balanceproof</code>.",
        "MCP tools <code>get_financial_statements</code> and "
        "<code>get_exceptions</code>.",
        "Every plan feature is usable from the dashboard; accounts can be "
        "deleted and subscriptions cancelled there.",
    )),
    ("2026-09-23", (
        "Paid plans: longer balance sheet history, what changed between "
        "filings, a watchlist with email alerts when a followed company files, "
        "checking many companies in one request, and signed webhooks. "
        "<a href=\"/pricing\">Which plan has what</a>.",
    )),
)


def _changelog_html() -> str:
    out = []
    for date, lines in CHANGELOG:
        items = "".join(f"<li>{line}</li>" for line in lines)
        out.append(f'<h2 id="d{escape(date)}"><time datetime="{escape(date)}">'
                   f"{escape(date)}</time></h2>\n    <ul>{items}</ul>")
    return "<article>\n    " + "\n    ".join(out) + "\n  </article>"


def _page(title: str, h1: str, lede: str, article: str, nav: str, *, description: str,
          canonical: str) -> str:
    from src.report.nav import SUPPORT_EMAIL, render_footer

    footer = render_footer(
        f'Questions: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.')
    body = (
        f'{nav}\n<main class="wrap post" id="main">\n'
        f'  <header class="mhead">\n    <h1>{h1}</h1>\n'
        f'    <p class="lede">{lede}</p>\n  </header>\n'
        f"{article}\n{footer}\n</main>\n"
        f'<script src="/static/nav.js?v={asset_version()}" defer></script>'
    )
    return shell(title, body, description=description, canonical=canonical)


def render_security(*, nav: str = "") -> str:
    from src.report.nav import SUPPORT_EMAIL

    return _page(
        "Security — BalanceProof",
        "Security",
        "How keys, accounts, payments and data are handled, stated exactly.",
        _SECURITY.replace("{support}", SUPPORT_EMAIL),
        nav,
        description="How BalanceProof stores API keys, handles accounts and "
                    "payments, signs webhooks, and which providers see what.",
        canonical="/security",
    )


def render_changelog(*, nav: str = "") -> str:
    return _page(
        "Changelog — BalanceProof",
        "Changelog",
        "What changed for people using the API, newest first.",
        _changelog_html(),
        nav,
        description="Changes to the BalanceProof API, MCP server, Python "
                    "client and data, newest first.",
        canonical="/changelog",
    )
