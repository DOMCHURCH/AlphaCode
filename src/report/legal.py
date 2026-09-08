"""Terms of Service and Privacy Policy.

Written against what the code actually does, which in four places is not what a
generic template would say. Each of those is a deliberate correction rather than
an omission, because a privacy policy that describes data flows a service does
not have is not merely useless -- it is a false statement to its users about
where their data goes:

* **Stripe takes the card; this service never sees it.** Payment goes through
  Stripe Checkout, which is Stripe's own page on Stripe's own domain. No card
  number, expiry or CVC is ever posted to this service and none is stored in
  its database -- what comes back is an event saying a payment settled, and an
  email address. So the policy names Stripe as a processor AND still says no
  payment information is collected here, because both are true and the second
  is the one people are actually asking about.
* **IP addresses are never stored.** What is kept is a salted digest
  (`analytics.hash_ip`), which is a stronger and truer claim than the usual
  "we collect IP addresses".
* **API keys are NOT encrypted at rest.** They are stored as issued, on
  purpose (see `models.ApiUser`). Claiming encryption there would be the exact
  kind of security assertion that matters when it turns out to be untrue, so
  the policy states the real position and tells people to treat the key as a
  password.
* **The optional question box sends text to OpenRouter.** A third party that a
  template would not know about, and the only one that receives anything a user
  typed.

These are documents, not advice. They are written to be accurate and readable;
whether they are sufficient for a given jurisdiction is a question for somebody
who is a lawyer.
"""

from __future__ import annotations

from src.report.company_page import asset_version
from src.report.home_page import shell

LAST_UPDATED = "8 September 2026"
# The same day in the form a machine wants it. Written next to the prose
# date rather than derived from it, so the two cannot drift: the sitemap
# tells crawlers when these documents last changed, and a lastmod that
# disagrees with the page is a claim the crawler can catch you making.
LAST_UPDATED_ISO = "2026-09-08"

# One sentence, used in three places, so the disclaimer cannot drift between
# the banner, the terms and the footer.
SHORT_DISCLAIMER = (
    "Data is for informational purposes only. Not financial advice."
)


def _legal_shell(title: str, nav: str, body: str, path: str = "/") -> str:
    from src.report.nav import render_footer

    _footer = render_footer()
    return shell(
        f"To Scale — {title}",
        f"""{nav}
<main class="wrap legal" id="main">
  <header class="hero">
    <h1 class="htitle">{title}</h1>
    <p class="hlede">Last updated {LAST_UPDATED}.</p>
  </header>
  {body}
{_footer}
</main>

<script src="/static/nav.js?v={asset_version()}" defer></script>""",
        # "quiet": the still image only, under the heaviest veil on the site.
        # These are the two pages somebody reads a thousand words of, and a
        # moving picture behind a sentence about liability is the one place
        # where atmosphere costs more than it is worth. A legal page that
        # looked like a different site would be the wrong page to make look
        # untrusted, so the backdrop stays -- it is simply turned right down.
        film="quiet",
        path=path,
        description=(
            f"{title} for To Scale — the SEC financial data API."
        ),
    )


