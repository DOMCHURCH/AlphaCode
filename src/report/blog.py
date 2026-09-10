"""/blog — posts, and the registry the sitemap reads.

Server-rendered like everything else here, and deliberately not a CMS. A post
is a `Post` in `POSTS` with its body written as HTML in this file. That is the
right shape at this size: there are no drafts, no scheduling and no second
author, and a database-backed CMS would be a table, a migration, an editor and
an auth story bought to solve a problem nobody has yet. When there are twenty
posts and somebody other than me is writing them, this becomes a directory of
Markdown files and a loader. Not before.

The registry is what `sitemap._blog_posts` reads, so publishing a post is one
edit rather than two -- a sitemap that has to be remembered is a sitemap that
goes stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell


@dataclass(frozen=True)
class Post:
    """One post. `body` is HTML because the posts carry code blocks and
    tables, and a Markdown renderer is a dependency bought for four files."""

    slug: str
    title: str
    # The <title> tag, which is not the headline: a headline is written to be
    # read on the page, a title tag to be read in a result list next to nine
    # others. Keeping them separate is the difference between the two jobs.
    seo_title: str
    description: str
    published: str
    updated: str
    minutes: int
    body: str


_POST_XBRL_ACCURACY = Post(
    slug="sec-xbrl-data-wrong-one-in-five",
    title="Why SEC XBRL Data Is Wrong 1 Out of 5 Times (And How to Fix It)",
    seo_title="SEC Filings API Accuracy: Why XBRL Data Is Wrong 1 in 5 Times",
    description=(
        "JPMorgan reports Total Assets 23 different ways in a single filing. "
        "Most SEC filings APIs pick one at random and are right about 78.6% of "
        "the time. Here is why, and how the accounting identity fixes it."
    ),
    published="2026-09-09",
    updated="2026-09-09",
    minutes=7,
    body="""
<p class="lede">I pulled JPMorgan's 10-Q expecting one number for total assets.
I got twenty-three.</p>

<p>Not twenty-three <em>values</em> — twenty-three <em>tags</em>, all of them
called <code>Assets</code>, all of them in the same filing, all of them
technically correct. One for the consolidated bank. One for the investment
bank. One for consumer banking. One for each subsidiary that has to be broken
out. And exactly one of them is the number you actually want.</p>

<p>If your code does what mine did the first time — take the first
<code>Assets</code> fact and move on — you get a segment. Sometimes a small
one. And nothing anywhere tells you that you got the wrong one, because you
didn't. You got <em>a</em> right one.</p>

<h2>This is not a JPMorgan problem</h2>

<p>XBRL lets a filer attach dimensions to a fact: this figure, but for this
segment, this geography, this legal entity. That is a genuinely good idea. A
bank's consumer arm and its investment arm are different businesses and
flattening them into one line would lose real information.</p>

<p>The problem is that the consolidated figure — the one on the face of the
balance sheet, the one everybody means by "total assets" — is stored the same
way as all the others. It is just the one with <em>no</em> dimensions on it.
So the correct figure is defined by an absence, and an absence is the easiest
thing in the world for a parser to fail to notice.</p>

<p>I went and measured how often this bites. Across the companies I had loaded
at the time, taking a naive first-match approach agreed with the consolidated
figure about <strong>78.6%</strong> of the time. Roughly one filing in five is
wrong. Not slightly wrong — wrong by whatever the largest segment happens to
be, which for a big bank can be most of the balance sheet.</p>

<p>That number is not a swipe at anyone. It is what you get from the obvious
implementation, and the obvious implementation is what almost everybody ships,
because nothing about it looks broken. Your parser runs clean. Your JSON has a
number in it. Your backtest returns a Sharpe ratio. Everything is fine right up
until somebody asks you to tie a figure back to the filing.</p>

<h2>The naive version</h2>

<p>Here is roughly what most extractors do. It reads fine and it is wrong one
time in five:</p>

