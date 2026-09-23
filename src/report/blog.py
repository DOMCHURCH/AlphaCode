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
    # What the INDEX says about this post, which is not the meta description.
    # A meta description is written to be read in a result list beside nine
    # others and has 155 characters to work in; this is read by somebody
    # already on the page, deciding which of five to open. Same distinction
    # as `title` and `seo_title`, for the same reason.
    summary: str
    published: str
    updated: str
    minutes: int
    body: str
    # Question-shaped queries reach some posts ("accounting identity meaning",
    # "the balance sheet identity is"). These render as a visible Q&A under
    # the article AND as FAQPage JSON-LD, from this one tuple, so the markup
    # can never claim an answer the page does not show (see `faq_ld`).
    faq: tuple[tuple[str, str], ...] = ()


_POST_XBRL_ACCURACY = Post(
    slug="sec-xbrl-data-wrong-one-in-five",
    # The subject is the PARSER, not the SEC. This post is linked directly
    # beneath the identity sentence on every flagged company page, and the
    # earlier title -- "Why SEC XBRL Data Is Wrong" -- read as a second
    # accusation against the filer sitting under the first. The body always
    # said this: the thing that is wrong 1 in 5 times is naive tag-picking,
    # which is also what `compare.py`'s NAIVE_ACCURACY measures. The slug is
    # deliberately unchanged -- `_learn_more` looks posts up by slug and drops
    # the link silently on a miss, so renaming it would quietly remove this
    # post from every company page.
    title="Why Naive XBRL Extraction Is Wrong 1 Out of 5 Times (And How to Fix It)",
    # 61 characters, which truncated in a result list. The "SEC Filings API
    # Accuracy:" prefix was the part carrying least weight -- "accuracy" is a
    # word this site deliberately does not quantify anywhere else -- so it is
    # the part that goes. The keyword and the hook both survive.
    # 47 chars. Moving the subject from "SEC XBRL Data" to "Naive XBRL
    # Extraction" cost eight characters, which pushed the old parenthetical
    # past the 60-char result-list budget and truncated it to nothing. The
    # "(And How to Fix It)" hook is the part that goes: the H1 above keeps it,
    # where there is room, and a hook nobody sees is not a hook.
    seo_title="Why Naive XBRL Extraction Is Wrong 1 in 5 Times",
    description=(
        "JPMorgan reports Total Assets 23 different ways in one SEC filing. "
        "Most filings APIs pick one at random. Here is why, and how "
        "A = L + E fixes it."
    ),
    summary=(
        "JPMorgan tags Total Assets 23 times in one filing and exactly one "
        "of them is the bank. This is how a parser picks the wrong one "
        "without ever raising an error, and what the accounting identity "
        "does about it."
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
figure roughly <strong>four times in five</strong>. The fifth is
wrong. Not slightly wrong — wrong by whatever the largest segment happens to
be, which for a big bank can be most of the balance sheet.</p>

<p><strong>Note:</strong> the 1-in-5 figure measures how often a naive
first-tag pick returns a non-consolidated value. It is not the same as the
share of filings still flagged after reconciliation, which is the much smaller
number on the <a href="/">home page</a>. The first is what you get without
BalanceProof. The second is what is left after.</p>

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

<p>So <a href="/">BalanceProof</a> is the reconciler, running over every filing,
with the result behind an API. Every figure is checked against A = L + E before
it is stored. Across <strong>{COMPANIES} companies and {FACTS} data points</strong>
that reconciles to the accounting identity — against the silent failure you get from taking the
first tag.</p>

<p>The exceptions are not rounding. Those are filings that genuinely do not balance,
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

<pre class="code"><code>curl -H "X-API-Key: YOUR_KEY" https://balanceproof.dev/api/company/JPM</code></pre>

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
        "liabilities, loans are assets, equity is a sliver. "
        "Read JPM and BAC at true proportion."
    ),
    summary=(
        "A bank runs on roughly ten cents of equity per dollar of assets, "
        "where a software company might run on sixty. Why that is the "
        "business rather than a warning sign, and what it looks like drawn "
        "at true proportion."
    ),
    published="2026-09-10",
    updated="2026-09-10",
    minutes=5,
    body="""
<p class="lede">Look at a bank drawn to scale and the first thing you notice is
that the equity block is almost too thin to label.</p>

<p>That is not a rendering bug. A large US bank runs on roughly ten cents of
equity per dollar of assets, where a software company might run on sixty. Draw
both at true proportion and they look like different kinds of object, which is
most of the argument for drawing them at all.</p>

<h2>The thin block is the business, not a warning</h2>

<p>A bank borrows short and lends long. It takes money that can be withdrawn on
demand and turns it into loans and securities that cannot be called back on
demand, and it keeps the difference between what it pays for the first and
earns on the second. That spread is thin, so it only produces a meaningful
return on equity if the equity is small relative to the assets it supports.
Leverage is not a risk a bank has taken on top of its business. Leverage
<em>is</em> the business.</p>

<p>Which is why the sliver is regulated rather than left to management. Capital
requirements are written as ratios, and the ones that bind are risk-weighted: a
book of government bonds and a book of unsecured consumer loans do not consume
the same capital at the same dollar size. So the equity block you see drawn
against total assets is not the ratio a supervisor is looking at. It is the
plain arithmetic one — the things, the claims, and what is left over — and it
is the one that tells you how much has to go wrong before the claims exceed the
things.</p>

<h2>The two big lines run backwards</h2>

<p><strong>Deposits are a liability.</strong> The money in your current account
is owed back to you, so it sits on the claims side. <strong>Loans are an
asset</strong> — a promise of repayment the bank owns. A reader who expects
"deposits = money the bank has" reads the whole picture backwards, and it is an
easy expectation to hold: in almost every other kind of company, a deposit is
something the business received and got to keep.</p>

<p>Once that flips, the rest of the shape follows. Deposits are usually the
largest single block on the claims side. Loans are usually the largest on the
things side. And the difference between how fast each of those can move is
most of what makes a bank a bank.</p>

<h2>What else is in there</h2>

<p>Loans are not the whole asset side. A bank also carries a securities
portfolio, and how it is measured depends on what the bank says it intends to
do with it. Securities classified <strong>available for sale</strong> are
carried at fair value, so a fall in market price shows up in the carrying
amount. Securities classified <strong>held to maturity</strong> are carried at
amortised cost, on the reasoning that a bond held to the end pays par whatever
it traded at in between.</p>

<p>That distinction is invisible in the totals and occasionally enormous. A
held-to-maturity book bought at low yields and carried at cost can sit on an
unrealised loss the balance sheet does not show, and the loss stays theoretical
exactly as long as the bank is never forced to sell. When deposits leave faster
than expected, it stops being theoretical. That was the mechanism behind the
2023 US regional bank failures, and none of it was concealed — it was in the
notes, in a table, beside a total that did not reflect it.</p>

<p>The lesson is not that the totals lie. It is that a balance sheet is a
statement of amounts, and the measurement basis behind an amount is a separate
question the totals cannot answer.</p>

<h2>Why this breaks most XBRL extractors</h2>

<p>A bank files <code>Assets</code> for the consolidated group and again for
each segment, and the segment figures for a large institution are themselves
larger than most companies' entire balance sheets. Picking the biggest number,
or the first one, produces something plausible and wrong. The accounting
identity is what settles it: only the consolidated set satisfies
<strong>A = L + E</strong>, so that is the set that gets served. The long
version of that argument is
<a href="/blog/sec-xbrl-data-wrong-one-in-five">its own note</a>.</p>

<h2>What to look at</h2>

<ul>
  <li><strong>Deposits as a share of liabilities.</strong> A deposit-funded
    bank and a wholesale-funded one behave very differently under stress.
    Retail deposits are stickier and cheaper; wholesale funding reprices, and
    it leaves.</li>
  <li><strong>Loans as a share of assets.</strong> The rest is securities, cash
    and trading positions, and a bank that is mostly securities is running a
    different business from one that is mostly loans.</li>
  <li><strong>The equity sliver.</strong> Ten percent is ordinary. Two percent
    is a different conversation.</li>
</ul>

<p>All three are ratios between blocks sitting on the same drawing, which is
the point of drawing it. Put <a href="/company/JPM">JPM</a> next to
<a href="/company/MSFT">MSFT</a> and the difference is not a number you have to
hold in your head — it is the shape of the picture.</p>
""",
)