def render_terms(*, nav: str = "", contact: str = "") -> str:
    who = contact or "the site owner"
    body = f"""
  <section class="sec doc">
    <h2>1. What this is</h2>
    <p>To Scale draws the balance sheets that US public companies filed with the
      SEC, and serves the same figures through an API. The underlying filings
      are public records published by the SEC. This service is the drawing and
      the plumbing around them.</p>

    <h2>2. Not financial advice</h2>
    <p class="callout"><strong>Not financial advice. No predictions. No
      guarantees.</strong> The visualisations and data are for educational and
      informational purposes only. Do not make investment decisions based solely
      on this data. Use at your own risk.</p>
    <p>Nothing here is a recommendation to buy, sell or hold anything. This
      service produces no scores, no ratings and no forecasts, and it is not a
      broker, dealer, or investment adviser.</p>

    <h2>3. The search and the drawings</h2>
    <p>The search and the visualiser are provided <strong>as-is</strong>, for
      information. We do not guarantee the accuracy, completeness or timeliness
      of anything displayed. Figures are extracted automatically from SEC
      filings and can be wrong, stale, or missing — a company can file late,
      restate, or tag a number in a way the extraction reads differently than
      you would.</p>
    <p>Always check the filing itself before relying on a number. Every company
      page names the source it was built from.</p>

    <h2>4. Accounts and API keys</h2>
    <p>You need an account only to use the API. Your API key identifies you and
      counts against your allowance; treat it as you would a password. You are
      responsible for what is done with your key. If it leaks, regenerate it
      from your dashboard — the old one stops working immediately.</p>
    <p>You must be at least 18 years old, or have the consent of a parent or
      guardian, to use this service.</p>

    <h2>5. Acceptable use</h2>
    <p>Do not:</p>
    <ul>
      <li>exceed or attempt to evade your API allowance, including by
        registering multiple accounts to multiply a free tier;</li>
      <li>scrape the site, or use the API in a way that degrades it for
        others;</li>
      <li>use the service for anything illegal, or to infringe anyone's
        rights;</li>
      <li>resell or redistribute the API as a competing service. Using the data
        inside your own product or research is fine — the SEC filings are
        public.</li>
    </ul>

    <h2>6. Paid plans and refunds</h2>
    <p>Pro is a monthly allowance. The full-dataset download is a one-time
      purchase of a file. Payment is taken by
      <a href="https://stripe.com" rel="noopener">Stripe</a> on Stripe's own
      checkout page. <strong>No card details are entered on this site or held
      by us</strong> — we receive confirmation that a payment succeeded and the
      email address it was made with, and nothing else.</p>
    <p><strong>Digital products are non-refundable</strong> once access has been
      granted or the dataset has been downloaded. If something goes wrong on our
      side, contact {who} and we will make it right.</p>
    <p>Pro renews monthly through Stripe until it is cancelled. To cancel,
      email {who} — self-serve cancellation is not built yet. Access continues
      to the end of the period already paid for; when it lapses your account
      returns to the free allowance — your key keeps working, nothing is
      deleted. A one-time dataset purchase does not renew.</p>

    <h2>7. Termination</h2>
    <p>We may suspend or revoke access if these terms are broken, or if an
      account is being used in a way that threatens the service. Where it is
      practical to do so first, we will tell you why.</p>
    <p>You can stop using the service at any time, and ask for your account to
      be deleted (see the Privacy Policy).</p>

    <h2>8. Intellectual property</h2>
    <p>The SEC filings and the figures in them are public records, free of
      royalty, and are not ours. The site, the drawings, the extraction code and
      the API are ours. You may use the data you obtain through the service —
      including commercially — subject to section 5.</p>

    <h2>9. Liability</h2>
    <p>The service is provided as-is, without warranties of any kind. To the
      fullest extent the law allows, our total liability to you for any claim
      arising out of the service is limited to <strong>the amount you have paid
      us in the twelve months before the claim</strong>. For a free account that
      amount is zero.</p>
    <p>We are not liable for indirect or consequential losses, including trading
      losses, lost profits, or decisions taken on the basis of anything shown
      here.</p>

    <h2>10. Indemnification</h2>
    <p>You agree to indemnify us against claims arising from your use of the
      service or your breach of these terms.</p>

    <h2>11. Changes</h2>
    <p>We may change these terms. Material changes will be announced by email to
      account holders, or by a notice on the site, before they take effect.
      Continuing to use the service after that means you accept the new
      terms.</p>

    <h2>12. Governing law</h2>
    <p>These terms are governed by the laws of the Province of Ontario and the
      federal laws of Canada that apply there. Disputes go to the courts of
      Ontario.</p>

    <h2>13. Contact</h2>
    <p>Questions about these terms: {who}.</p>
  </section>"""
    return _legal_shell("Terms of Service", nav, body, path="/terms")