<pre class="code"><code>import requests

FACTS = "https://data.sec.gov/api/xbrl/companyconcept/CIK0000019617/us-gaap/Assets.json"
facts = requests.get(FACTS, headers={"User-Agent": "you you@example.com"}).json()

# Take the most recent 10-Q figure. What could go wrong.
quarterly = [f for f in facts["units"]["USD"] if f.get("form") == "10-Q"]
total_assets = sorted(quarterly, key=lambda f: f["end"])[-1]["val"]

print(total_assets)   # a number. which one? nobody knows</code></pre>

<p>There is no error here to catch. There is no exception, no null, no warning.
The API gave you facts and you picked one. The bug is entirely in the
<em>selection</em>, and selection bugs are invisible.</p>

<h2>The fix is 500 years old</h2>

<p>You do not need a heuristic for this, and you definitely do not need a
model. You need the identity that has defined a balance sheet since Pacioli
wrote it down in 1494:</p>

<p class="pull">Assets = Liabilities + Equity</p>

<p>That equation is not a guideline. It is what makes the document a balance
sheet. And it gives you something better than a guess: a
<strong>test</strong>. Pull every candidate for assets, every candidate for
liabilities, every candidate for equity — then find the combination that
actually balances. The consolidated figures balance against each other. A
segment's assets do not balance against the whole company's liabilities. The
arithmetic tells you which set is the real one.</p>

<pre class="code"><code># Pick the candidate set that satisfies A = L + E.
#
# `assets` and friends are EVERY fact reported for that concept in one
# filing, dimensioned ones included. Exactly one combination balances,
# and it is the consolidated one.
def reconcile(assets, liabilities, equity, tolerance=0.005):
    best = None
    for a in assets:
        for lia in liabilities:
            for eq in equity:
                if a <= 0:
                    continue
                gap = abs(a - (lia + eq)) / a
                if gap &lt;= tolerance and (best is None or gap &lt; best[0]):
                    best = (gap, a, lia, eq)
    if best is None:
        return None          # it does not balance: say so, do not guess
    _, a, lia, eq = best
    return {"total_assets": a, "total_liabilities": lia, "total_equity": eq}</code></pre>

<p>Two details matter more than the loop.</p>

<p><strong>The tolerance is small and it is a fraction, not a constant.</strong>
Filers round. Half a percent of assets absorbs that; a fixed dollar tolerance
either fails every large bank or passes anything at a small one.</p>

<p><strong>When nothing balances, return nothing.</strong> This is the part
that is tempting to skip. If no combination reconciles, the honest answer is
that this filing does not balance — not the closest match. A number that is
wrong by a factor is worse than no number, because you will never audit the
one you were given.</p>

<h2>What I built</h2>

<p>I'm 17. I started this because I wanted balance sheet data for something
else entirely and I could not find a source I trusted enough to build on. Every
free one I tried disagreed with the filing somewhere, and the paid ones wanted
enterprise money to tell me what the SEC publishes for free.</p>

<p>So <a href="/">To Scale</a> is the reconciler, running over every filing,
with the result behind an API. Every figure is checked against A = L + E before
it is stored. Across <strong>{COMPANIES} companies and {FACTS} data points</strong>
that gets to <strong>99.9%</strong> — against the 78.6% you get from taking the
first tag.</p>

<p>The 0.1% is not rounding. Those are filings that genuinely do not balance,
and the site draws them with a red warning saying so rather than quietly
adjusting the numbers until they agree. If a company filed something that does
not add up, that is a fact about the company, and it should reach you as one.</p>

<p>Every response is as-reported. Nothing is estimated, nothing is
forward-filled, nothing is smoothed. If a filer does not break out receivables,
you get a labelled remainder rather than a zero — because a zero is a claim and
"they did not say" is not.</p>

<h2>Try it against a filing you already know</h2>

<p>The free tier needs no card. Pick a company where you know the answer, call
it, and check the number against the 10-Q yourself — that is the only test that
means anything:</p>