_POST_ACCOUNTING_IDENTITY = Post(
    slug="understanding-the-accounting-identity",
    title="Understanding the Accounting Identity",
    # 54 characters. Search Console (2026-09-23) showed the post reached by
    # "accounting identity", "accounting identity meaning", "the balance sheet
    # identity is" and "balance sheet identity" -- 16 impressions, 0 clicks --
    # while neither the title tag nor the description contained the phrase.
    seo_title="The Accounting Identity: Assets = Liabilities + Equity",
    description=(
        "The accounting identity is Assets = Liabilities + Equity. It follows "
        "from double-entry bookkeeping, which makes it a test on data you did "
        "not produce."
    ),
    summary=(
        "Assets = Liabilities + Equity is not a guideline, it is what makes "
        "the document a balance sheet. That makes it a test you can run "
        "against data you did not produce, including on companies with "
        "negative equity."
    ),
    published="2026-09-10",
    updated="2026-09-23",
    minutes=5,
    faq=(
        (
            "What is the accounting identity?",
            "The accounting identity is the equation Assets = Liabilities + "
            "Equity. It holds for every balance sheet because double-entry "
            "bookkeeping records each transaction on two sides at once, so it "
            "is a mechanical result of how the books are kept rather than a "
            "rule a company chooses to follow.",
        ),
        (
            "What is the balance sheet identity?",
            "The balance sheet identity is another name for the same equation, "
            "Assets = Liabilities + Equity. Because it holds by construction, "
            "it can check figures taken from a filing, not only define what a "
            "balance sheet is.",
        ),
        (
            "What does the accounting identity mean in practice?",
            "Three figures from one filing, total assets, total liabilities and "
            "equity, have to agree. If they do not, something in the extraction "
            "or, rarely, in the filing itself is wrong, which is why "
            "BalanceProof runs the check on every company before serving it.",
        ),
        (
            "Why doesn't a balance sheet always balance?",
            "When a filing appears not to balance, there are five causes: a "
            "tag the extractor did not read, noncontrolling interests, "
            "mezzanine equity, rounding, and, rarely, a filing that genuinely "
            "does not balance. The first four are gaps in the test, not in the "
            "filing.",
        ),
    ),
    body="""
<p class="lede">The identity is not a rule companies are asked to obey. It is a
consequence of how the books are kept, which is exactly what makes it useful to
somebody reading those books from outside.</p>

<p>Every entry in double-entry bookkeeping touches two accounts. Buy a machine
with cash and assets do not change — one asset becomes another. Buy it with a
loan and assets and liabilities rise together. There is no legal transaction
that moves one side without the other, so at the end of any period
<strong>Assets = Liabilities + Equity</strong> holds by construction.
That equation is the accounting identity, also called the balance sheet
identity, because it is the one equation every balance sheet has to
satisfy.</p>

<p>That is why it works as a <em>test</em>. A figure you have extracted from a
filing is not verifiable on its own — you cannot tell a correct total assets
from an incorrect one by looking at it. But three figures together either
balance or they do not, and a set that balances is very unlikely to contain a
wrong one. It is a checksum somebody else already computed for you.</p>

<h2>The right-hand side has more than two terms</h2>

<p>This is where a naive implementation of the test starts failing filings that
are perfectly fine.</p>

<p>"Equity" in the identity means the equity of the whole consolidated entity.
When a parent owns 80% of a subsidiary it consolidates <em>all</em> of that
subsidiary's assets and liabilities, and the fifth it does not own appears on
the claims side as a <strong>noncontrolling interest</strong>. Filers present
this two ways: some publish one total equity figure that already includes the
NCI, and some publish parent equity and the noncontrolling interest as separate
lines. Test <code>A = L + E</code> against the second shape using parent equity
alone and you get a gap exactly the size of the minority stake. The filing is
correct. The test is wrong.</p>

<p>A second term sits in neither column cleanly. <strong>Mezzanine
equity</strong> — redeemable preferred stock, redeemable noncontrolling
interests — is presented between liabilities and equity precisely because it is
not unambiguously either. It can be required to be redeemed, which is debt-like,
but it carries no fixed obligation the way debt does. It turns up in airlines,
in biotech, and in anything that came through a SPAC. Ignore it and, again, the
arithmetic fails on a filing that is fine.</p>

<p>So the identity a reader actually needs is closer to this:</p>

<p class="pull">Assets = Liabilities + Mezzanine + Equity + NCI</p>

<p>The discipline that keeps this honest is to treat every additional term as a
<em>reason</em> rather than a fudge factor. Each one has to be a line the filer
actually published, named before it is used. Adding a term because it closes a
gap you do not understand is how a test quietly stops being a test.</p>

<h2>When it does not balance</h2>

<p>Sometimes the arithmetic genuinely fails. Rounding in the filing, a
presentation choice, or an error. When that happens the honest answer is to
serve the figures <strong>as reported</strong> and say the filing does not
balance, with the gap stated as a percentage — not to adjust a number until the
columns agree. An adjusted figure is no longer what the company filed, and what
the company filed is the whole product.</p>

<p>The tolerance for "balances" should be a fraction of assets rather than a
fixed amount. Filers round, and they round at a scale set by their own size: a
flat dollar tolerance either fails every large bank or waves through anything
at a small company. Half a percent of assets absorbs presentation rounding
without absorbing a real error.</p>

<p>The one case that looks like a failure and is not: <strong>negative
equity</strong>. Liabilities exceeding assets balances perfectly well — it just
draws with the equity block below the baseline.
<a href="/company/AAL">AAL</a> is the standing example, and the drawing says
more about it than the number does.</p>

<h2>What the identity does not prove</h2>

<p>It is a consistency check, not a truth check. Books can balance to the cent
and still describe a company that does not exist: the ledgers in the large
accounting frauds balanced too, because balancing is what double-entry does
automatically. Misstatement happens earlier, in what gets recorded and at what
value, and it arrives at the balance sheet already reconciled.</p>

<p>So the identity tells you that the three figures you pulled belong to the
same statement. It tells you nothing about whether that statement is honest.
That is still worth a great deal, because it is the failure mode you can
actually do something about from outside. You are not going to catch a fraud
from a data feed. You <em>are</em> going to pick the wrong <code>Assets</code>
tag out of twenty-three candidates, and the identity catches that every
time.</p>

<p>Across {COMPANIES} companies the reconciliation closes on about
<strong>every valid filing</strong>. The exceptions are flagged with the reason rather
than quietly adjusted — <a href="/methodology">the methodology page</a> lists
which reasons, and <a href="/company/WMT">WMT</a> and
<a href="/company/FCX">FCX</a> are ordinary worked examples if you want to see
a closed identity drawn.</p>
""",
)


