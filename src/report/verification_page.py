"""/verification -- the page to link in outreach: what the check is, what the
flags mean, what the 2026-10-09 study found, and calls anyone can run.

Three kinds of number sit on it, and each says which kind it is:

* the live counts, read from the database when the page renders and dated by
  `identity_breakdown()["computed_on"]` (one latest balance sheet per company);
* the frozen study counts (995 of 38,568, 971 of 995, ...), which are literals
  because the snapshot is dated 2026-10-09 17:12 UTC and does not move;
* the sample responses, which are real output captured from the live service on
  the date printed next to them. Nothing on the page is made up, and a sample
  that has not been captured is left out rather than written by hand
  (`samples.PAID`; see the [CONFIRM] comments in `_run_it`).

The study is /blog/which-sec-balance-sheets-fail-the-identity-check; its CSV and
README sit in static/research/balance-sheet-check-2026-10/. Where the two say
the same thing, the wording here follows that README.
"""

from __future__ import annotations

from html import escape

from src.report.company_page import asset_version
from src.report.home_page import fmt_int, plural, shell
from src.report.samples import CAPTURED as _CAPTURED
from src.report.samples import CHECK_AACB as _CHECK_AACB
from src.report.samples import CHECK_AAPL as _CHECK_AAPL
from src.report.samples import DEMO_AAPL as _DEMO_AAPL
from src.report.samples import PAID as _SAMPLES

# The sentence used wherever the balance-sheet check is quoted whole; pinned by
# tests/test_check_claims.py.
_CHECK_SENTENCE = (
    "Each filing is checked against Assets = Liabilities + Equity when it "
    "reports the lines needed; where it does not, it is marked not testable "
    "instead of passing."
)



def _code(text: str) -> str:
    return f'<pre class="code" tabindex="0"><code>{escape(text)}</code></pre>'


def _live_counts() -> str:
    """The four flag reasons with today's counts, or "" when unreadable."""
    try:
        from src.company.stats import identity_breakdown

        bd = identity_breakdown() or {}
    except Exception:  # noqa: BLE001 - a count must never take the page down
        bd = {}
    c = bd.get("counts") or {}
    if not bd or not c or not bd.get("computed_on"):
        return ""
    companies = int(bd.get("companies") or 0)
    return (
        '<p class="note">Counted on '
        f"{escape(str(bd['computed_on']))} (UTC) across the latest balance sheet "
        f"of each of {fmt_int(companies)} {plural(companies, 'company', 'companies')}: "
        f"{fmt_int(bd.get('checked', 0))} could be tested, "
        f"{fmt_int(bd['reconciled'])} reconcile and {fmt_int(bd['flagged'])} are "
        f"flagged, as {fmt_int(c.get('rounding', 0))} rounding, "
        f"{fmt_int(c.get('missing_tag', 0))} a line item that did not reach us, "
        f"{fmt_int(c.get('unexplained', 0))} with no stated total to test against, "
        f"and {fmt_int(c.get('broken', 0))} a filing that does not balance. "
        f"{fmt_int(bd.get('not_testable', 0))} report too little to test. "
        "These are companies, not filings, and they move as filings load; the "
        "study below counts filings on a frozen date.</p>"
    )


_HEAD = f"""
  <header class="mhead">
    <h1>How the balance check works, and what it found</h1>
    <p class="lede">{escape(_CHECK_SENTENCE)} Figures are as reported to the
      SEC and are never adjusted to make a sheet fit. This page sets out the
      test, the four reasons a filing is flagged, one dated study of what the
      test found, and calls you can run yourself.</p>
  </header>
"""

_CHECK = """
    <h2 id="check">The check</h2>
    <p>A balance sheet has to satisfy Assets = Liabilities + Equity. We test it
    on each filing's own figures: total assets must equal total liabilities
    plus total equity to within half of one percent of total assets. Equity is total equity
    including noncontrolling interests where the filer reports it.</p>
    <p>If the plain sum misses, we also try adding the lines that sit beside or
    between liabilities and equity: noncontrolling interest, temporary equity,
    redeemable preferred stock, redeemable noncontrolling interest and minority
    interest in an operating partnership. A filing that balances on one of
    those bases passes, and the response names the basis
    (<code>identity_basis</code>). A filer that publishes total assets and its
    own stated total of liabilities and equity, but no separate liabilities or
    equity line, is tested on that stated total.</p>
    <p>A filing that does not report the lines the test needs is returned as
    <code>not_testable</code>. That is neither a pass nor a failure, and the
    response names what is missing.</p>
    <h3>Where the figures come from</h3>
    <p>The SEC Financial Statement Data Sets, which are the SEC's quarterly bulk
    release of XBRL facts, and the SEC's XBRL frames API, which brings in
    filings as they are filed, weeks before the quarter's data set is
    published. A frames figure takes its filing date from EDGAR's quarterly
    form index, matched on the accession number, and a figure whose filing
    cannot be found there is dropped rather than dated by guesswork. Where both
    routes supply the same figure, the bulk row is kept.
    Only consolidated values are read: rows for a segment, a geography, an
    equity component or a coregistrant are ignored. Every figure is as reported,
    and each line item carries the period it is for and the date of the filing
    that first reported that value.</p>
    <h3 id="restated">What the <code>restated</code> flag means</h3>
    <p>A line item's <code>restated</code> flag is true only for a figure taken from a
    vendor's restated series (FMP or Yahoo) rather than as reported to the SEC. A
    revision in a later SEC filing is not marked this way: it appears as that
    filing's value and date, and in <code>/api/company/{ticker}/changes</code>.</p>
"""