<pre class="code"><code>curl -H "X-API-Key: YOUR_KEY" https://toscale.pro/api/company/JPM</code></pre>

<p>You can also look up any company on the site with no key and no account at
all — the drawings are free and always will be. Get a key from the
<a href="/dashboard">dashboard</a>, read the
<a href="/api">API reference</a>, or see what the tiers cost on the
<a href="/pricing">pricing page</a>. If you would rather have the whole thing
as one file, the <a href="/dataset">full dataset</a> is a one-time download.</p>

<p>And if you find a figure that disagrees with the filing, tell me. That is
the one bug report I actually want.</p>
""",
)

_POST_BANK_BALANCE_SHEETS = Post(
    slug="why-bank-balance-sheets-are-different",
    title="Why Bank Balance Sheets Are Different",
    seo_title="How to Read a Bank Balance Sheet (Deposits, Loans, Leverage)",
    description=(
        "A bank's balance sheet inverts the shape you expect: deposits are "
        "liabilities, loans are assets, and equity is a sliver. What that "
        "means when you read JPM, BAC or WFC at true proportion."
    ),
    published="2026-09-10",
    updated="2026-09-10",
    minutes=3,
    body="""
<p class="lede">Look at a bank drawn to scale and the first thing you notice is
that the equity block is almost too thin to label.</p>

<p>That is not a rendering bug. A large US bank runs on roughly ten cents of
equity per dollar of assets, where a software company might run on sixty. Draw
both at true proportion and they look like different kinds of object, which is
most of the argument for drawing them at all.</p>

<p>The second thing that trips people up is the direction of the two big lines.
<strong>Deposits are a liability.</strong> The money in your current account is
owed back to you, so it sits on the claims side. <strong>Loans are an
asset</strong> — a promise of repayment the bank owns. A reader who expects
"deposits = money the bank has" reads the whole picture backwards.</p>

<p>This also breaks the tag-picking heuristics that most XBRL extractors use. A
bank files <code>Assets</code> for the consolidated group and again for each
segment, and the segment figures for a large institution are themselves larger
than most companies' entire balance sheets. Picking the biggest number, or the
first one, produces something plausible and wrong. The accounting identity is
what settles it: only the consolidated set satisfies
<strong>A = L + E</strong>, so that is the set that gets served.</p>

<h2>What to look at</h2>

<ul>
  <li><strong>Deposits as a share of liabilities.</strong> A deposit-funded
    bank and a wholesale-funded one behave very differently under stress.</li>
  <li><strong>Loans as a share of assets.</strong> The rest is securities, cash
    and trading positions.</li>
  <li><strong>The equity sliver.</strong> Ten percent is ordinary. Two percent
    is a different conversation.</li>
</ul>

<p><em>This is a short note and it will grow. The section on securities
portfolios and held-to-maturity accounting is the obvious next piece.</em></p>
""",
)


_POST_ACCOUNTING_IDENTITY = Post(
    slug="understanding-the-accounting-identity",
    title="Understanding the Accounting Identity",
    seo_title="Assets = Liabilities + Equity: What the Identity Actually Proves",
    description=(
        "Assets = Liabilities + Equity is not a rule filers follow. It is a "
        "consequence of double-entry bookkeeping, which is what makes it "
        "usable as a test on data you did not produce."
    ),
    published="2026-09-10",
    updated="2026-09-10",
    minutes=3,
    body="""
<p class="lede">The identity is not a rule companies are asked to obey. It is a
consequence of how the books are kept, which is exactly what makes it useful to
somebody reading those books from outside.</p>

<p>Every entry in double-entry bookkeeping touches two accounts. Buy a machine
with cash and assets do not change — one asset becomes another. Buy it with a
loan and assets and liabilities rise together. There is no legal transaction
that moves one side without the other, so at the end of any period
<strong>Assets = Liabilities + Equity</strong> holds by construction.</p>