# Newest first: /blog lists them in this order, and a reader arriving at
# the hub should meet the most recent thinking rather than the oldest.
_POST_EDGAR_PIPELINE = Post(
    slug="build-scalable-sec-edgar-pipeline",
    title="How to Build a Scalable SEC EDGAR Pipeline Under the 10 RPS Limit",
    seo_title="SEC EDGAR API Rate Limits: A Python Pipeline Under 10 RPS",
    description=(
        "SEC EDGAR blocks you for ten minutes if you exceed 10 requests/sec. "
        "Here's the token-bucket setup and bulk loads that actually work."
    ),
    summary=(
        "Three IP bans, and none of them were going too fast. The "
        "User-Agent rule, why time.sleep(0.1) stops working the moment you "
        "add a second worker, and the bulk loads that make the rate limiter "
        "almost irrelevant."
    ),
    published="2026-09-12",
    updated="2026-09-12",
    minutes=8,
    body="""
<p class="lede">SEC EDGAR banned my IP three times while I was building BalanceProof.
Not rate-limited. Banned.</p>

<p>Each one arrived the same way: a run that had been going fine for twenty
minutes started returning 403 on every request, including the ones that had
worked a second earlier. No <code>Retry-After</code>, no JSON error body, no
429. Just a wall of HTML telling me my access had been suspended, and a ten
minute wait before anything worked again.</p>

<p>All three had different causes, and none of them were "I was going too
fast". Here is what actually breaks a pipeline against EDGAR, in the order it
broke mine.</p>

<h2>The limit is 10 requests per second, and it is not the hard part</h2>

<p>SEC publishes a <a href="https://www.sec.gov/about/webmaster-frequently-asked-questions"
rel="noopener">fair access policy</a>: no more than ten requests per second,
across all of <code>sec.gov</code> and <code>data.sec.gov</code>, per
requester. Exceed it and you are blocked for ten minutes.</p>

<p>Ten per second is generous. The entire public filer universe is a few
thousand companies. At ten per second you can walk all of them in under ten
minutes. The reason people still get banned is that "ten per second" is easy
to say and surprisingly hard to actually guarantee.</p>

<h2>Ban one: no User-Agent</h2>

<p>SEC requires a descriptive <code>User-Agent</code> carrying a real contact
address. This is not advisory. A request that arrives with
<code>python-requests/2.31.0</code> on it gets a 403, and the 403 looks
exactly like the rate limit 403, which is how I spent an afternoon tuning a
delay that was never the problem.</p>

<pre class="code"><code class="language-python"># The header is the difference between a working client and a 403.
# Put a real address in it. Someone at SEC will use it if your
# crawler misbehaves, which is better than being cut off silently.
HEADERS = {
    "User-Agent": "BalanceProof dominique@example.com",
    "Accept-Encoding": "gzip, deflate",
}

r = httpx.get(
    "https://data.sec.gov/submissions/CIK0000019617.json",
    headers=HEADERS,
    timeout=30,
)</code></pre>

<p>Set it at client construction and make the constructor raise if it is
missing. A default that quietly falls back to the library's own User-Agent is
a 403 you will debug at some point, probably at the least convenient moment.</p>

<h2>Ban two: time.sleep(0.1) is not a rate limiter</h2>

<p>This is the line almost everyone writes first, including me:</p>

<pre class="code"><code class="language-python">for cik in ciks:
    fetch(cik)
    time.sleep(0.1)   # "10 per second"</code></pre>

<p>It holds exactly as long as you have one worker. The moment you add a
second process, or an async gather, or a retry that fires while the main loop
is still going, each one sleeps its own 0.1 seconds and the combined rate is
whatever you multiplied by. Four workers at "10 per second" is 40 per second,
and 40 per second is a ban.</p>

<p>It also paces the wrong thing. <code>sleep(0.1)</code> after a request that
took 800ms means you made one request in 900ms, so you are running at roughly
one per second and the crawl that should take ten minutes takes two hours. The
sleep punishes you for slow responses and does nothing about fast ones.</p>

<h2>What a shared limiter looks like</h2>

<p>The fix is a token bucket that lives outside the process, so every worker
draws from one budget. Redis with a Lua script does this in a few lines, and
the script matters: reading the bucket, computing the refill and writing it
back has to be one atomic operation, or two workers read the same token count
and both spend it.</p>

<pre class="code"><code class="language-python"># One bucket per source, shared by every worker.
#
# `rate` is the sustained refill in tokens/second and `burst` is the
# capacity. Running at 9/sec against a 10/sec ceiling leaves room for
# the clock skew between your box and theirs, which is the margin
# that stops a "compliant" crawler tripping the limit anyway.
LUA = '''
local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local ts     = tonumber(redis.call('HGET', KEYS[1], 'ts'))
local rate, burst, now = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
if tokens == nil then tokens, ts = burst, now end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
return tostring(wait)
'''

async def acquire(source):
    while True:
        wait = float(await redis.eval(LUA, 1, f"bucket:{source}", 9.0, 10, time.time()))
        if wait &lt;= 0:
            return
        await asyncio.sleep(wait)</code></pre>

<p>Two things I would do differently if I were starting again. Give
<code>acquire</code> a maximum wait and raise past it, so a misconfigured
bucket stalls one stage instead of hanging the whole run. And fall back to a
process local bucket when Redis is unreachable, so local development does not
require a Redis container to make one request.</p>

<h2>The requests you do not make</h2>

<p>Pacing is the small win. The large one is not sending the request at all.</p>

<p><strong>companyfacts returns the whole history.</strong> The endpoint at
<code>data.sec.gov/api/xbrl/companyfacts/CIK##########.json</code> hands back
every XBRL fact that company has ever filed, for every period, in one
response. If you are looping over quarters and fetching each one, you are
making forty requests for a payload you already had after the first. Pull it
once, cache it, serve every question about that filer out of the cache.</p>

<p><strong>The bulk datasets replace the crawl entirely.</strong> SEC publishes
<a href="https://www.sec.gov/dera/data/financial-statement-data-sets"
rel="noopener">Financial Statement Data Sets</a>: one ZIP per quarter holding
<code>sub.txt</code> (submission metadata) and <code>num.txt</code> (every
numeric XBRL fact filed that quarter), tab separated, joined on the accession
number. That is one download instead of one request per company per concept.
For seeding history it is not a marginal improvement, it is a different order
of magnitude, and it is how I load fundamentals now.</p>

<pre class="code"><code class="language-bash"># One file. Every numeric fact filed in that quarter, every filer.
curl -H "User-Agent: You you@example.com" -O \\
  https://www.sec.gov/files/dera/data/financial-statement-data-sets/2026q1.zip

unzip -p 2026q1.zip num.txt | head -3
# adsh              tag      version   ddate     qtrs  uom  value
# 0000019617-26-...  Assets   us-gaap/2026  20260331  0   USD  4210000000000</code></pre>

<p><strong>Stay current from the feed, not from a re-crawl.</strong> Once
history is seeded, the only thing you need is what changed. The current-events
Atom feed lists filings as they land, so an incremental job is a handful of
requests a day rather than a full sweep:</p>

<pre class="code"><code class="language-bash">curl -H "User-Agent: You you@example.com" \\
  "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&amp;type=10-Q&amp;output=atom"</code></pre>

<p>Between the bulk seed and the feed, the steady state load on SEC is small
enough that the rate limiter almost never has to say no. Which is the actual
goal: the limiter is a seatbelt, not an engine.</p>

<h2>Ban three was not a rate limit at all</h2>

<p>The third one taught me the thing I did not expect. I had the limiter
working, the bulk seed loading, the feed running. And the numbers were wrong.</p>

<p>I pulled JPMorgan's balance sheet and got a total assets figure that was
too small by a lot. Not corrupted, not truncated. Just a different real
number.</p>

<p>XBRL lets a filer attach dimensions to a fact: this figure, but for this
segment, this legal entity, this geography. JPMorgan reports
<code>Assets</code> twenty-three times in one filing. Twenty-two of them are
segments and subsidiaries. The consolidated figure, the one on the face of the
balance sheet, is the one with <em>no</em> dimensions attached. So the number
you want is defined by an absence, and taking the first match gets you a
segment with nothing anywhere telling you so.</p>

<p>The fix is arithmetic rather than heuristics. A balance sheet balances:</p>

<pre class="code"><code class="language-python"># Every candidate for each concept, dimensioned ones included.
# Exactly one combination satisfies A = L + E, and it is the
# consolidated one. A segment's assets do not balance against the
# whole company's liabilities.
def reconcile(assets, liabilities, equity, tolerance=0.005):
    best = None
    for a in assets:
        if a &lt;= 0:
            continue
        for lia in liabilities:
            for eq in equity:
                gap = abs(a - (lia + eq)) / a
                if gap &lt;= tolerance and (best is None or gap &lt; best[0]):
                    best = (gap, a, lia, eq)
    if best is None:
        return None          # it does not balance: say so, do not guess
    _, a, lia, eq = best
    return {"total_assets": a, "total_liabilities": lia, "total_equity": eq}</code></pre>

<p>The tolerance is a fraction and not a constant, because filers round and a
fixed dollar tolerance either rejects every bank or accepts anything at a
small cap. And when nothing balances, return nothing. The closest match is the
tempting answer and it is the wrong one: a number that is wrong by a segment
is worse than no number, because you will never go back and audit the one you
were handed.</p>

<h2>Build it or buy it</h2>

<p>Build it if the pipeline is the point, or if you need something specific
enough that no general dataset will carry it. Everything above is a weekend of
work plus however long it takes to discover the parts nobody writes down,
which for me was about three bans and a month of wrong numbers.</p>

<p>Do not build it if you want balance sheets. I did, and the result is
<a href="/">BalanceProof</a>: the reconciler above running over every filing, with
the result behind an API. Every figure is checked against A = L + E before it
is stored, and the filings that genuinely do not balance are flagged as such
rather than quietly adjusted. Across {COMPANIES} companies and {FACTS} data
points, that check is the whole product.</p>

<p>The <a href="/methodology">methodology page</a> shows what is checked and
what is still failing, with live counts rather than a claim. The
<a href="/pricing">pricing page</a> has the tiers, and the free one needs no
card. If you are comparing options,
<a href="/best/sec-filings-api-for-quants">the SEC filings APIs worth
considering for quant work</a> covers the alternatives, mine included.</p>

<p>Either way, run the reconciliation. If you build your own pipeline and it
does not check that assets equal liabilities plus equity, you do not have a
data quality problem yet. You have one you cannot see.</p>
""",
)


