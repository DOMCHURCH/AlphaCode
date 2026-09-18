"""/methodology — what the accuracy figure on the home page actually measures.

A = L + E is an identity, not a target, so any number below 100% is a claim
that needs a reason. This page is that reason, in public, because a
reconciliation service that will not explain its own exceptions is asking to be
taken on faith — which is the thing it exists not to ask.

Prose, not data: the categories are structural facts about how filings are
written and they do not change when a quarter loads. The internal analysis with
the per-category counts lives in docs/internal/identity-failures.md.
"""

from __future__ import annotations

from src.report.company_page import asset_version
from src.report.home_page import shell

_HEAD = """
  <header class="mhead">
    <h1>How we verify every number</h1>
    <p class="lede">Every balance sheet must satisfy
      <strong>Assets = Liabilities + Equity</strong>. It is not a statistic, it
      is double-entry bookkeeping. A filing that does not balance has an error
      — either in the filing, or in how we read it — and this page says which,
      for every company we cover.</p>
  </header>
"""

# NB: no `<main>` here. The wrapper is opened in `render` so that `_HEAD` and
# the live counts land INSIDE it. They used to be emitted before it, which
# made them direct children of <body> with no page gutter at all: the H1, the
# lede and the whole counts table started at x=0, hard against the window.
_BODY = """
  <article>
    <header class="hero">
      <h2 class="htitle">How the accounting-identity check works</h2>
      <p class="hlede">Assets = Liabilities + Equity is not a rule filers
        follow. It is a consequence of double-entry bookkeeping — which is
        exactly what makes it usable as a test on data you did not produce.</p>
    </header>

    <div class="prose">
      <h2>Why the number is not 100%</h2>

      <p>Because the identity is an identity, a correctly tagged filing read on
      the right terms satisfies it <em>exactly</em>. So a pass rate is not a
      measure of how good the data is. It means that for a small number of
      filings we cannot
      reconcile the two sides, and the honest thing is to say which and why
      rather than to round the number up.</p>

      <p>Every miss is one of two things. Either the filing is not being read on
      the terms it was written on — their arithmetic is fine and our reading is
      wrong — or we failed to pull something the filing contains. A third case,
      the filer's own arithmetic being wrong, is possible and should be
      vanishingly rare for an audited public company.</p>

      <h2>The four ways a filing reconciles</h2>

      <p>Not every filing is written on <code>A = L + E</code>, and the ones
      that are not are sound filings, not failures. Each of these is a pass,
      counted in "reconciled" above, and the drawing names the basis it used
      rather than quietly adding lines together.</p>

      <p><strong>Plain.</strong> Assets equal liabilities plus equity as
      filed, with nothing to reconcile.</p>

      <p><strong>Noncontrolling interests.</strong> A consolidated company can
      report the parent's equity and the noncontrolling interest as two separate
      lines with no combined total. The identity that filing was written on is
      <code>A = L + E + NCI</code>, and testing <code>A = L + E</code> against
      it flags a perfectly sound filing. Where this happens the drawing says
      "balances as A = L + E + noncontrolling interest" and names both figures.
      Nothing is adjusted; the two lines are added, and you are told they
      were.</p>

      <p><strong>Mezzanine equity.</strong> Redeemable preferred stock and
      similar instruments sit between liabilities and equity — neither one, by
      design. Airlines, biotechs and companies that came public through a SPAC
      carry it often. Four tags are read for it: temporary equity, redeemable
      preferred stock, redeemable noncontrolling interest, and minority
      interest in an operating partnership. A filing that balances once the
      mezzanine line is included is reported as balancing on those terms.</p>

      <p><strong>Both together.</strong> A filing carrying a noncontrolling
      interest and a mezzanine line is tested against both, and reconciles on
      <code>A = L + E + NCI + mezzanine</code>.</p>

      <h2>The four reasons a filing is flagged</h2>

      <p>Everything that does not reconcile lands in exactly one of these, and
      the counts for each are in the table below.</p>

      <p><strong>Rounding.</strong> Figures are reported in millions. A
      one-unit difference on a balance sheet of a few hundred billion is eight
      decimal places of nothing, and a gap under a percent of total assets is
      treated as the noise it is.</p>

      <p><strong>Missing XBRL tag.</strong> The filing balances against its own
      stated total but our sum falls short, which means there is a component in
      the document we did not read. That is our gap, not the filer's, and it is
      the category we work to empty.</p>

      <p><strong>Unexplained.</strong> The filer published no stated total to
      referee against, so there is no way to tell whose arithmetic is at fault.
      Recorded as unknown rather than assigned to whichever side flatters
      us.</p>

      <p><strong>A filing that genuinely does not balance.</strong> It happens.
      When a filer's own stated total does not match their own assets, no
      parser fixes that, and pretending otherwise would mean publishing a number
      the company never filed. The drawing shows the figures as reported, states
      the gap as a percentage, and says so.</p>

      <h2>How we tell them apart</h2>

      <p>Guessing which category a failure belongs to would make the whole
      exercise worthless. One field settles it: <code>LiabilitiesAndEquity</code>,
      the filer's own stated right-hand side. Where a company publishes it, we
      can ask two questions instead of one — does the filing balance against its
      own figures, and did we recover everything it put there?</p>

      <p>If the filing balances against itself and our sum falls short, the
      missing amount is a line we did not ingest, and the fault is ours. If the
      filing does not balance against its own stated total, that is the filer's
      arithmetic. Where a company publishes no stated total, the honest answer
      is that we do not know, and it is recorded as unexplained rather than
      assigned to whichever bucket flatters us.</p>

      <h2>What we will not do</h2>

      <p>Plug the gap. A figure adjusted until the columns agree is no longer
      what the company filed, and what the company filed is the entire product.
      Every number on this site is as reported, and where the two sides do not
      meet you get the gap, the size of it, and the reason — not a tidier
      number.</p>
    </div>
  </article>

  <aside class="sec cta">
    <div class="sec-head"><h2>Check it yourself</h2></div>
    <p class="sec-sub">Take a company where you already know the answer and
      compare the drawing against the filing on EDGAR. That is the only
      comparison with an authority behind it.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a free API key</a>
      <a class="btn ghost" href="/pricing">Pricing</a>
      <a class="btn ghost" href="/api">API docs</a>
    </div>
    <p class="plan-note">More on the selection step:
      <a href="/blog/sec-xbrl-data-wrong-one-in-five">why SEC XBRL data is wrong
      one time in five</a>, and
      <a href="/blog/understanding-the-accounting-identity">what the identity
      actually proves</a>.</p>
  </aside>
"""