<p>That is why it works as a <em>test</em>. A figure you have extracted from a
filing is not verifiable on its own — you cannot tell a correct total assets
from an incorrect one by looking at it. But three figures together either
balance or they do not, and a set that balances is very unlikely to contain a
wrong one. It is a checksum somebody else already computed for you.</p>

<h2>When it does not balance</h2>

<p>Sometimes the arithmetic genuinely fails. Rounding in the filing, a
presentation choice, or an error. When that happens the honest answer is to
serve the figures <strong>as reported</strong> and say the filing does not
balance, with the gap stated as a percentage — not to adjust a number until the
columns agree. An adjusted figure is no longer what the company filed, and what
the company filed is the whole product.</p>

<p>The one case that looks like a failure and is not: <strong>negative
equity</strong>. Liabilities exceeding assets balances perfectly well, it just
draws with the equity block below the baseline.</p>

<p><em>A short note. Minority interest and the difference between parent-only
and NCI-inclusive equity deserve their own piece.</em></p>
""",
)


# Newest first: /blog lists them in this order, and a reader arriving at
# the hub should meet the most recent thinking rather than the oldest.
POSTS: tuple[Post, ...] = (
    _POST_XBRL_ACCURACY,
    _POST_BANK_BALANCE_SHEETS,
    _POST_ACCOUNTING_IDENTITY,
)
BY_SLUG: dict[str, Post] = {p.slug: p for p in POSTS}


def _nav_and_footer(nav: str) -> tuple[str, str]:
    from src.report.nav import render_footer

    return nav, render_footer(
        'Written by Dominique Church. Source: '
        '<a href="https://www.sec.gov/dera/data/financial-statement-data-sets"'
        ' rel="noopener">SEC Financial Statement Data Sets</a>.'
    )


def render_index(*, nav: str = "") -> str:
    """The post list. Kept plain: one post does not need a grid."""
    from src.report.schema import breadcrumb_ld

    nav_html, footer = _nav_and_footer(nav)
    items = "".join(
        f"""
      <li class="post-item">
        <a class="post-link" href="/blog/{escape(p.slug)}">{escape(p.title)}</a>
        <p class="post-desc">{escape(p.description)}</p>
        <p class="post-meta">{escape(p.published)} · {p.minutes} min read</p>
      </li>"""
        for p in POSTS
    )
    body = f"""{nav_html}