_POST_DUPLICATE_TAGS = Post(
    slug="sec-xbrl-duplicate-tags",
    title="Why Your SEC Filing Data Is Wrong (And How to Check)",
    seo_title="SEC XBRL Duplicate Tags: How to Check Your Data",
    description=(
        "One filing reports the same figure up to 23 times. Why picking the "
        "first XBRL tag fails silently, and a 5-minute test to check your data."
    ),
    summary=(
        "The consolidated figure in an XBRL filing is the one with no "
        "dimensions attached, so the right answer is defined by an absence. "
        "A five minute test you can run against your own data to find out "
        "which one you have."
    ),
    published="2026-09-12",
    updated="2026-09-12",
    minutes=6,
    body="""
<p class="lede">The first filing that broke my parser was American Airlines.
The code returned total equity as a positive number. The actual figure is
negative, and has been for years.</p>

<p>Nothing errored. No exception, no warning, no null. The parser found a tag
called <code>StockholdersEquity</code>, read a plausible dollar figure out of
it, and handed it back. It was a real number from a real filing. It was just
the wrong one: a subsidiary's equity rather than the consolidated deficit that
belongs on the face of the balance sheet.</p>

<p>That is the failure mode worth understanding, because it is silent. Bad SEC
data almost never arrives as a crash or an obviously broken figure. It arrives
as a number that looks exactly like the number you wanted.</p>

<h2>Why one filing contains the same figure many times</h2>

<p>XBRL lets a filer attach dimensions to a fact. The same concept, tagged
repeatedly, each instance qualified: this figure but for the consumer segment,
this one for the investment bank, this one for a named subsidiary, this one
for a geography.</p>

<p>That is a good design. A bank's consumer arm and its investment arm are
genuinely different businesses, and flattening them into a single line would
throw away information somebody needs.</p>

<p>The problem is how the consolidated figure is distinguished from the rest.
It is not flagged. It is not first. It does not carry a label saying
"consolidated". It is the instance with <em>no</em> dimensions attached. The
number you want is defined by an absence.</p>

<p>JPMorgan reports <code>Assets</code> twenty-three times in a single filing.
Twenty-two of those are dimensioned. One is not, and that one is the bank. If
your code takes the first <code>Assets</code> fact it finds, it gets whichever
one the document happens to list first, and nothing tells it that a choice was
made at all.</p>

<h2>Why "take the first tag" fails more than it looks like it should</h2>

<p>The reason first-match survives casual testing is that it is right for
simple filers. A single-segment company reports <code>Assets</code> once, and
the first match is the only match. Test against a handful of mid-cap
industrials and the approach looks fine.</p>

<p>It breaks on exactly the filings that matter most. Banks, insurers,
conglomerates, REITs with joint ventures, anything with a captive finance arm,
anything post-acquisition that still reports the acquired entity separately.
The companies with the most segments are also the companies people most want
data on, so the error rate on the filings you care about is higher than the
error rate across the filer universe.</p>

<p>I am not going to put a percentage on it. Any figure I could quote would
depend on which companies were in the sample, which quarters, and how I
counted a partial match, and a number with those caveats stripped off becomes
a marketing claim rather than a measurement. The failure mode is the point:
picking by position picks a segment, and picking a segment is silent.</p>

<h2>A five minute test you can run on your own data</h2>

<p>You do not have to take my word for any of this. Pick a company you already
hold figures for and ask SEC directly. The companyfacts endpoint returns every
XBRL fact a filer has ever reported, with dimensions intact:</p>

<pre class="code"><code class="language-bash"># JPMorgan. The CIK is zero-padded to ten digits.
curl -H "User-Agent: You you@example.com" \\
  https://data.sec.gov/api/xbrl/companyfacts/CIK0000019617.json \\
  -o jpm.json</code></pre>

<pre class="code"><code class="language-python">import json
from collections import Counter

facts = json.load(open("jpm.json"))
units = facts["facts"]["us-gaap"]["Assets"]["units"]["USD"]

# Group by period end. Anything with more than one entry for a single
# period is the same concept reported at several levels at once.
per_period = Counter(f["end"] for f in units)
for end, n in sorted(per_period.items())[-4:]:
    print(end, n, "reported values")
    for f in units:
        if f["end"] == end:
            print("   ", f"{f['val']:>20,}", f.get("frame", "(dimensioned)"))</code></pre>

<p>Run it and you will see several figures for one date. Then check which one
your current data source gave you. If it matches the largest, you are probably
fine on that filing. If it matches something else, you have found the bug, and
you have found it on one company out of however many you are carrying.</p>

<h2>The check that actually resolves it</h2>

<p>Heuristics do not fix this. "Take the largest" fails on a company whose
parent is smaller than a consolidated subsidiary line. "Take the one without
dimensions" is closer but depends on every filer tagging cleanly, which they
do not.</p>

<p>Arithmetic fixes it. A balance sheet balances, and that gives you a test
rather than a guess:</p>

<p class="pull">Assets = Liabilities + Equity</p>

<p>Pull every candidate for assets, every candidate for liabilities, every
candidate for equity, and find the combination that reconciles. Consolidated
figures balance against each other. A segment's assets do not balance against
the whole company's liabilities. The arithmetic identifies the right set
without needing to know anything about the company.</p>

<p>Three adjustments make it hold in the real world.</p>

<p><strong>Noncontrolling interests belong in equity.</strong> When a parent
consolidates a subsidiary it does not wholly own, the outside shareholders'
stake sits in equity as NCI. <code>StockholdersEquity</code> excludes it;
<code>StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest</code>
includes it. Use the wrong one and the identity misses by exactly the minority
stake, which reads as a reconciliation failure when the filing is fine.</p>

<p><strong>Mezzanine is neither, and it is not optional for SPACs.</strong>
Redeemable preferred and redeemable NCI sit between liabilities and equity on
the face of the sheet. A SPAC with shares subject to possible redemption
carries most of its balance sheet there. Ignore the mezzanine line and the
identity fails on every one of them.</p>

<p><strong>The tolerance is a fraction, not a constant.</strong> Filers round.
Half a percent of assets absorbs that at any size. A fixed dollar tolerance
either rejects every large bank or accepts anything at a small cap.</p>

<h2>When it still does not balance</h2>

<p>Sometimes no combination reconciles. The tempting move is to return the
closest match, and it is the wrong one. A figure that is wrong by a segment is
worse than a missing figure, because a gap gets investigated and a plausible
number does not.</p>

<p>So I do not hide the failures, I name them. A filing that does not
reconcile is drawn on the site with a red warning saying so, rather than
quietly adjusted until it agrees. If a company filed something that does not
add up, that is a fact about the company and it should reach you as one. The
same goes for missing components: if a filer does not break out receivables,
you get a labelled remainder rather than a zero, because a zero is a claim and
"they did not say" is not.</p>

<p>This is also why I publish the method and not a rate. The
<a href="/methodology">methodology page</a> shows what is checked, how, and
what is currently failing, with live counts read off the database rather than
a figure written down once. A single accuracy percentage is the easiest thing
in the world to quote and the hardest to verify, and I would rather hand you
something you can check.</p>

<h2>What to do with this</h2>

<p>Run the companyfacts test above against three companies you have data for.
Pick a bank, a REIT and something with a recent acquisition, because those are
where it breaks. If the figures agree, your source is doing the reconciliation
and you can stop worrying about it. If they do not, you now know which
direction the error goes.</p>

<p>If you would rather not maintain that yourself,
<a href="/">BalanceProof</a> runs the check above over every filing before storing
anything, across {COMPANIES} companies and {FACTS} data points. You can look
up any company on the site with no key and no account. The
<a href="/compare/balanceproof-vs-intrinio">comparison with Intrinio</a> covers how
that differs from a general-purpose financial data feed, and
<a href="/pricing">pricing</a> has the tiers.</p>

<p>And if you find a figure that disagrees with the filing, tell me. That is
the bug report I actually want.</p>
""",
)


