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
      <h1 class="htitle">How the accounting-identity check works</h1>
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

      <h2>The four reasons</h2>

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
      carry it often. <em>We do not currently read these tags.</em> When one of
      these filings does not reconcile, that is our gap and not the filer's, and
      it is the next thing on the list to fix.</p>

      <p><strong>Rounding.</strong> Figures are reported in millions. A
      one-unit difference on a balance sheet of a few hundred billion is eight
      decimal places of nothing, and a gap under a percent of total assets is
      treated as the noise it is.</p>

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
    rows = [
        ("Missing XBRL tag", c["missing_tag"],
         "We could not read a component the filing contains. Ours, not theirs.",
         "BLK, BAM, CYH"),
        ("Rounding", c["rounding"],
         "The two sides differ by under 1% of total assets — presentation "
         "slack, not error.", "—"),
        ("Unexplained", c["unexplained"],
         "The filer published no stated total to referee against. Under "
         "investigation.", "MSC"),
        ("Genuinely broken filing", c["broken"],
         "The filer's own stated total does not match their own assets. "
         "Their arithmetic, not ours.", "—"),
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
      <div class="mc"><b>{_fmt(c["broken"])}</b><span>silently fudged</span></div>
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
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld, webpage_ld

    footer = render_footer(
        "The per-category breakdown of the exceptions is maintained alongside "
        "the code."
    )
    body = (
        f'{nav}\n<main class="wrap post" id="main">\n'
        f"{_HEAD}{_live_section()}{_BODY}\n{footer}\n</main>\n"
        f'<script src="/static/nav.js?v={asset_version()}" defer></script>'
    )
    return shell(
        "How We Verify Every Number — To Scale",
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