_FLAGS = """
    <h2 id="flags">What the flags mean</h2>
    <p>A flag is a reason. It is a statement about the arithmetic on figures we
    hold, not a statement about the filer. There are four.</p>
    <div class="tscroll"><table class="mtable">
      <thead><tr><th>Flag</th><th>What it means</th></tr></thead>
      <tbody>
        <tr><td><code>rounding</code></td>
          <td>The components we hold miss total assets by half of one percent to one percent.</td></tr>
        <tr><td><code>missing_tag</code></td>
          <td>A line item did not reach us. The gap is one percent of total assets or
            more, and the filer's own stated total of liabilities and equity
            agrees with its total assets. The filing agrees with itself and our
            components fell short. It does not say why the line did not
            reach us.</td></tr>
        <tr><td><code>unexplained</code></td>
          <td>The gap is one percent of total assets or more and we hold no stated total
            to test against, so the check cannot say whose figures fall
            short.</td></tr>
        <tr><td><code>broken</code></td>
          <td>A filing that does not balance: the filer's own stated total
            disagrees with its own total assets by more than half of one percent.</td></tr>
      </tbody>
    </table></div>
    <p>In the exceptions feed (Pro and above) each failed check also carries an
    <code>attribution</code>: <code>filer</code>, <code>extraction</code> (a gap
    on our side) or <code>unknown</code>. The full definition and examples are on
    <a href="/methodology">how we check the numbers</a>.</p>
"""

_STUDY = """
    <h2 id="study">The 971-of-995 finding</h2>
    <p>On 2026-10-09 at 17:12 UTC we took a frozen copy of our database and ran
    the check on every filing in it that could be tested. The numbers below are
    from that copy and do not move. Each one is quoted from
    <a href="/blog/which-sec-balance-sheets-fail-the-identity-check">the
    published report</a> (2026-10-09), and the 995 failures, their labels and
    their tickers can be recounted from its CSV. The live site will differ, because its data
    and the code that reads filings both change.</p>
    <ul>
      <li><strong>995 of 38,568 filings</strong> failed the check, from 286
        tickers. Another 728 could not be tested and are in neither count.
        The tested filings have period ends from 2024-06-30 to 2026-09-05, and
        the 995 failures from 2024-08-31 to 2026-08-31. The unit is one filing
        per company and period, so this counts filings, not companies.</li>
      <li><strong>971 of the 995</strong> carry the <code>missing_tag</code>
        label: the filer's own stated total agrees with its total assets and a
        credit-side line did not reach our sum. 14 are <code>rounding</code>
        and 10 are <code>unexplained</code>. None is <code>broken</code>.</li>
      <li><strong>20 of 20</strong> failures we read against the filer's own
        XBRL on EDGAR balance on the filer's figures. The sample is 20
        failures from 18 issuers, drawn at random with a fixed seed. It does
        not speak for every one of the 995, and 971 is our label, not 971
        line-by-line checks.</li>
      <li><strong>433 of the 995</strong> are blank-check companies by the SIC
        description in our sector map, and most of the rise in the failure rate
        from 2024 to 2026 comes from that group.</li>
    </ul>
    <p>A failure of our check is not a finding that a filing is wrong. It means
    the figures we hold for it did not satisfy the identity. The method, the
    charts and three filings in detail are in
    <a href="/blog/which-sec-balance-sheets-fail-the-identity-check">the
    report</a>, and the 995 filings are in
    <a href="/static/research/balance-sheet-check-2026-10/flagged-filings-2026-10-09.csv">a
    CSV</a> with a link to each filing on EDGAR where we found one.</p>
    <p>This count and the live one above measure different things. The live
    count is the latest balance sheet of each company today. The study is every
    filing in a two-year window on one date. Neither includes the filings that
    cannot be tested.</p>
"""