# Newest first. The index renders in this order and so does the sitemap, so
# the order here is the editorial decision rather than a detail of the loop.
_POST_WHAT_I_GOT_WRONG = Post(
    slug="what-i-got-wrong-about-sec-filings",
    title="What I Got Wrong About SEC Filings",
    seo_title="What I Got Wrong About SEC Filings and XBRL Data",
    description=(
        "I deleted 1,231,927 rows because a reload script deleted before it "
        "downloaded. What that taught me about checking SEC XBRL data."
    ),
    summary=(
        "A reload script that deleted before downloading, an SEC rate limit, "
        "and an empty database with a zero exit code. Why financial data "
        "failures do not announce themselves, and the fetch-then-replace "
        "pattern that fixed it."
    ),
    published="2026-09-14",
    updated="2026-09-14",
    minutes=6,
    body="""
<p class="lede">I deleted 1,231,927 rows of production data at 2 AM. Here is how.</p>

<p>The reload script had one job. Replace the fundamentals table with a fresh
copy of the last seven quarters of SEC Financial Statement Data Sets. It had
run cleanly a dozen times. That night it ran in this order: delete every row,
then download the quarters, then parse them in.</p>

<p>SEC started returning 429 on the second quarter it asked for. Rate limited.
The download loop caught the error, logged it, and moved on to the next
quarter, which also came back 429. So did the rest. The script finished
without raising anything. The table was empty and the process exit code was
zero.</p>

<h2>Nothing announced itself</h2>

<p>Here is the part worth sitting with. Every individual step behaved
correctly. The delete worked. The HTTP client correctly identified a 429 and
correctly declined to hammer a government server that had just asked it to
stop. The logging wrote a line for each failure. The script exited cleanly
because, as far as it knew, it had done what it was told.</p>

<p>What was missing was any statement about what the database was supposed to
look like at the end. There was no step that said: this table should have
roughly a million rows in it, and if it has zero, something went wrong. So a
total data loss and a successful run produced the same output.</p>

<p>That is the shape of almost every financial data failure I have run into
since. It is not a crash. A crash is a gift. A crash tells you where to look
and stops the bad value from reaching anyone. The failures that cost you are
the ones where a wrong number and a right number are the same data type, the
same order of magnitude, and arrive through the same code path.</p>

<h2>The fix that stuck</h2>

<p>The rule I came out with is narrow and has not needed revising. Never
delete anything until the replacement is already on disk.</p>

<p>The reload now runs in three phases. Download every quarter to a local
cache first. A 404 is fine, because the newest quarter is often a few weeks
from being published and asking for it early is normal. Any other failure
aborts immediately, before a single row has been touched. Only once every
quarter is sitting on disk does the delete happen, and it happens inside the
same transaction as the write.</p>

<pre class="code"><code>async def reload_fundamentals(quarters: int = 7):
    # 1. Everything obtainable, before anything is destroyed.
    #    A failure here raises with the table still intact.
    staged = await prefetch_quarters(recent_quarters(date.today(), quarters))

    # 2. Delete and reload inside ONE transaction, parsing from disk only.
    with session_scope() as session:
        before = session.execute(select(func.count()).select_from(Fundamental)).scalar_one()
        session.execute(delete(Fundamental))

        written = 0
        for year, q in staged:
            rows = extract(sec_cache.cached_bytes(year, q))
            written += save_fundamentals(session, rows)

        if written == 0:
            # Committing here would swap a million rows for nothing at all.
            raise RuntimeError("extracted 0 rows; the table was left untouched")
    # 3. Verify against known companies before calling it done.
    return before, written</code></pre>

<p>The <code>if written == 0</code> check is the line that would have saved
me. It is four lines of code and it encodes the thing the original script
never said out loud: I know what the end state should look like, and I will
refuse to commit one that is obviously wrong.</p>

<p>The transaction boundary is doing the other half of the work. If parsing
quarter five throws, the delete of the first four rolls back with it. There is
no window where the table is half a dataset. It is either the old copy or the
new one.</p>

<h2>The same pattern, one level up</h2>

<p>Once the pipeline stopped losing data, the same question applied to the
data itself. A filing that parses without error is not a filing that parsed
correctly. SEC XBRL gives you plenty of ways to read a plausible wrong number.
One filing can report the same concept under a dozen tags, and picking the
first one you find gets you a subsidiary's figure instead of the consolidated
one. It is a real number from a real filing. It is just not the number on the
face of the balance sheet.</p>

<p>So the reload does not get to declare success on its own either. Every
filing gets checked against the accounting identity. Assets equals liabilities
plus equity. That identity is not a heuristic or a tolerance I picked. It is
the definition of a balance sheet, and it holds on data I did not produce,
which is exactly what makes it useful as a test. If the figures I extracted do
not satisfy it, then at least one of them is wrong, and I do not need to know
which one to know that.</p>

<p>When a filing does not balance, it gets published with the reason it does
not, rather than quietly dropped or nudged into agreement. <a
href="/methodology">The full method is written up here</a>, including what
counts as a reconciling difference and what does not.</p>

<h3>The counts, as they stand</h3>

<p>6,222 companies in the database. 4,911 of them reconcile directly against
the identity. 215 are flagged with the reason they do not. 0 are hidden. The
remainder are filings that do not report enough of a balance sheet to check,
and those say so on the page rather than being counted as passes.</p>

<p>I publish counts and not a percentage, deliberately. A percentage invites
you to read one number and stop. The counts make you ask what is in each
bucket, which is the question that actually tells you whether the data is fit
for what you are doing with it. You can see the whole thing on any company
page. <a href="/company/JPM">JPMorgan Chase is a good one to start with</a>,
because a bank's balance sheet looks nothing like the industrial shape most
people picture.</p>

<h2>What to ask whoever sells you filings data</h2>

<p>If you are buying SEC data from anyone, including me, there is one question
worth more than the rest. Ask which filings fail their checks, and ask to see
the list.</p>

<p>A provider who has never looked will tell you their coverage is complete.
A provider who has looked will be able to tell you how many filings do not
balance and why, because they had to make a decision about each one. The
second answer is less comfortable and considerably more useful. Silence on
this is not evidence that everything reconciles. It is usually evidence that
nobody checked.</p>

<p>Every figure on this site is as filed. Nothing is estimated, smoothed, or
restated, and where a filing and the identity disagree, the disagreement is
what gets shown. <a href="/pricing">The API and the bulk dataset are priced
here</a>, and looking things up on the site stays free.</p>
""",
)