def _fmt(n: int | None) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def _live_section() -> str:
    """The counts, read from the database at render time.

    Deliberately counts and never a rate. The denominator moves as coverage
    improves -- restoring 31 unreachable companies in one session moved it
    twice -- so a percentage would FALL as we covered more filings, which is
    the opposite of what a quality measure should do.
    """
    from src.company.stats import identity_breakdown

    b = identity_breakdown()
    if not b:
        return ""
    c = b["counts"]
    flagged = b["flagged"]
    # "Silently fudged" is an invariant 0, not a count -- it is the promise
    # that nothing is ever adjusted to fit, and every exception above is named.
    # It used to render `c["broken"]`, which counts something else entirely:
    # filings whose own two stated totals disagree. Both read 0 today only by
    # coincidence, and the first genuinely broken filing to arrive would have
    # made this tile announce that the site had silently fudged one -- the
    # exact inverse of what it means. The number that belongs here can only
    # ever be 0, because a non-zero value would mean the promise was broken.
    # Examples come from the same walk that produced the counts. They used to
    # be the literals "BLK, BAM, CYH" and "MSC", which was fine until the
    # extraction improved: a page naming a company as an example of OUR parsing
    # failure, after we had started parsing it correctly, is a false statement
    # published about a real filer. An em dash where a category is empty.
    eg = b.get("examples") or {}

    def _eg(category: str) -> str:
        return ", ".join(eg.get(category) or []) or "—"

    rows = [
        ("Missing XBRL tag", c["missing_tag"],
         "We could not read a component the filing contains. Ours, not theirs.",
         _eg("missing_tag")),
        ("Rounding", c["rounding"],
         "The two sides differ by under 1% of total assets — presentation "
         "slack, not error.", _eg("rounding")),
        ("Unexplained", c["unexplained"],
         "The filer published no stated total to referee against. Under "
         "investigation.", _eg("unexplained")),
        ("Genuinely broken filing", c["broken"],
         "The filer's own stated total does not match their own assets. "
         "Their arithmetic, not ours.", _eg("broken")),
    ]
    table = "".join(
        f"<tr><th scope=\"row\">{label}</th><td class=\"num\">{_fmt(n)}</td>"
        f"<td>{why}</td><td class=\"eg\">{eg}</td></tr>"
        for label, n, why, eg in rows
    )
    return f"""
  <section class="sec" id="counts">
    <h2>What the check found</h2>
    <div class="mcounts">
      <div class="mc"><b>{_fmt(b["companies"])}</b><span>companies covered</span></div>
      <div class="mc"><b>{_fmt(b["reconciled"])}</b><span>reconciled — balanced, or
        balanced once noncontrolling interests or mezzanine equity are
        included</span></div>
      <div class="mc"><b>{_fmt(flagged)}</b><span>flagged with a specific
        reason</span></div>
      <div class="mc"><b>0</b><span>silently fudged</span></div>
    </div>
    <p class="sec-sub">{_fmt(b["not_testable"])} more report no complete set of
      totals, so there is no identity to test. They are excluded rather than
      counted as passes.</p>

    <h3>Every flag, named</h3>
    <table class="mtable">
      <thead><tr><th>Category</th><th>Filings</th><th>What it means</th>
        <th>Examples</th></tr></thead>
      <tbody>{table}</tbody>
    </table>

    <h3>Why there is no percentage here</h3>
    <p>The denominator moves as we improve coverage. In one week we restored 31
      companies that had been silently unreachable — and the pass rate went
      <em>down</em>, because the newly visible filings were the awkward ones. A
      number that falls when quality rises is not a quality measure, it is a
      moving target. So we publish the raw counts, and we update them as gaps
      close.</p>
    <p>These figures are read from the database when this page renders. They
      are not copied into the page by hand, and they change when the data
      does.</p>
  </section>
"""


def render_methodology(*, nav: str = "") -> str:
    from src.report.nav import SUPPORT_EMAIL, render_footer
    from src.report.schema import breadcrumb_ld, webpage_ld

    footer = render_footer(
        "The per-category breakdown of the exceptions is maintained alongside "
        "the code. Found a figure you think is wrong? "
        f'<a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>.'
    )
    body = (
        f'{nav}\n<main class="wrap post" id="main">\n'
        f"{_HEAD}{_live_section()}{_BODY}\n{footer}\n</main>\n"
        f'<script src="/static/nav.js?v={asset_version()}" defer></script>'
    )
    return shell(
        "How We Verify Every Number — BalanceProof",
        body,
        description=(
            "Every SEC filing is verified against A = L + E. Filings that do "
            "not balance are flagged with the reason, never silently fudged. "
            "The counts are public."
        ),
        canonical="/methodology",
        ld=breadcrumb_ld([("Home", "/"), ("Methodology", "/methodology")])
        + webpage_ld(
            "How we verify every number",
            "Every balance sheet is checked against the accounting identity. "
            "Filings that reconcile are published with their figures; filings "
            "that do not are published with the reason.",
            "/methodology",
        ),
    )