def _run_it() -> str:
    paid_note = (
        "Calls with <code>as_of</code> need the Pro plan or above, and the "
        "changes endpoint needs Starter or above. A free key does not call "
        "them, and they return an error that names the plan that does."
    )
    as_of_out = _SAMPLES.get("as_of", "")
    restate_out = _SAMPLES.get("restatement", "")
    # [CONFIRM: paste a real as_of response here. Capture it with a Pro key
    #  from the live API, put the text in _SAMPLES["as_of"], and re-run
    #  tests/test_verification_page.py.]
    # [CONFIRM: paste a real restatement response here (the figure as first
    #  reported and as revised). Capture it with a Starter or above key from
    #  GET /api/company/{ticker}/changes and put it in _SAMPLES["restatement"].]
    as_of_block = _code(as_of_out) if as_of_out else ""
    restate_block = _code(restate_out) if restate_out else ""
    return f"""
    <h2 id="run">Run it</h2>
    <p>Responses below were captured from the live service on {_CAPTURED}.
    Some fields are left out and none is edited. Live responses change when a
    new filing arrives.</p>

    <h3>1. A balance sheet, no key</h3>
    {_code("curl https://balanceproof.dev/api/demo/AAPL")}
    <p>The demo route is rate-limited per address. With a free key the same
    data is at <code>/api/company/AAPL</code>, sent with an
    <code>X-API-Key</code> header. Excerpt of the response:</p>
    {_code(_DEMO_AAPL)}

    <h3>2. The check, on a filing that balances</h3>
    <p>Over MCP, the <code>check_balance_sheet</code> tool; in Python,
    <code>client.verify(["AAPL"])</code> for several at once on Pro. Excerpt:</p>
    {_code(_CHECK_AAPL)}

    <h3>3. The check, on a filing that is flagged</h3>
    <p>The filer's own stated total balances, and the components we read do not.
    The response says so instead of adjusting anything:</p>
    {_code(_CHECK_AACB)}

    <h3>4. A point-in-time figure with <code>as_of</code></h3>
    <p>The balance sheet exactly as it was public on a date, using only filings
    made by then. For a backtest this is the original figure before a
    restatement and not the revision after it. Pro and above.</p>
    {_code('curl.exe -H "X-API-Key: YOUR_KEY" "https://balanceproof.dev/api/company/AAPL?as_of=2025-03-01"')}
    {as_of_block}

    <h3>5. A restatement</h3>
    <p>Figures are as reported to the SEC. When a later filing revises a figure,
    the default view shows the revised value under the later filing's date;
    <code>as_of</code> returns the value known on the date you ask. The changes
    endpoint lists each revised figure with the original value, the revised one
    and the filings that said each. Starter and above. The last 90 days of
    restatements are public at <a href="/restatements">/restatements</a>.</p>
    {_code('curl.exe -H "X-API-Key: YOUR_KEY" https://balanceproof.dev/api/company/AAPL/changes')}
    {restate_block}
    <p class="note">{paid_note}</p>
"""


_TRY = """
    <h2 id="try">Try it</h2>
    <p>The free tier is 1,000 calls a month, with no card. <a href="/dashboard">Get a
    free API key</a> and check a company you already know the answer for against
    EDGAR. The full reference is at <a href="/api">the API docs</a>, the
    definitions are on <a href="/methodology">how we check the numbers</a>, and
    prices are on <a href="/pricing">the pricing page</a>.</p>
"""


def render_verification(*, nav: str = "") -> str:
    from src.report.nav import SUPPORT_EMAIL, render_footer
    from src.report.schema import breadcrumb_ld, webpage_ld

    footer = render_footer(
        "Found a figure you think is wrong? "
        f'<a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.'
    )
    body = (
        f'{nav}\n<main class="wrap post" id="main">\n{_HEAD}'
        f'  <article class="prose">{_CHECK}{_FLAGS}{_live_counts()}{_STUDY}'
        f"{_run_it()}{_TRY}  </article>\n{footer}\n</main>\n"
        f'<script src="/static/nav.js?v={asset_version()}" defer></script>'
    )
    description = (
        "How the balance check works, what the four flags mean, the 971-of-995 "
        "finding with its date, and calls you can run with as_of."
    )
    return shell(
        "Verification: How We Check SEC Balance Sheets — BalanceProof",
        body,
        description=description,
        canonical="/verification",
        ld=breadcrumb_ld([("Home", "/"), ("Verification", "/verification")])
        + webpage_ld("How the balance check works, and what it found",
                     description, "/verification"),
    )