# Newest first, and this one is the other half of the duplicate-tag
# posts: those are about picking the right figure, this is about what a
# gap MEANS once the right figures are in hand.
_POST_FIVE_FAILURES = Post(
    slug="five-ways-a-balance-sheet-fails",
    title="The Five Ways a Balance Sheet Fails the Identity Check",
    # 48 characters. The headline names the number because the number is the
    # point of the piece; the title tag names the SEARCH, which is somebody
    # typing the symptom rather than the taxonomy.
    seo_title="SEC Filing Does Not Balance: The Five Reasons Why",
    description=(
        "A filing that fails A = L + E fails in one of five ways. "
        "Noncontrolling interests, mezzanine equity, rounding, a missing "
        "XBRL tag, or a genuine error."
    ),
    summary=(
        "Once a reconciler runs over every filing the failures stop looking "
        "random. Five mechanisms account for nearly all of them, two are "
        "passes wearing a failure's clothes, and one of the five has never "
        "once turned up in this dataset."
    ),
    published="2026-09-16",
    updated="2026-09-16",
    minutes=8,
    body="""
<p class="lede">Run A = L + E over every filing you hold and the failures stop
looking random inside a day.</p>

<p>They cluster. Five mechanisms account for nearly all of them, and they are
not variations on one problem. Two are filings that balance perfectly well once
read on the terms they were written on. One is noise, one is a gap in your own
extraction, one is a company that filed something wrong. Four different
responses.</p>

<p>The response that ruins the exercise is to take whichever candidate figure
comes closest to closing the gap and return it. That turns a wrong number into
a wrong number nobody can audit: the discrepancy that would have told you it
was wrong is the thing you just closed.</p>

<h2>1. A tag the extractor did not read</h2>

<p>The components do not add up because one is missing from your data rather
than from the filing: a company-specific extension tag, a concept from a
taxonomy version your mapping predates, or an ordinary us-gaap element nobody
thought to map. The filing is complete. Your copy of it is not.</p>

<p><strong>Detection.</strong> The gap is far larger than rounding, and the
filing's raw facts contain a line that never reached your reconstructed
right-hand side. The decisive version of that test needs one more field, and it
gets its own section below: the same field separates this category from the
last one.</p>

<p><strong>Flag it as a missing tag, name the ticker, and do not pick a
substitute.</strong> A near-enough figure in place of an unread one is wrong,
plausible, and erases the evidence that anything was unread.</p>

<p>BlackRock is the ordinary example in my data: a redeemable noncontrolling
interest in a form my mapping still does not reach, so liabilities plus equity
falls short and the filing is flagged rather than filled in. Note that the line
I am failing to read is itself a mezzanine line. The category is about whose
fault a gap is, not about which row it sits on.</p>

<h2>2. Noncontrolling interests</h2>

<p>A parent that owns most of a subsidiary consolidates all of it. Every asset
and liability lands in the parent's totals at full value, because the parent
controls them. The slice it does not own is not netted out of assets; it sits
on the claims side as a noncontrolling interest.</p>

<p>So a consolidated filing is written on <code>A = L + E + NCI</code>, and
testing <code>A = L + E</code> against parent-only equity misses by exactly the
minority stake.</p>

<p><strong>Detection.</strong> The gap matches the reported
<code>MinorityInterest</code> or
<code>StockholdersEquityAttributableToNoncontrollingInterest</code> to within
the tolerance. Not approximately. Exactly, which is what makes this one safe to
act on.</p>

<p><strong>Reconcile on A = L + E + NCI and record that basis.</strong> This is
a pass. The filing was never wrong.</p>

<p>Most filers hand you the answer already summed:
<code>StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest</code>
carries the minority interest inside it, and a filing that publishes it
balances plainly. So the category only fires where a filer reports parent
equity and the NCI as two lines with no combined total, which in the universe I
cover is rare enough to count on one hand. It is nearly empty because the
common case is settled a step earlier.</p>

<h2>3. Mezzanine equity</h2>

<p>Redeemable preferred stock, redeemable noncontrolling interests and shares
subject to possible redemption sit between the liabilities and equity sections,
in a band of their own. The instrument can be required to be redeemed, which is
debt-like, while carrying none of the fixed obligation that makes debt debt.
Genuinely neither, so it is shown as neither.</p>

<p>The identity that filing was written on is
<code>A = L + E + Mezzanine</code>.</p>

<p><strong>Detection.</strong> The gap matches the filer's temporary equity or
redeemable preferred line:
<code>TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests</code>
and its parent-only and plain variants,
<code>RedeemablePreferredStockCarryingAmount</code>, or
<code>RedeemableNoncontrollingInterestEquityCarryingAmount</code>.</p>

<p><strong>Reconcile with the mezzanine included, and say so.</strong> Also a
pass.</p>

<p>One trap sits inside that detection. Temporary equity is the section total
and redeemable preferred is a component of it, so adding both overshoots and
breaks a filing that balanced. Prefer the total. Never add a section to its own
parts.</p>

<p>KKR is the live example: a filing that does not close on A = L + E and does
close once the redeemable block is added, with the drawing naming the basis. It
matters most, though, on the blank-cheque company. A pre-merger SPAC holds a
trust account as its only real asset and carries most of the
shareholder money as Class A stock subject to possible redemption, which is
temporary equity. Drop the mezzanine line there and the arithmetic does not
miss by a little: liabilities plus permanent equity comes out a rounding error
beside total assets, reading like an extractor that has failed outright rather
than one absent term.</p>

<h2>The classifier, in full</h2>

<p>Three of the five are in view, so here is the whole decision: the three
totals, the two extra terms where the filer published them, and one field the
last two categories turn on.</p>

<pre class="code"><code class="language-python">TOLERANCE = 0.005      # half a percent of assets
ROUNDING_BAND = 0.01   # and one percent is the outer edge of noise

def classify(assets, liabilities, equity,
             nci=None, mezzanine=None, stated_rhs=None):
    # Returns (verdict, basis). A term is admitted only where the filer
    # published it, and only after the plain sum has already failed:
    # adding a term to a filing that balances breaks one that was right.
    if assets &lt;= 0:
        return "not testable", None

    def gap(extra=0.0):
        return abs(assets - (liabilities + equity + extra)) / assets

    if gap() &lt;= TOLERANCE:
        return "balances", None

    # The two terms the plain identity does not carry. Try each and both,
    # and let the TIGHTEST fit win rather than the first to clear: where
    # two bases close, the tighter one is the shape the filer used.
    bases = []
    if nci:
        bases.append(("noncontrolling interests", nci))
    if mezzanine:
        bases.append(("mezzanine equity", mezzanine))
    if nci and mezzanine:
        bases.append(("both", nci + mezzanine))

    closed = sorted((gap(x), name) for name, x in bases if gap(x) &lt;= TOLERANCE)
    if closed:
        return "balances", closed[0][1]

    if gap() &lt;= ROUNDING_BAND:
        return "rounding", None

    # Still open, and whose fault it is turns on one field: the total the
    # filer printed at the foot of the claims column.
    if stated_rhs is None:
        return "unexplained", None
    if abs(assets - stated_rhs) / assets &gt; TOLERANCE:
        return "genuinely broken filing", None
    return "missing tag", None</code></pre>

<p>Every branch returns a name. None returns an adjusted figure, and that is
the property worth keeping if you write your own.</p>

<h2>4. Rounding</h2>

<p>Filers report in thousands or millions, and the rounding happens before the
totals are struck rather than after. A balance sheet a few hundred billion
dollars deep can show a one-unit difference between its two sides and be
entirely correct.</p>

<p><strong>Detection.</strong> The gap is small against total assets and
nothing else accounts for it: no noncontrolling interest line, no mezzanine
line, no shortfall against the filer's own stated total.</p>

<p><strong>Pass it, and record the tolerance that let it pass.</strong> The
second half is the part worth arguing about. A tolerance has to be a fraction
of assets rather than a number of dollars: one calibrated on a mid-cap rejects
every large bank, and one calibrated on a large bank waves through a genuine
error at a small company. Half a percent of assets absorbs presentation
rounding at any size.</p>

<p>I am not naming a current member. The category holds a handful of filings
and turns over with every ingest, so a ticker printed here would be stale
before you checked it.</p>

<h2>5. A filing that genuinely does not balance</h2>

<p>The company made an error. It should be rare for an audited public filing,
and the interesting part is proving it rather than assuming.</p>

<p><strong>Detection is one field.</strong>
<code>LiabilitiesAndStockholdersEquity</code> is the filer's own stated
right-hand side, the total printed at the foot of the claims column. Where they
publish it, two questions are available instead of one.</p>

<p><em>Does the filing balance against its own stated total?</em> Assets
against the stated right-hand side. If those two disagree, the filer's
arithmetic disagrees with itself, and no extractor repairs that.</p>

<p><em>Did you recover everything the filing put there?</em> The stated total
against your reconstructed liabilities plus equity. If the filing balances
against itself and your sum falls short, the shortfall is a line you did not
read: category one, and yours.</p>

<p>That single field separates a missing tag from a broken filing, and without
it neither claim is available. Where a filer publishes no stated total the
honest label is <strong>unexplained</strong>, because assigning it to whichever
bucket reads better is how a classification stops being a measurement.
Business development companies and commodity trusts land here most often.</p>

<p><strong>Flag the filing. Say what is wrong. Do not adjust.</strong></p>

<p>Then the honest part. Across three separate measurements of every testable
filing I hold, the count of genuinely broken filings has been zero each time.
Zero is the right answer for audited public companies, and worth stating
precisely because it is the outcome most worth measuring: every gap that looked
like a filer's error turned out to be a term I was not reading. So I have no
real example to show you, and inventing one would undo the point of the
category.</p>

<h2>What the classification is worth</h2>

<p>It turns a percentage into a list.</p>

<p>"Ninety-nine percent of filings reconcile" tells you nothing you can act on.
You do not know whether the remainder is a kind of company you hold, or
whether those failures are the provider's extraction or the filers' arithmetic.
The number asks to be believed rather than read.</p>

<p>"Three hundred filings failed, here are the tickers, and here is the reason
for each" tells you which of your positions are affected and which of the
failures deserve any attention at all. A rounding flag on a company you hold is
nothing. A missing-tag flag on the same company means a component of that
balance sheet is absent from your data, and you know to go and read the filing
yourself.</p>

<p>Two questions to put to a data provider follow directly. Which filings
failed? And why did each one fail?</p>

<p>Without the first, the output cannot be audited at all. Every figure arrives
carrying the same implied confidence, and the wrong ones look exactly like the
right ones. Bad financial data shows up not as a crash but as a plausible
number from a real filing, which is the argument in
<a href="/blog/sec-xbrl-duplicate-tags">the companion note on duplicate
tags</a>.</p>

<p>Without the second, the extraction cannot be fixed either. A failure with no
category is a filing somebody inspects by hand once and never again. A failure
labelled "mezzanine" is a mapping to write, and writing it closes several
hundred filings instead of one. A bucket that fills up is a specification.</p>

<h2>How this runs on BalanceProof</h2>

<p>Every filing is reconciled before it is stored. The plain identity first;
where that does not close, the noncontrolling interest and the mezzanine block
are tried singly and together, and whichever basis closes with the smallest
remaining gap is the one recorded. The company page names that basis in words,
because <em>this balances once the minority interest is included</em> is a
different statement from <em>this balances</em>.</p>

<p>Anything that does not close keeps a category: missing tag, rounding,
genuinely broken, or unexplained where there is no stated total to referee
with. The <a href="/methodology">methodology page</a> carries the count for
each, read off the database when the page renders rather than written down
once. There is no accuracy rate on it and there is not going to be: the
denominator moves every time coverage improves, so a number that falls as the
data gets better is not measuring the data.</p>

<p>Figures are served as reported. A filing that does not balance is drawn with
the gap shown and the reason named rather than adjusted until the columns
agree. <a href="/company/JPM">JPM</a> is the worked example worth opening
first: twenty-three Assets tags, one consolidated set among them, an identity
that closes. Looking a company up needs no account, and
<a href="/pricing">pricing</a> covers the API and the bulk dataset.</p>

<h2>If you find a sixth</h2>

<p>Run the classification over your own data. It needs three totals per filing,
the filer's stated right-hand side where published, and the two extra terms.
The function above is the whole of it.</p>

<p>Then look at what does not fit. A filing whose failure is none of these five
is the interesting result, and worth more to me than another confirmation of
the categories I have. These five are the residue of what has broken so far,
which is exactly the kind of list that stays incomplete without anybody
noticing. Send me the ticker and the period and I will look.</p>
""",
)