def render_privacy(*, nav: str = "", contact: str = "") -> str:
    who = contact or "the site owner"
    body = f"""
  <section class="sec doc">
    <p class="callout">The short version: an email address if you want an API
      key, a count of the calls you make, and no card details at all — those go
      to Stripe and never to us. We do not sell anything to anyone.</p>

    <h2>1. What we collect</h2>
    <ul>
      <li><strong>Email address</strong> — only if you create an account. It is
        the account's identity and where login links are sent.</li>
      <li><strong>API key</strong> — generated by us, used to authenticate your
        API calls.</li>
      <li><strong>Password</strong> — only if you choose to set one. Stored as a
        bcrypt hash; we never see or store the password itself.</li>
      <li><strong>Usage logs</strong> — which endpoint you called and when, so
        your monthly allowance can be counted and shown to you.</li>
      <li><strong>Page views and searches</strong> — the path visited and, on a
        company page, the ticker. Recorded against a salted digest of your
        address, not against your account.</li>
      <li><strong>A salted digest of your IP address</strong> — for rate
        limiting and abuse control. <strong>We do not store IP addresses.</strong>
        The digest is enough to tell two visitors apart for a day; it is not
        reversible into an address, and it is not a person — one office shares
        an address, and one phone moving from wifi to cellular produces two.</li>
    </ul>

    <h2>2. What we do not collect</h2>
    <p><strong>No payment information, of any kind.</strong> There is no card
      form on this site. Paying takes you to
      <a href="https://stripe.com" rel="noopener">Stripe</a>'s own checkout
      page, where your card details are given to Stripe — they are never posted
      to this service, never pass through it, and are not in its database. What
      comes back to us is that a payment succeeded, the email address it was
      made with, and Stripe's own identifiers for the customer and the
      subscription, which is what lets your access be granted and later
      cancelled.</p>
    <p>No tracking cookies, no advertising identifiers, no third-party
      analytics, and no cross-site tracking of any kind.</p>

    <h2>3. Why we collect it</h2>
    <ul>
      <li>To run the service and let you make API calls.</li>
      <li>To count usage against your allowance, and to show you that count.</li>
      <li>To send you login links and account email.</li>
      <li>To rate-limit and to stop abuse.</li>
      <li>To understand which companies are looked at, so the site can be
        improved. This is aggregate interest in tickers, not a profile of
        you.</li>
    </ul>

    <h2>4. Who else sees it</h2>
    <ul>
      <li><strong>Railway</strong> — hosting. The application and its database
        run there.</li>
      <li><strong>AgentMail</strong> — email delivery. Receives your address and
        the message when we send you a login link or an account email.</li>
      <li><strong>Stripe</strong> — payments, and only if you buy something.
        Your card details go to Stripe and not to us; Stripe receives your
        email address and holds the payment record. Stripe is the data
        controller for what it collects on its own page, under its
        <a href="https://stripe.com/privacy" rel="noopener">privacy policy</a>.
        If you never buy anything, nothing is sent to Stripe.</li>
      <li><strong>OpenRouter</strong> — only if you use the optional question
        box on a company page. Your question and the filed figures for that
        company are sent to a language model to answer it. Do not type anything
        private into that box. If you never use it, nothing is sent.</li>
    </ul>
    <p><strong>We do not sell your data, and we do not share it for
      advertising.</strong> We will disclose data if the law requires it.</p>

    <h2>5. Security, stated honestly</h2>
    <ul>
      <li>The site is served over HTTPS.</li>
      <li>Passwords are hashed with bcrypt and are not recoverable by us.</li>
      <li>Session cookies are signed, HttpOnly and Secure.</li>
      <li><strong>API keys are stored as issued, not encrypted or hashed.</strong>
        That is a deliberate trade: the key is read-only over data that is
        already public, and a recoverable key means a lost key is a lookup
        rather than a re-registration. It also means you should treat your key
        as a password, and that anyone with access to our database could read
        it. If you would rather it were not sitting there, you can regenerate
        it at any time, and access is revocable.</li>
    </ul>
    <p>No system is perfectly secure and we do not claim otherwise.</p>

    <h2>6. How long we keep it</h2>
    <ul>
      <li>Account data — for as long as the account exists, or until you ask us
        to delete it.</li>
      <li>Usage logs — kept so your allowance can be counted and audited.</li>
      <li>Page views and digests — retained in aggregate for analytics. They are
        not linked to your account.</li>
      <li>Login links — deleted shortly after they expire.</li>
    </ul>

    <h2>7. Your rights</h2>
    <p>Under PIPEDA and comparable law you may ask for access to the personal
      information we hold about you, its correction, or its deletion.</p>
    <ul>
      <li><strong>Access</strong> — your account, tier, usage and key are all on
        your dashboard. Ask {who} for anything beyond that.</li>
      <li><strong>Correction</strong> — email {who} to change the address on an
        account.</li>
      <li><strong>Deletion</strong> — email {who} and we will delete the
        account, its key and its usage logs. Aggregate figures that are not
        linked to you may remain.</li>
    </ul>
    <p>If you are unhappy with how we have handled a request, you can complain
      to the Office of the Privacy Commissioner of Canada.</p>

    <h2>8. Cookies</h2>
    <p>One cookie, set only when you sign in, holding a signed session. It is
      how the dashboard knows who you are. There are no tracking or advertising
      cookies. Your browser also stores your API key locally if you paste one
      in; that never leaves your device except as the header on your own API
      calls.</p>

    <h2>9. Where the data lives</h2>
    <p>The service is operated from Ontario, Canada, and hosted on
      infrastructure that may process data outside Canada. Using the service
      means accepting that.</p>

    <h2>10. Changes</h2>
    <p>Material changes will be announced by email to account holders, or by a
      notice on the site.</p>

    <h2>11. Contact</h2>
    <p>Privacy questions, access requests and deletion requests: {who}.</p>
  </section>"""
    return _legal_shell("Privacy Policy", nav, body, path="/privacy")