<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">Notes</h1>
    <p class="hlede">What I learned building a reconciler over every SEC filing
      — the parts that surprised me, written down while they were still
      surprising.</p>
  </header>
  <section class="sec">
    <ul class="post-list">{items}</ul>
  </section>
{footer}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>"""

    return shell(
        "Notes — To Scale",
        body,
        description=(
            "Notes on SEC XBRL data quality, balance sheet reconciliation and "
            "building a financial data API that agrees with the filing."
        ),
        canonical="/blog",
        ld=breadcrumb_ld([("Home", "/"), ("Notes", "/blog")]),
    )


def _fill_scale(prose: str) -> str:
    """Put today's figures into a post's `{COMPANIES}` / `{FACTS}` slots.

    A post is prose written on a date, so a live number in the middle of a
    paragraph is a real question rather than an obvious win. It is done anyway,
    because the alternative was what happened: this post said "1.7 million data
    points" while /dataset counted the same table and said 1.8M, and a note
    ARGUING that most providers get their numbers wrong is the worst possible
    place on the site to disagree with itself.

    Falls back to the figures as first published rather than to a blank -- a
    sentence reading "Across  companies and  data points" is worse than a
    slightly old number, and unlike the meta descriptions this text has to
    scan as English.
    """
    from src.dataset import facts_label
    from src.report.home_page import companies_label

    return prose.replace(
        "{COMPANIES}", companies_label() or "6,201"
    ).replace("{FACTS}", facts_label() or "1.8M")


# The companies each post actually TALKS ABOUT, with the reason it names them.
#
# Hand-written per post, not generated. An automatic "related companies" strip
# would put the same five tickers under every note, which is link stuffing with
# extra steps: the value of a link from a post about bank balance sheets to JPM
# is that the post is genuinely about what JPM's balance sheet looks like, and
# nothing computable knows that.
_POST_COMPANIES: dict[str, tuple[tuple[str, str], ...]] = {
    "sec-xbrl-data-wrong-one-in-five": (
        ("JPM", "the filing this post opens with — 23 tags for one figure"),
        ("BAC", "the same segment problem, a different bank"),
        ("GS", "fewer segments, and it shows in the tag count"),
    ),
    "why-bank-balance-sheets-are-different": (
        ("JPM", "deposits and loans at the scale the post describes"),
        ("WFC", "a deposit-funded balance sheet"),
        ("MSFT", "the contrast — asset-light, equity-funded"),
    ),
    "understanding-the-accounting-identity": (
        ("AAL", "negative equity that balances perfectly"),
        ("WMT", "an ordinary A = L + E, drawn"),
        ("FCX", "capital-heavy, and the identity still holds"),
    ),
}


def _related_companies(post: Post) -> str:
    """Links from a post to the companies it is actually about."""
    rows = _POST_COMPANIES.get(post.slug, ())
    if not rows:
        return ""
    items = "".join(
        f'<li><a href="/company/{t}">{t}</a> — {escape(why)}</li>'
        for t, why in rows
    )
    return f"""
  <section class="sec" id="worked-examples">
    <div class="sec-head"><h2>See it on a real filing</h2></div>
    <p class="sec-sub">Every figure on these pages is as reported, drawn at true
      proportion. No account needed.</p>
    <ul class="notelist">{items}</ul>
  </section>"""


def _related_reading(post: Post) -> str:
    """The other posts, with the sentence that says why you would read them.

    Every post links to every other one because there are three of them. When
    there are thirty this becomes a real relevance question; the shape is here
    so that is a change to one function rather than to every post.
    """
    others = [p for p in POSTS if p.slug != post.slug]
    if not others:
        return ""
    items = "".join(
        f'<li><a href="/blog/{p.slug}">{escape(p.title)}</a> — '
        f"{escape(p.description.split('.')[0])}.</li>"
        for p in others
    )
    return f"""
  <section class="sec" id="related-reading">
    <div class="sec-head"><h2>Related reading</h2></div>
    <ul class="notelist">{items}</ul>
  </section>"""


def render_post(post: Post, *, nav: str = "") -> str:
    from src.report.schema import blogposting_ld, breadcrumb_ld

    nav_html, footer = _nav_and_footer(nav)
    body = f"""{nav_html}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <p class="post-meta"><a href="/blog">Notes</a> · {escape(post.published)}
        · {post.minutes} min read</p>
      <h1 class="htitle">{escape(post.title)}</h1>
    </header>
    <div class="prose">{_fill_scale(post.body)}</div>
  </article>
  {_related_companies(post)}

  <aside class="sec cta">
    <div class="sec-head"><h2>Check it against a filing you know</h2></div>
    <p class="sec-sub">The free tier needs no card, and looking companies up on
      the site needs no account at all.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a free API key</a>
      <a class="btn ghost" href="/pricing">See pricing</a>
      <a class="btn ghost" href="/api">Read the API docs</a>
    </div>
  </aside>

  {_related_reading(post)}
{footer}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>"""

    return shell(
        post.seo_title,
        body,
        description=post.description,
        canonical=f"/blog/{post.slug}",
        ld=blogposting_ld(
            title=post.title,
            description=post.description,
            slug=post.slug,
            published=post.published,
            modified=post.updated,
        )
        + breadcrumb_ld([
            ("Home", "/"),
            ("Notes", "/blog"),
            (post.title, f"/blog/{post.slug}"),
        ]),
    )