_POST_TOTAL_LIABILITIES = Post(
    slug="total-liabilities-from-sec-edgar",
    title="Total Liabilities from SEC EDGAR: Why the Obvious Tag Is Often Empty",
    # 54 characters. The headline above keeps "Obvious" because the whole
    # point is that the obvious move fails; in a result list the reader has
    # not made the move yet, so the word is doing nothing and the length is.
    # 57 characters, and "API" in both fields since 2026-09-23: the queries
    # reaching this post are commercial -- "free api with total liabilities",
    # "api with total assets and total liabilities" -- and neither field said
    # API or free.
    seo_title="Total Liabilities from the SEC EDGAR API: Why It Is Empty",
    description=(
        "Coca-Cola, Amazon and Walmart report no us-gaap:Liabilities tag. Why "
        "it is missing, how to derive it, and a free API that returns it "
        "reconciled."
    ),
    summary=(
        "Ask EDGAR for us-gaap:Liabilities and a large share of filers "
        "return nothing, Coca-Cola and Amazon among them. The tag is not "
        "missing data: it is a subtotal those companies never printed."
    ),
    published="2026-09-21",
    updated="2026-09-21",
    minutes=7,
    body="""
<p class="lede">You want total liabilities for a company. EDGAR publishes XBRL
facts for free, there is a <code>us-gaap</code> element called
<code>Liabilities</code>, and the request is three lines of Python. You run it
over a few hundred tickers and a large share of them come back with nothing at
all.</p>

<p>The tag is right. The request is right. The assumption underneath it is
wrong. XBRL does not contain every figure you can compute from a balance sheet.
It contains the figures the filer actually presented. If a company's balance
sheet never prints a line reading "Total liabilities," there is no fact to tag,
and the element is simply absent.</p>

<p>This is not an edge case and it is not a data quality problem at EDGAR. Ask
the company concept endpoint for <code>Liabilities</code> at
<a href="/company/KO">Coca-Cola</a>, <a href="/company/AMZN">Amazon</a> or
Walmart and all three return a 404. Ask it for
<code>LiabilitiesAndStockholdersEquity</code> and all three return a figure.
<a href="/company/AAPL">Apple</a> happens to publish both. Nothing about the
three that do not is unusual — they run from the last liability line into the
equity section without printing a subtotal on the way.</p>

<h2>What the endpoint gives you</h2>

<p>Company facts is one call, no key, no auth. The CIK is zero-padded to ten
digits, so Apple's 320193 becomes <code>CIK0000320193</code>. Send a
descriptive <code>User-Agent</code> with a real address on it and stay inside
the rate ceiling — that is its own problem, and it has
<a href="/blog/build-scalable-sec-edgar-pipeline">its own note</a>.</p>

<pre class="code"><code class="language-python">import requests

HEADERS = {"User-Agent": "YourCompany you@example.com"}

def facts(cik):
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    return requests.get(url, headers=HEADERS, timeout=30).json()

def latest(f, tag, unit="USD"):
    # A tag the filer never used is absent from the document entirely.
    # That is a presentation fact about the company, not a failed request.
    node = f["facts"].get("us-gaap", {}).get(tag)
    if not node:
        return None
    rows = [r for r in node["units"].get(unit, [])
            if r.get("form") in ("10-K", "10-Q")]
    return max(rows, key=lambda r: r["end"]) if rows else None</code></pre>

<p>Run that across a universe and the empty returns are not failures of your
code. They are filings in which no total-liabilities line was presented.</p>

<h2>The figure that is always there</h2>

<p>A balance sheet has two sides that must agree. The left totals to assets.
The right totals to the claims on those assets, and presentation of the right
side is where filers diverge. What they all print is the bottom line:</p>

<p class="pull">us-gaap:LiabilitiesAndStockholdersEquity</p>

<p>That is the sum of everything on the claims side, the figure that has to
equal total assets, and the most reliably present element on that side of the
taxonomy. Total liabilities is what remains of it once every equity claim is
taken out.</p>

<p class="pull">Liabilities = (L + SE) − Equity − NCI − Mezzanine</p>

<p>Each subtraction is there for a reason, and skipping one produces a number
that looks plausible and is wrong.</p>

<p><strong>Equity.</strong> <code>StockholdersEquity</code> is parent-only. Its
sibling
<code>StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest</code>
already has the minority stake folded in. Take whichever the filer published
and know which one you took — take the inclusive one and also subtract
<code>MinorityInterest</code> and you have removed the same money twice.</p>

<p><strong>Noncontrolling interests.</strong> A parent consolidating a
subsidiary it does not wholly own carries all of that subsidiary's assets, and
carries the slice it does not own as a claim on the right side.
<code>MinorityInterest</code> is equity, not liability. Leave it in and total
liabilities is overstated by exactly the minority stake.</p>

<p><strong>Mezzanine.</strong> Redeemable preferred, redeemable noncontrolling
interests and shares subject to possible redemption sit between the two
sections and belong to neither.
<code>TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests</code>,
<code>RedeemablePreferredStockCarryingAmount</code> and
<code>RedeemableNoncontrollingInterestEquityCarryingAmount</code> are the tags
to look for. They are not liabilities.</p>

<pre class="code"><code class="language-python">def total_liabilities(f):
    direct = latest(f, "Liabilities")
    if direct:
        return direct["val"], "reported"

    rhs = latest(f, "LiabilitiesAndStockholdersEquity")
    if not rhs:
        return None, "no right-hand-side total"

    period = rhs["end"]

    # Facts are not aligned for you. Equity from one quarter subtracted
    # from a right-hand side from another is a number with no meaning,
    # and nothing in the response will tell you it happened.
    def at(tag):
        r = latest(f, tag)
        return r["val"] if r and r["end"] == period else 0

    incl = at("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
    equity = incl if incl else at("StockholdersEquity") + at("MinorityInterest")

    mezz = (
        at("TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests")
        or at("RedeemablePreferredStockCarryingAmount")
        or at("RedeemableNoncontrollingInterestEquityCarryingAmount")
    )

    return rhs["val"] - equity - mezz, "derived"</code></pre>

<h2>Check the answer before you use it</h2>

<p>The derivation gives you a figure. It does not tell you the figure is right.
The check is the identity itself: pull <code>us-gaap:Assets</code> for the same
period and confirm it meets the right-hand side within a tolerance set as a
fraction of assets rather than a flat amount, because filers round at a scale
set by their own size.</p>

<p>If the two sides do not meet, one of three things is true: you missed a
claim line, the filing uses a basis you have not accounted for, or the filing
itself is wrong. Those are different problems with different responses, and
<a href="/blog/five-ways-a-balance-sheet-fails">the five mechanisms</a> are
worth knowing before you meet one.</p>

<p>The response that ruins the exercise is to reach for whichever candidate
closes the gap. That converts a wrong number into a wrong number nobody can
audit, and it destroys the discrepancy that was about to tell you
something.</p>

<h2>When to stop building this</h2>

<p>The derivation above is forty lines. The work after it is not: period
alignment across amended filings, extension tags no standard mapping reaches,
taxonomy versions drifting under you, banks and insurers presenting a
right-hand side shaped like nobody else's, and the standing job of noticing
when a filer changes presentation between quarters.</p>

<p>That is the work here. Every balance sheet across {COMPANIES} companies is
reconciled against A = L + E before it is served, and the ones that do not
close are flagged with the reason rather than quietly patched — total
liabilities comes back as a field with the basis it was computed on attached to
it. <a href="/pricing">The free tier</a> covers the whole universe. If you
would rather keep your own pipeline, the sections above are the parts people
usually find out about after they ship.</p>
""",
)


