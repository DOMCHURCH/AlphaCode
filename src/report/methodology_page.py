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

_BODY = """
<main class="wrap post" id="main">
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


def render_methodology(*, nav: str = "") -> str:
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld

    footer = render_footer(
        "The per-category breakdown of the exceptions is maintained alongside "
        "the code."
    )
    body = (
        f"{nav}{_BODY}\n{footer}\n</main>\n"
        f'<script src="/static/nav.js?v={asset_version()}" defer></script>'
    )
    return shell(
        "How the Accounting-Identity Check Works — To Scale",
        body,
        description=(
            "Why some filings do not balance on Assets = Liabilities + "
            "Equity: noncontrolling interests, mezzanine equity, rounding, and "
            "filings that genuinely do not balance — each flagged, never fudged."
        ),
        canonical="/methodology",
        ld=breadcrumb_ld([("Home", "/"), ("Methodology", "/methodology")]),
    )