_POST_SUBMISSIONS_API = Post(
    slug="sec-submissions-api-reconciliation",
    title="The SEC Submissions API: What It Returns, and What It Cannot Reconcile",
    # 56 characters. The query this answers was already reaching the site
    # ("sec edgar submissions api filing reconciliation", position 41) with no
    # page that was about it.
    seo_title="The SEC Submissions API Cannot Reconcile a Balance Sheet",
    description=(
        "The EDGAR submissions endpoint lists filings but holds no figures. "
        "How to join it to company facts on the right key, and why amendments "
        "break it."
    ),
    summary=(
        "Submissions is an index of documents and company facts is a bag of "
        "numbers. Reconciling a balance sheet needs both, joined on the "
        "period and kept inside one accession number."
    ),
    published="2026-09-23",
    updated="2026-09-23",
    minutes=6,
    body="""
<p class="lede">There are two EDGAR endpoints people reach for first, and they
answer different questions. Confusing them costs an afternoon, and the confusion
is reasonable, because both are described as giving you a company's
filings.</p>

<pre class="code"><code>https://data.sec.gov/submissions/CIK##########.json
https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json</code></pre>

<p>The first is an index of documents. The second is a bag of numbers. Neither
one, on its own, lets you reconcile a balance sheet, and the reason is worth
understanding before you build around either.</p>

<h2>What submissions actually returns</h2>

<p>Submissions is the filing history. For a given CIK you get identity fields
(name, SIC, exchange, former names) and then a <code>filings</code> object whose
<code>recent</code> member holds parallel arrays, one per column, not a list of
records:</p>

<pre class="code"><code>import requests

HEADERS = {"User-Agent": "YourCompany yourname@example.com"}

def submissions(cik: int) -&gt; dict:
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    return requests.get(url, headers=HEADERS, timeout=30).json()

def recent_filings(cik: int, forms=("10-K", "10-Q")):
    doc = submissions(cik)
    r = doc["filings"]["recent"]
    rows = zip(r["accessionNumber"], r["form"], r["filingDate"],
               r["reportDate"], r["primaryDocument"])
    return [
        {"accession": a, "form": f, "filed": fd, "period": rd, "doc": pd}
        for a, f, fd, rd, pd in rows
        if f in forms
    ]</code></pre>

<p>Two things about that response catch people out.</p>

<p><strong>The arrays are columnar.</strong> You have to zip them back into
records yourself. Index drift between columns is silent and produces filings
whose form type belongs to one document and whose date belongs to another.</p>

<p><strong><code>recent</code> is not everything.</strong> It holds at least a
year of filings or the latest thousand, whichever is more; anything older is paged out into separate files listed
under <code>filings.files</code>. For an established filer that window may not
reach back as far as you assume. Fetch the additional files and concatenate
before you claim to have a company's history.</p>

<p>What you will not find anywhere in that response is a number off the balance
sheet. There are no assets, no liabilities, no equity. Submissions tells you
that a 10-Q exists, when it was filed, what period it covers, and where the
document lives. It does not tell you what the document says.</p>

<h2>Why reconciliation needs the second call</h2>

<p>Reconciliation is a statement about figures: assets on one side, liabilities
and equity on the other, agreeing within tolerance. Submissions has no figures,
so there is nothing to reconcile. The facts live in company facts, or in the
narrower company concept endpoint if you know exactly which element you
want:</p>

<pre class="code"><code>https://data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/Assets.json</code></pre>

<p>So the shape of any real pipeline is a join. Submissions establishes which
filings exist and what periods they cover. Company facts supplies the values.
The join key is where the work is.</p>

<h2>Joining on period, not on filing date</h2>

<p>The instinct is to join on <code>filingDate</code>. It does not work, and the
failure is quiet.</p>

<p>A fact in company facts carries <code>end</code> (the balance sheet date),
<code>fy</code> and <code>fp</code> (the fiscal year and period it was reported
under), <code>form</code>, <code>filed</code>, and usually <code>accn</code>, the
accession number. A filing in submissions carries <code>reportDate</code> and
<code>filingDate</code>. The pair that means the same thing is
<code>reportDate</code> and <code>end</code>. <code>filingDate</code> is when
the document was transmitted, which may be weeks later and which changes on
amendment while the period does not.</p>

<p><a href="/company/KO">Coca-Cola</a> is a clean example. Its first-quarter
2024 10-Q, for the period ended 29 March 2024, was filed on 2 May 2024. A 10-Q/A
for the same period followed on 30 May 2024. Same <code>reportDate</code>, two
filing dates, two accession numbers. A join on filing date treats them as two
unrelated quarters.</p>

<pre class="code"><code>def facts_for_period(f: dict, tag: str, period: str, unit: str = "USD"):
    node = f["facts"].get("us-gaap", {}).get(tag)
    if not node:
        return []
    return [r for r in node["units"].get(unit, []) if r["end"] == period]</code></pre>

<p>Call that and you will often get more than one row back for a single period.
That is not a bug either.</p>

<h2>The same period, reported more than once</h2>

<p>A balance sheet date appears in company facts once for every filing that
reported it. A figure as of the end of Q2 shows up in the Q2 10-Q, again as a
comparative in the Q3 10-Q, again in the annual report, and again in any
amendment to any of those. The values are usually identical. When they are not,
the difference is the thing you actually wanted to know, and taking
<code>max</code> over the list throws it away.</p>

<p>Use <code>accn</code> to keep the provenance:</p>

<pre class="code"><code>def by_filing(rows):
    out = {}
    for r in rows:
        out.setdefault(r.get("accn"), []).append(r)
    return out</code></pre>

<p>Reconcile within a single accession number. Assets from the original 10-Q
against liabilities and equity from the amendment is not a reconciliation of
anything; it is two filings averaged into a number that appears in neither. If
the identity closes on the original and fails on the amendment, that is a real
finding about the company. If you mixed them, you have destroyed the evidence
and produced a figure you cannot defend to anyone who asks where it came
from.</p>

<p>This is the same discipline that applies to the individual claim lines. I set
out the five mechanisms that make a filing fail the identity check, and what the
correct response is to each, in <a href="/blog/five-ways-a-balance-sheet-fails">The
Five Ways a Balance Sheet Fails the Identity Check</a>.</p>

<h2>What submissions is genuinely good for</h2>

<p>Having said what it cannot do, it does three things nothing else does as
well.</p>

<p><strong>Knowing what to fetch.</strong> Company facts returns a company's
entire tagged history in one object, which for a large filer is a substantial
download. If you only need the most recent annual figures, submissions tells you
which period that is before you commit to the larger call.</p>

<p><strong>Detecting amendments.</strong> <code>10-K/A</code> and
<code>10-Q/A</code> appear in the form column. An amendment means a figure you
already stored may have been restated. Submissions is where you learn that
cheaply, on a schedule, without re-pulling facts for companies that have not
filed anything.</p>

<p><strong>Resolving identity.</strong> <code>formerNames</code> carries prior
names with the dates they applied. <a href="/company/META">Meta</a> still lists
Facebook Inc there, and <a href="/company/XYZ">Block</a> lists Square, Inc. up
to December 2021, a rename later followed by a ticker change. Renames and
reverse mergers are the ordinary reason a company appears to vanish from a
universe between quarters, and the mapping is right there.</p>

<p>Between the two endpoints, and the rate discipline in <a
href="/blog/build-scalable-sec-edgar-pipeline">SEC EDGAR API Rate Limits: A
Python Pipeline Under 10 RPS</a>, you have everything EDGAR offers for free.
What remains is the reconciliation itself, the extension tags no standard
mapping reaches, and the long tail of filers who present a right-hand side
shaped like nobody else's.</p>

<p>That is the layer <a href="/">BalanceProof</a> is. Every balance sheet across
{COMPANIES} companies is reconciled within a single accession before it is
served, and the ones that do not close carry the reason rather than a patched
number. <a href="/pricing">The free tier</a> covers the full universe.</p>
""",
)


POSTS: tuple[Post, ...] = (
    _POST_SUBMISSIONS_API,
    _POST_TOTAL_LIABILITIES,
    _POST_FIVE_FAILURES,
    _POST_WHAT_I_GOT_WRONG,
    _POST_EDGAR_PIPELINE,
    _POST_DUPLICATE_TAGS,
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
        <p class="post-desc">{escape(p.summary)}</p>
        <p class="post-meta">{escape(p.published)} · {p.minutes} min read</p>
      </li>"""
        for p in POSTS
    )
    body = f"""{nav_html}
<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">SEC XBRL Data Notes</h1>
    <p class="hlede">What I learned building a reconciler over every SEC
      filing: the parts that surprised me, written down while they were still
      surprising.</p>
  </header>
  <section class="sec">
    <p class="sec-sub">These are working notes rather than articles: each one
      starts from something that broke while I was building
      <a href="/">BalanceProof</a> and works out why. Most of them are about XBRL,
      because XBRL is where the surprises are. A filing can report the same
      figure twenty-three times, all of them correct, and hand you the wrong
      one without raising anything.</p>
    <p class="sec-sub">The recurring theme is that bad financial data almost
      never looks bad. It arrives as a plausible number from a real filing,
      which is why every note here ends in a check you can run yourself rather
      than a claim you have to take. Where there is code, it is the code this
      site actually runs. Where there is a figure, it is counted live or it is
      not published: the <a href="/methodology">methodology page</a> shows
      what is checked and what is currently failing.</p>
    <ul class="post-list">{items}</ul>
  </section>
{footer}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>"""

    return shell(
        "SEC XBRL Data Notes — BalanceProof",
        body,
        description=(
            "Notes on SEC XBRL data quality, balance sheet reconciliation and "
            "building an SEC filings API that agrees with the filing."
        ),
        canonical="/blog",
        ld=breadcrumb_ld([("Home", "/"), ("Research", "/blog")]),
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
    "build-scalable-sec-edgar-pipeline": (
        ("JPM", "the filing whose 23 Assets tags broke the pipeline"),
        ("AAPL", "a clean single-segment filer, for contrast"),
    ),
    "five-ways-a-balance-sheet-fails": (
        ("JPM", "an identity that closes, drawn, with 23 Assets tags behind it"),
        ("BLK", "the missing-tag category, on a filing this post names"),
        ("KKR", "reconciles once the mezzanine block is added, and says so"),
    ),
    "sec-xbrl-duplicate-tags": (
        ("AAL", "the negative equity the parser got wrong first"),
        ("JPM", "23 tags for one figure, in one filing"),
        ("WFC", "a deposit-funded sheet with the same segment problem"),
    ),
    "understanding-the-accounting-identity": (
        ("AAL", "negative equity that balances perfectly"),
        ("WMT", "an ordinary A = L + E, drawn"),
        ("FCX", "capital-heavy, and the identity still holds"),
    ),
    "total-liabilities-from-sec-edgar": (
        ("KO", "no Liabilities tag at all — the post opens on this filing"),
        ("AMZN", "the same absence, a different balance sheet"),
        ("AAPL", "publishes the subtotal, for the contrast"),
    ),
    "sec-submissions-api-reconciliation": (
        ("KO", "a 10-Q and its 10-Q/A, one period, two filing dates"),
        ("META", "still carries Facebook Inc under formerNames"),
        ("XYZ", "Square, Inc. until 2021, then a ticker change"),
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


def _faq_html(post: Post) -> str:
    """The visible half of `post.faq`; `faq_ld` is the other, from the same tuple."""
    if not post.faq:
        return ""
    items = "".join(
        f"""
      <details class="faq-item">
        <summary>{escape(q)}</summary>
        <div class="faq-a">{escape(a)}</div>
      </details>"""
        for q, a in post.faq
    )
    return f"""
  <section class="sec" id="faq">
    <div class="sec-head"><h2>Questions</h2></div>
    <div class="faq">{items}</div>
  </section>"""


def render_post(post: Post, *, nav: str = "") -> str:
    from src.report.schema import blogposting_ld, breadcrumb_ld, faq_ld

    nav_html, footer = _nav_and_footer(nav)
    body = f"""{nav_html}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <p class="post-meta"><a href="/blog">Research</a> · {escape(post.published)}
        · {post.minutes} min read</p>
      <h1 class="htitle">{escape(post.title)}</h1>
    </header>
    <div class="prose">{_fill_scale(post.body)}</div>
  </article>
  {_faq_html(post)}
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
            ("Research", "/blog"),
            (post.title, f"/blog/{post.slug}"),
        ])
        + faq_ld(post.faq),
    )
