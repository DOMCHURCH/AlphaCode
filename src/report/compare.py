"""Comparison and decision pages: /compare/*, /best/*, /alternatives/*.

Somebody evaluating a financial data API asks four questions in order, and
only one of them had a page here. "What is this" is the home page. "What does
it cost" is /pricing. "How do I use it" is /api. The fourth -- "why this one
rather than the one I already found" -- is the question with commercial intent
on it, and it was answered nowhere.

**On writing about competitors, which is the whole risk in this file.**

Every claim about another company here is either (a) structural -- a fact
about how a product is built that follows from its documented design -- or (b)
explicitly marked as something the reader must check for themselves. There are
NO invented prices, NO invented accuracy figures, and no claim that a
competitor is wrong about anything.

That is not squeamishness. It is the same rule the rest of this site runs on:
every figure traces to something real. A comparison page that made up a rival's
price would be a page asserting an unverified number on a site whose entire
pitch is that it does not do that -- and it would be wrong within a quarter
anyway, because prices move and this file does not. So the comparisons are
about METHOD, which is stable, checkable, and the actual reason to choose one
of these over another.

Where a competitor genuinely does something better, these pages say so. A
comparison page that concludes "us, always" is an advertisement, and readers
sophisticated enough to be shopping for an XBRL API can tell.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from src.report.company_page import asset_version
from src.report.home_page import shell

# Facts about THIS product, in one place, so five pages cannot drift from each
# other or from the pricing page.
#
# The coverage figures were literals here -- "6,201 SEC filers, 1.8 million
# facts" -- and the comment that used to sit in this spot defended them: a
# live read meant `stats.counts()`, an uncached COUNT(*) plus COUNT(DISTINCT)
# over the fundamentals table, and putting that on five more pages to print a
# number that moves four times a year was not worth the render.
#
# Both halves of that have since stopped being true. `counts()` and
# `dataset.row_count()` are memoised for fifteen minutes, so the read costs
# nothing; and the drift the comment accepted as the price duly arrived --
# 6,201 against a live 6,222, and 1.8 million against a live 1.6M, on the
# pages whose whole argument is that this product's figures agree with each
# other. The literals were also the only claim here that a reader could check
# in one click and find wrong.
#
# So the figures are now tokens, filled at render time from the same two
# functions the home page, /api, the blog and llms.txt read. They are whole
# PHRASES rather than bare numbers, because a count that cannot be read is
# not a count to guess at: the sentence gives up the figure and keeps the
# claim, instead of carrying a stale one.
ACCURACY = "reconciled to the accounting identity"
NAIVE_ACCURACY = "wrong roughly one filing in five"

# Measured against production on 2026-09-09: three consecutive calls to
# /api/demo/AAPL -- the same read path as the paid endpoint, minus metering --
# returned in 292, 306 and 364 ms. Stated as a range rather than an average
# because one machine on one evening is not a latency guarantee, and a single
# tidy number would read like one.
LATENCY = "~300 ms typical (measured 292–364 ms, Sep 2026)"


@dataclass(frozen=True)
class Row:
    """One line of a comparison table."""

    label: str
    ours: str
    theirs: str
    # True where the honest answer favours the other side, or is a draw. The
    # table marks these plainly rather than quietly omitting the row.
    ours_wins: bool = True


@dataclass(frozen=True)
class Page:
    slug: str
    h1: str
    seo_title: str
    description: str
    rival: str
    lede: str
    rows: tuple[Row, ...]
    choose_us: tuple[str, ...]
    choose_them: tuple[str, ...]
    body: str
    updated: str = "2026-09-09"
    kind: str = "compare"
    extras: tuple[str, ...] = field(default_factory=tuple)


# Why the method is the product. Rendered on every comparison page,
# because the question a buyer is actually asking is not "is it right"
# but "will it tell me when it isn't".
_TRANSPARENCY = """
<h2>Why transparency matters more than a number</h2>

<p>Every provider in this category reads the same filings. The difference is
what happens when a filing is hard to read. The usual answer is that you get a
number anyway, with nothing attached to say how confident it is &mdash; and a
figure that is quietly a segment instead of a company looks exactly like one
that is right.</p>

<p>BalanceProof checks every balance sheet against
<strong>Assets = Liabilities + Equity</strong> before publishing it. A filing
that reconciles is published with its figures. A filing that does not is
published <em>with the reason</em>: a noncontrolling interest reported as a
separate line, mezzanine equity outside permanent equity, rounding inside one
percent, a component we could not read, or a filing whose own totals disagree
with each other. The exceptions are counted in public and named individually on
<a href="/methodology">how we verify</a>.</p>

<p>That is the whole claim. Not that nothing is ever wrong &mdash; that when
something is, you are told which number and why, instead of finding out from
your own reconciliation three weeks later.</p>
"""

_METHOD = """
<h2>The difference is which tag gets picked</h2>

<p>Every provider in this category reads the same source. SEC EDGAR publishes
XBRL for every filer, free, and nobody has better raw material than anybody
else. What separates one API from the next is not access. It is the
<em>selection step</em>, and that step is almost never documented.</p>

<p>Here is the problem it has to solve. Open JPMorgan's 10-Q and search for
<code>Assets</code> and you get twenty-three facts. Not twenty-three values —
twenty-three tagged instances, one for the consolidated bank and one for each
segment and subsidiary that has to be broken out separately. Every one is
valid XBRL. Exactly one is the number on the face of the balance sheet, and
the only thing marking it is an <em>absence</em>: it is the fact with no
dimensions attached.</p>

<p>An extractor that takes the first match, or the largest, or the most
recently filed, will be right most of the time and wrong in a way that leaves
no trace. No exception, no null, no warning. Just a number that is a segment
instead of a company. Measured across the filings loaded here, that naive
approach disagrees with the consolidated figure often enough to matter —
roughly one filing in five.</p>

<p>BalanceProof resolves it with arithmetic rather than a heuristic: pull every
candidate for assets, liabilities and equity, and keep the combination that
satisfies <strong>Assets = Liabilities + Equity</strong>. The consolidated
figures balance against each other. A segment's assets do not balance against
the whole company's liabilities. The identity is a test, not a guideline, and
it is the reason a figure here is <strong>checked</strong> rather than
guessed at.</p>

<p>When nothing balances, the answer is that nothing balances. The filing is
served as-reported with a warning on it rather than adjusted until the columns
agree, because a filing that does not add up is a fact about the company, and
you should get it as one.</p>
"""


_HONESTY = """
<h2>What I am not going to pretend</h2>

<p>I am not going to put {rival}'s prices in a table on my own website. They change,
this page would not, and you would be reading a number I had no way to verify
at the moment you read it. Go and look at their pricing page. It is the only copy
that is current.</p>

<p>I am also not going to tell you their data is bad. I have not audited it and
I am not in a position to. What I can tell you is what <em>this</em> service
does and how to check it, which is the part I am actually responsible
for.</p>

<p>The check that settles it costs you nothing either way: take a company where
you already know the answer, call both, and compare each against the filing on
EDGAR. Not against each other — against the filing. That is the only
comparison that means anything, and it is why the free tier here needs no
card.</p>
"""


def _table(page: Page) -> str:
    rows = "".join(
        f"""
          <tr><th scope="row">{escape(r.label)}</th>
              <td class="{'win' if r.ours_wins else ''}">{r.ours}</td>
              <td>{r.theirs}</td></tr>"""
        for r in page.rows
    )
    return f"""
    <div class="tablewrap">
      <table class="compare compare-3">
        <caption class="vh">BalanceProof compared with {escape(page.rival)}</caption>
        <thead>
          <tr><th scope="col">&nbsp;</th>
              <th scope="col">BalanceProof</th>
              <th scope="col">{escape(page.rival)}</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _choose(page: Page) -> str:
    def half(head: str, sub: str, items: tuple[str, ...]) -> str:
        li = "".join(f"<li>{i}</li>" for i in items)
        return f"""
      <div class="split-half">
        <span class="plan-name">{escape(head)}</span>
        <p class="split-lede">{escape(sub)}</p>
        <ul class="notelist">{li}</ul>
      </div>"""

    return f"""
  <section class="sec" id="which">
    <div class="sec-head"><h2>When to choose each</h2></div>
    <div class="split">
      {half("Choose BalanceProof", "If the number has to be right.", page.choose_us)}
      {half(f"Choose {page.rival}", "If any of these is you.", page.choose_them)}
    </div>
  </section>"""


PAGES: tuple[Page, ...] = (
    Page(
        slug="compare/balanceproof-vs-intrinio",
        h1="BalanceProof vs Intrinio: SEC Filings API Comparison",
        seo_title="BalanceProof vs Intrinio — SEC Filings API Comparison (2026)",
        description=(
            "BalanceProof vs Intrinio for SEC XBRL balance sheet data: "
            "reconciliation method, coverage, pricing, and when each one is "
            "the right fit for a quant team."
        ),
        rival="Intrinio",
        lede=(
            "Intrinio is a broad financial data platform. BalanceProof does one "
            "thing. That is the entire comparison, and which way it cuts "
            "depends on what you are building."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Broad: fundamentals, prices, options, news, ESG", False),
            Row("Selection method", "A = L + E, published and testable", "Not publicly documented"),
            Row("Tells you when it is unsure", "Yes — every exception is flagged with its reason, and the counts are public at /methodology", "No — a figure is returned either way"),
            Row("Accuracy method", "Every valid filing checked against A = L + E; exceptions flagged", "Not published"),
            Row("Coverage", "{COVERAGE}", "Wider — many asset classes and vendors", False),
            Row("Latency", LATENCY + "; EDGAR swept every 6h", "Varies by feed and plan — check their site"),
            Row("Price", "Free tier · $49/mo · $490/yr · $79.99 one-off", "Quote-based, tiered by feed — check their site"),
            Row("Free tier", "Yes, no card", "Sandbox keys available — check their site"),
            Row("Bulk download", "$79.99, one CSV, no contract", "Available on higher plans", False),
        ),
        choose_us=(
            "<b>You only need balance sheets, and you need them right.</b> "
            "Paying for prices, options and news you will not call is a tax on "
            "the one endpoint you wanted.",
            "<b>You need to defend the number.</b> Every figure here reconciles "
            "against A = L + E and every response is as-reported, so you can "
            "tie a row back to the filing in front of a risk committee.",
            "<b>You want to start today.</b> Free tier, no card, no call.",
            "<b>You want the whole thing as a file.</b> $79.99 once, no "
            "contract, no seat count.",
        ),
        choose_them=(
            "<b>You need more than balance sheets.</b> Prices, options chains, "
            "estimates, news, alternative data. That is a platform's job, and "
            "this is not a platform.",
            "<b>You need non-US or non-SEC coverage.</b> Everything here comes "
            "from EDGAR. If a company does not file with the SEC, it does not "
            "exist to this API.",
            "<b>You need a vendor with an enterprise contract, an SLA and a "
            "support desk.</b> I am one person. That is a real difference and "
            "you should weigh it honestly.",
            "<b>You need decades of history across many asset classes.</b>",
        ),
        body=_METHOD + _TRANSPARENCY + _HONESTY.format(rival="Intrinio"),
    ),
    Page(
        slug="compare/balanceproof-vs-sec-api",
        h1="BalanceProof vs sec-api.io: Which SEC XBRL API Should You Use?",
        seo_title="BalanceProof vs sec-api.io — SEC XBRL API Comparison",
        description=(
            "BalanceProof vs sec-api.io for SEC filings data: filing access "
            "versus reconciled financials, and which one fits a screener, a "
            "backtest or a pipeline."
        ),
        rival="sec-api.io",
        lede=(
            "These two are not really competitors. One gives you filings. The "
            "other gives you numbers you can put in a model. Plenty of teams "
            "should use both."
        ),
        rows=(
            Row("Primary object", "A reconciled balance sheet", "A filing, and everything in it", False),
            Row("Full-text filing search", "No — this is not a filings API", "Yes, and it is good at it", False),
            Row("Form coverage", "Balance sheet data from periodic filings", "Every form type: 8-K, S-1, 13F, Form 4, more", False),
            Row("Selection method", "A = L + E, published and testable", "Returns the tags as filed"),
            Row("Duplicate-tag handling", "Resolved to the consolidated figure", "Yours to resolve"),
            Row("Tells you when it is unsure", "Yes — every exception is flagged with its reason, and the counts are public at /methodology", "No — a figure is returned either way"),
            Row("Accuracy method", "Every valid filing checked against A = L + E; exceptions flagged", "Not applicable — it is not selecting for you"),
            Row("Coverage", "{COVERAGE}", "Every EDGAR filing, all form types", False),
            Row("Latency", LATENCY, "Real-time filing stream — faster to the document", False),
            Row("Price", "Free tier · $49/mo · $490/yr · $79.99 one-off", "Tiered by call volume — check their site"),
            Row("Bulk download", "$79.99, one CSV", "Available — check their site", False),
        ),
        choose_us=(
            "<b>You want the answer, not the filing.</b> If your next step "
            "after fetching a document is writing a parser, that parser is the "
            "thing this replaces.",
            "<b>You are ranking or screening companies.</b> A screen built on "
            "unreconciled tags is a screen that quietly ranks some companies "
            "by a segment.",
            "<b>You are backtesting.</b> As-reported figures with a filing "
            "date on each one, so you can ask what was knowable then.",
        ),
        choose_them=(
            "<b>You need the filing itself.</b> Full text, exhibits, the "
            "original document. That is their product and it is not mine.",
            "<b>You need forms other than periodic financials.</b> 8-K, 13F, "
            "Form 4, S-1: none of that is here.",
            "<b>You want to do your own extraction.</b> If you have an opinion "
            "about which tag is right, you want raw access, not my opinion.",
            "<b>You need real-time filing alerts.</b>",
        ),
        body=_METHOD + _TRANSPARENCY + _HONESTY.format(rival="sec-api.io"),
    ),
    Page(
        slug="compare/balanceproof-vs-xignite",
        h1="BalanceProof vs Xignite: Financial Data API Comparison",
        seo_title="BalanceProof vs Xignite — Financial Data API",
        description=(
            "BalanceProof vs Xignite for SEC fundamentals: an independent "
            "XBRL reconciler against an enterprise market-data platform. "
            "Pricing, coverage and accuracy."
        ),
        rival="Xignite",
        lede=(
            "Xignite sells market data to institutions at institutional scale. "
            "BalanceProof sells one reconciled dataset to whoever wants it. If you "
            "are choosing between these two you probably already know which "
            "side of that line you are on."
        ),
        rows=(
            Row("Built for", "Individual quants and small teams", "Enterprises, brokerages, banks", False),
            Row("Buying process", "Card, self-serve, thirty seconds", "Sales cycle, contract, procurement", False),
            Row("Asset classes", "SEC fundamentals only: balance sheet, income, cash flow", "Equities, FX, rates, ETFs, indices and more", False),
            Row("Selection method", "A = L + E, published and testable", "Not publicly documented"),
            Row("Tells you when it is unsure", "Yes — every exception is flagged with its reason, and the counts are public at /methodology", "No — a figure is returned either way"),
            Row("Accuracy method", "Every valid filing checked against A = L + E; exceptions flagged", "Not published as a figure"),
            Row("Coverage", "{COVERAGE}", "Global, many asset classes", False),
            Row("Latency", LATENCY, "Real-time market data — a different problem", False),
            Row("SLA", "None. One person.", "Contractual, with support", False),
            Row("Price", "Free tier · $49/mo · $490/yr · $79.99 one-off", "Enterprise quote — check their site"),
            Row("Time to first call", "Minutes", "Weeks, typically", False),
        ),
        choose_us=(
            "<b>You are one person or a small team.</b> Nothing here requires "
            "a procurement conversation.",
            "<b>You only need fundamentals.</b> An enterprise market-data "
            "contract to get balance sheets is a very expensive way to get "
            "balance sheets.",
            "<b>You want to test before you buy.</b> Free tier, no card, and "
            "the whole dataset for $79.99 if you would rather have a file.",
            "<b>You care how the number was chosen.</b> The method is written "
            "down and you can check it against EDGAR yourself.",
        ),
        choose_them=(
            "<b>You need an SLA and somebody to call.</b> This is the honest "
            "one. If uptime is contractual for you, buy from a company that "
            "can sign for it.",
            "<b>You need real-time market data.</b> Prices, quotes, FX: none "
            "of it is here and none of it is planned.",
            "<b>You are an institution with compliance requirements</b> around "
            "vendor diligence, redundancy and data lineage attestations.",
            "<b>You need global coverage.</b> This is SEC filers only.",
        ),
        body=_METHOD + _TRANSPARENCY + _HONESTY.format(rival="Xignite"),
    ),
    Page(
        slug="best/sec-filings-api-for-quants",
        h1="The Best SEC Filings API for Quants: How to Actually Choose One",
        seo_title="Best SEC Filings API for Quants — How to Choose (2026)",
        description=(
            "How to evaluate an SEC filings API for quant work: the "
            "duplicate-tag problem, as-reported versus restated, point-in-time "
            "correctness, and four tests."
        ),
        rival="the alternatives",
        kind="best",
        lede=(
            "I am not going to rank other people's products. I am going to "
            "give you the four tests I wish somebody had given me, so you can "
            "rank them yourself, including against this one."
        ),
        rows=(
            Row("Resolves duplicate XBRL tags", "Yes — A = L + E", "Ask. Most do not document it"),
            Row("As-reported, not restated", "Yes, with filing dates", "Ask — many silently serve restated"),
            Row("Point-in-time queries", "Yes — as-of respects filing dates", "Ask"),
            Row("Publishes an accuracy figure", "No — the method is published instead", "Rare"),
            Row("Free tier without a call", "Yes", "Varies"),
            Row("Coverage", "{COVERAGE}", "Ask — and ask whether it is SEC-only"),
            Row("Latency", LATENCY, "Ask, and measure it yourself"),
            Row("Price", "Free · $49/mo · $490/yr · $79.99 one-off", "Often quote-based"),
            Row("Bulk download", "$79.99, one CSV", "Varies"),
        ),
        choose_us=(
            "<b>Test 1: the JPMorgan test.</b> Ask for JPM's total assets and "
            "check it against the 10-Q. If you get a segment instead of the "
            "consolidated figure, the provider is not resolving duplicate "
            "tags, and everything downstream inherits that.",
            "<b>Test 2: the restatement test.</b> Find a company that revised "
            "a figure. Ask for the original period. If you get the revised "
            "number with no filing date attached, you cannot backtest with it "
            "You will be trading on information that did not exist yet.",
            "<b>Test 3: the identity test.</b> Pull assets, liabilities and "
            "equity for fifty companies and check that A = L + E on each. Any "
            "provider selecting tags badly will fail some of them, and the "
            "failures tell you the shape of their error.",
            "<b>Test 4: the missing-data test.</b> Find a filer that does not "
            "break out receivables. A good API says so. A bad one returns "
            "zero, and a zero is a claim.",
        ),
        choose_them=(
            "<b>You need more than fundamentals.</b> Prices, options, news, "
            "alternative data. A single-purpose API is the wrong shape.",
            "<b>You need non-SEC coverage.</b>",
            "<b>You need an SLA and vendor diligence paperwork.</b>",
            "<b>You already have a reconciler you trust.</b> Then you want raw "
            "filing access, and there are good products for that.",
        ),
        body="""
<h2>Why this is harder than it looks</h2>

<p>Fundamental data feels like a solved problem. The filings are public, the
format is machine-readable, and there are a dozen APIs. So the natural
assumption is that they all return the same numbers and you are choosing on
price and ergonomics.</p>

<p>They do not return the same numbers. I checked.</p>

<p>The reason is the duplicate-tag problem. XBRL lets a filer attach dimensions
to a fact: this figure, but for this segment, this subsidiary, this geography.
JPMorgan tags <code>Assets</code> twenty-three separate times in a single
filing. Every instance is valid. The consolidated one, the number on the face
of the balance sheet, is marked only by having no dimensions on it, which means
the correct answer is identified by an absence, the easiest thing in the world
for a parser to miss.</p>

<p>Taking the first match agrees with the consolidated figure about
<strong>most</strong> of the time. One filing in five is
wrong, silently, by whatever the largest segment happens to be.</p>

<h2>The three questions that actually separate providers</h2>

<p><strong>How do you pick which tag is the right one?</strong> If the answer
is vague, that is your answer. This is the entire difficulty of the category
and a provider who has solved it will tell you how. BalanceProof uses the
accounting identity: keep the combination of assets, liabilities and equity
that balances, because the consolidated figures balance against each other and
a segment's do not.</p>

<p><strong>As-reported or restated?</strong> These are different products and
the difference decides whether you can backtest. As-reported is what the
company said at the time. Restated is what they said later. If a provider
serves restated figures with no filing date, every backtest you run on it has
lookahead bias baked in and no way to detect it.</p>

<p><strong>What happens when the data is missing or wrong?</strong> A filer
that does not break out receivables has not reported zero receivables. If your
provider returns 0, your ratios are wrong and nothing tells you. And when a
filing genuinely does not balance (it happens), you want to be told, not
handed a silently adjusted number.</p>

<h2>What BalanceProof is, plainly</h2>

<p>One reconciled dataset: {COVERAGE_PROSE}, every one checked
against A = L + E before it is stored.
Every valid filing reconciles against that test, and the exceptions are
flagged rather than hidden. Free tier with no card, $49 a month for
""" + "10,000" + """ calls, or $79.99 once for the whole thing as a CSV.</p>

<p>I built it because I wanted this data for something else and could not find
a source I trusted enough to build on. I am 17. That is a real thing to weigh:
there is no SLA and no support desk, and if that matters to you, buy from
somebody who can sign a contract. What there is instead is a method written
down in public and a free tier so you can check it yourself against a filing
you already know.</p>
""",
    ),
    Page(
        slug="alternatives/intrinio",
        h1="Intrinio Alternatives for SEC Balance Sheet Data",
        seo_title="Intrinio Alternatives for SEC XBRL Balance Sheet Data",
        description=(
            "Looking for an Intrinio alternative for SEC fundamentals? What "
            "to look for in a replacement, and when a single-purpose XBRL "
            "reconciler is the better fit."
        ),
        rival="Intrinio",
        kind="alternatives",
        lede=(
            "People usually go looking for an alternative for one of three "
            "reasons: price, scope, or they want to know how the number was "
            "chosen. Only the third one is a reason to pick this."
        ),
        rows=(
            Row("Scope", "SEC fundamentals only: balance sheet, income, cash flow", "Broad platform, many feeds", False),
            Row("Pricing model", "Published, self-serve, card", "Quote-based, tiered — check their site"),
            Row("Selection method", "A = L + E, published", "Not publicly documented"),
            Row("Entry price", "Free, then $49/mo", "Check their site"),
            Row("Bulk file", "$79.99 one-off", "Higher plans", False),
            Row("Tells you when it is unsure", "Yes — every exception is flagged with its reason, and the counts are public at /methodology", "No — a figure is returned either way"),
            Row("Accuracy method", "Every valid filing checked against A = L + E; exceptions flagged", "Not published as a figure"),
            Row("Latency", LATENCY, "Varies by feed — check their site"),
            Row("Coverage", "{FILERS}", "Wider, including non-SEC", False),
        ),
        choose_us=(
            "<b>Scope is your reason.</b> If you evaluated a platform and "
            "realised you wanted one endpoint, a single-purpose API is cheaper "
            "and simpler in exactly the way you were hoping.",
            "<b>Method is your reason.</b> If you want to know why a figure "
            "was chosen, the answer here is one equation and you can test it.",
            "<b>Procurement is your reason.</b> Card, self-serve, no call.",
        ),
        choose_them=(
            "<b>Price alone is your reason.</b> Then compare like for like. A "
            "platform doing ten things is not overpriced for doing ten things, "
            "and you may just need fewer of them.",
            "<b>You need the breadth.</b> Prices, estimates, news, non-SEC "
            "coverage: none of that exists here and none is planned.",
            "<b>You need vendor guarantees.</b> No SLA, one maintainer.",
        ),
        body="""
<h2>Work out which problem you actually have</h2>

<p>"Alternative to X" searches usually mean one of three things, and they lead
to different answers.</p>

<p><strong>If it is price</strong>, be careful comparing a platform to a
single-purpose tool. A broad data platform is not expensive for being broad.
It is expensive because you are buying ten feeds. The question is whether you
need ten. If you only ever call fundamentals, you are paying nine-tenths of a
bill for nothing, and that is a scope problem wearing a price problem's
clothes.</p>

<p><strong>If it is scope</strong>, a narrower tool is straightforwardly
better. Fewer endpoints, less surface, simpler mental model, and the thing you
wanted is the thing on the front page.</p>

<p><strong>If it is method</strong>, and you found a number that disagreed
with a filing and want to know why, that is the reason I built this, and it is the
one where I can give you a real answer.</p>
""" + _METHOD + _TRANSPARENCY + """
<h2>What to demand from any replacement</h2>

<p>Whatever you end up choosing, including if it is not this: make the provider
answer how they resolve duplicate XBRL tags, whether figures are as-reported or
restated, whether filing dates come with the data, and what happens when a
filer does not report a line. Those four answers predict almost everything
about whether the data will hold up in production.</p>

<p>Then run the check that settles it. Take a company you know, call the API,
and compare the response against the filing on EDGAR, not against another
vendor. That comparison is the only one with an authority behind it.</p>
""" + _HONESTY.format(rival="Intrinio"),
    ),
    # ------------------------------------------------------------------
    # Rivals added 2026-09-18. Every figure in the `theirs` column below was
    # read off that vendor's own page ON THAT DATE, or it says "check their
    # site". Nothing here is recalled, inferred from a press mention, or
    # rounded from memory -- a wrong claim about a competitor's price is the
    # same failure as a wrong claim about a filer's balance sheet, and this
    # site has already made one of those.
    # ------------------------------------------------------------------
    Page(
        slug="compare/balanceproof-vs-financial-modeling-prep",
        updated="2026-09-18",
        h1="BalanceProof vs Financial Modeling Prep: SEC Balance Sheet Data",
        seo_title="BalanceProof vs FMP \u2014 SEC Balance Sheet Data (2026)",
        description=(
            "BalanceProof vs Financial Modeling Prep for SEC balance sheets: "
            "how each one resolves a filing, what happens when a filing is "
            "ambiguous, and which is the right fit."
        ),
        rival="Financial Modeling Prep",
        lede=(
            "FMP is the feed underneath a great deal of retail tooling \u2014 "
            "spreadsheet add-ins, screeners, dashboards. If you are reading "
            "this you are probably not choosing between the two so much as "
            "asking whether something should sit in front of the one you "
            "already have."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Broad: statements, prices, ratios, estimates", False),
            Row("Selection method", "A = L + E, published and testable", "Check their site \u2014 not found published"),
            Row("Tells you when it is unsure", "Yes \u2014 every exception flagged with its reason, counts public at /methodology", "Check their site"),
            Row("Coverage", "{COVERAGE}", "Wider \u2014 many markets and data types", False),
            Row("Latency", LATENCY + "; EDGAR swept every 6h", "Check their site"),
            Row("Price", "Free tier \u00b7 $49/mo \u00b7 $490/yr \u00b7 $79.99 one-off", "Check their site \u2014 their pricing page blocks automated reads"),
            Row("Free tier", "Yes, {FREE_CALLS} calls/month, no card", "Check their site"),
            Row("Bulk download", "$79.99, one CSV, no contract", "Check their site"),
        ),
        choose_us=(
            "<b>You are already on FMP and want a referee.</b> The free tier "
            "is {FREE_CALLS} calls a month with no card, which is enough to run "
            "against what you already serve and see whether anything "
            "disagrees. That is the actual use case here, not replacement.",
            "<b>You need to defend a number.</b> Every figure reconciles "
            "against A = L + E or is flagged with the reason it does not, so "
            "a row ties back to the filing in front of somebody who asks.",
            "<b>Redeemable equity matters to you.</b> Some filings only close "
            "once mezzanine equity is counted as its own block \u2014 "
            "<a href=\"/company/LCID\">Lucid</a> is the clean example. Read "
            "permanent equity alone on a filer shaped like that and the sheet "
            "is short the whole line, with no error raised.",
        ),
        choose_them=(
            "<b>You need more than balance sheets.</b> Income statements, cash "
            "flow, prices, ratios, estimates. One endpoint that does one thing "
            "is the wrong shape for that.",
            "<b>You need non-US or non-SEC coverage.</b>",
            "<b>You want one vendor and one invoice.</b> Adding a second "
            "service to check the first is a real cost, and for plenty of "
            "products it is not worth paying.",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>A note on what is not in the table</h2>

<p>Most of the right-hand column above says <em>check their site</em>. That is
deliberate and it is not evasion: Financial Modeling Prep&rsquo;s pricing pages
return a 403 to automated readers, so nothing about their current tiers could be
verified on the day this page was written. Writing a number in anyway &mdash;
from memory, from a blog post, from what was true last year &mdash; is how
comparison pages end up quietly wrong about somebody else&rsquo;s business.</p>

<p>The rest of this site is built on refusing to publish a figure that cannot be
checked. It would be strange to abandon that on the page where the figure is
about someone else.</p>
""" + _HONESTY.format(rival="Financial Modeling Prep"),
    ),
    Page(
        slug="compare/balanceproof-vs-polygon",
        updated="2026-09-18",
        h1="BalanceProof vs Polygon.io: Fundamentals, and Where Polygon Went",
        seo_title="BalanceProof vs Polygon.io \u2014 Fundamentals (2026)",
        description=(
            "BalanceProof vs Polygon.io for SEC balance sheet data, including "
            "what polygon.io now redirects to and what that means for "
            "fundamentals coverage."
        ),
        rival="Polygon.io",
        lede=(
            "Start with something you should check for yourself: as of "
            "18 September 2026, polygon.io and polygon.io/pricing both return "
            "a 301 to massive.com. The figures below are read off that site, "
            "because that is where the product now answers from."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Market data: stocks, options, indices, currencies, futures", False),
            Row("Fundamentals", "The whole product", "An add-on \u2014 \u201cFinancials &amp; Ratios\u201d, sold separately or bundled on their top tier"),
            Row("Selection method", "A = L + E, published and testable", "Check their site \u2014 not found published"),
            Row("Tells you when it is unsure", "Yes \u2014 every exception flagged with its reason", "Check their site"),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "A free Basic tier, metered per minute, end-of-day \u2014 check their site"),
            Row("Entry paid tier", "$49/mo", "Check their site"),
            Row("Latency", LATENCY, "Check their site \u2014 real-time market data is their pitch, not ours", False),
            Row("Bulk download", "$79.99, one CSV, no contract", "Check their site"),
        ),
        choose_us=(
            "<b>Balance sheets are the thing you need, not a side dish.</b> On "
            "their side fundamentals are an add-on to a market-data "
            "platform; here they are the product, and the verification is the "
            "reason to buy.",
            "<b>You want the method written down.</b> A = L + E is testable, "
            "and the exceptions are counted in public on "
            "<a href=\"/methodology\">how we verify</a>.",
            "<b>A call-per-minute cap is the wrong shape for a backfill.</b> "
            "Five a minute is a sensible free tier for streaming quotes and an "
            "awkward one for walking a universe of filings.",
        ),
        choose_them=(
            "<b>You need prices, options, or anything real-time.</b> That is "
            "their actual product and it is not remotely this one. Their "
            "homepage pitch is institutional market data; fundamentals are not "
            "mentioned on it.",
            "<b>You need one vendor across asset classes.</b>",
            "<b>You want unmetered API calls on a cheap tier.</b> Their paid "
            "tiers advertise unlimited calls, which is a different and "
            "perfectly good bargain.",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>About the redirect</h2>

<p>On 18 September 2026, <code>https://polygon.io</code> and
<code>https://polygon.io/pricing</code> both returned <code>301 Moved
Permanently</code> to <code>massive.com</code>. The destination site does not
mention Polygon anywhere, and describes itself as &ldquo;modernizing Wall St.
one market at a time&rdquo;.</p>

<p>That is the whole of what can be said with certainty, so it is the whole of
what is said here. This page does not assert a rebrand, an acquisition, or a
shutdown, because none of those were verified &mdash; a redirect is a fact and
the story behind it is not. Check it yourself before making a procurement
decision on it; it is one <code>curl -I</code>.</p>
""" + _HONESTY.format(rival="Polygon.io"),
    ),
    Page(
        slug="compare/balanceproof-vs-alpha-vantage",
        updated="2026-09-18",
        h1="BalanceProof vs Alpha Vantage: SEC Fundamentals Compared",
        seo_title="BalanceProof vs Alpha Vantage \u2014 Fundamentals (2026)",
        description=(
            "BalanceProof vs Alpha Vantage for SEC balance sheet data: free "
            "tier limits, what each publishes about its method, and which "
            "suits a backfill."
        ),
        rival="Alpha Vantage",
        lede=(
            "Alpha Vantage is the default free answer in this category, and "
            "for a lot of projects that is the correct answer. The question "
            "worth asking is what happens at the point where free stops being "
            "the constraint and correctness starts."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Broad: prices, FX, crypto, technicals, fundamentals", False),
            Row("Selection method", "A = L + E, published and testable", "Check their site \u2014 not documented on their premium page"),
            Row("Tells you when it is unsure", "Yes \u2014 every exception flagged with its reason", "Check their site"),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Free tier metered per DAY \u2014 check their site"),
            Row("Entry paid tier", "$49/mo", "Check their site"),
            Row("Paid tiers sold in", "Calls per month", "Requests per minute", False),
            Row("Coverage", "{COVERAGE}", "Wider \u2014 many asset classes", False),
            Row("Bulk download", "$79.99, one CSV, no contract", "Check their site"),
        ),
        choose_us=(
            "<b>You are doing a backfill, not a poll.</b> A per-day free cap is "
            "a sampling budget; {FREE_CALLS} calls a month is a walk-the-universe "
            "budget. Check their current limit against that shape \u2014 it "
            "is the difference that matters, not the number.",
            "<b>You want the exceptions named.</b> Their premium page documents "
            "rate limits thoroughly and says nothing about how a figure is "
            "chosen out of a filing that offers several. Ours is on "
            "<a href=\"/methodology\">one page</a>, with counts.",
            "<b>Redeemable and noncontrolling equity are handled explicitly</b> "
            "rather than absorbed silently.",
        ),
        choose_them=(
            "<b>You need prices, FX, crypto or technical indicators.</b> "
            "Genuinely broad coverage, and this is not that.",
            "<b>Rate per minute matters more than calls per month.</b> Their "
            "paid tiers are sold in requests/minute, which is the right shape "
            "for a live dashboard.",
            "<b>You are already integrated.</b> A working integration has real "
            "value and &ldquo;it reconciles&rdquo; is not on its own a reason "
            "to rip one out.",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="Alpha Vantage"),
    ),
    Page(
        slug="alternatives/financial-modeling-prep",
        updated="2026-09-18",
        h1="Financial Modeling Prep Alternatives for SEC Balance Sheets",
        seo_title="FMP Alternatives for SEC Balance Sheets (2026)",
        description=(
            "Looking for an FMP alternative for SEC balance sheet data? The "
            "honest answer depends on whether you need breadth or need the "
            "numbers to reconcile."
        ),
        rival="Financial Modeling Prep",
        kind="alternatives",
        lede=(
            "If you are searching this, something has gone wrong with a "
            "number. The useful question is not &ldquo;what else is "
            "there&rdquo; but &ldquo;what exactly broke&rdquo;, because those "
            "have different answers and only one of them is this site."
        ),
        rows=(
            Row("If you need broader data", "Not the answer \u2014 this is SEC fundamentals only", "Stay. Breadth is what it is for", False),
            Row("If a balance sheet did not reconcile", "This is the whole product", "Check their site for a published method"),
            Row("If you need to defend a figure", "Every row ties to a filing; exceptions named", "Check their site"),
            Row("If you need point-in-time", "As-reported, with filing dates", "Check their site"),
            Row("Migration cost", "Low \u2014 add it alongside, do not replace", "n/a"),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site"),
            Row("Price", "Free \u00b7 $49/mo \u00b7 $490/yr \u00b7 $79.99 one-off", "Check their site"),
        ),
        choose_us=(
            "<b>Do not migrate. Add.</b> The common shape here is a product "
            "that needs FMP&rsquo;s breadth and needs its balance sheets "
            "checked. Those are two jobs, and the second one costs nothing to "
            "trial.",
            "<b>The failure you hit probably has a name.</b> A noncontrolling "
            "interest reported as its own line, mezzanine equity outside "
            "permanent equity, or a duplicate tag resolved to a segment. All "
            "of them are counted in public on "
            "<a href=\"/methodology\">how we verify</a>.",
        ),
        choose_them=(
            "<b>Your problem was breadth, not correctness.</b> Then no part of "
            "this site helps and you want a wider vendor, not a stricter one.",
            "<b>Your problem was a single bad row.</b> Ask them to fix it "
            "first. A vendor who corrects a filing when you report it is worth "
            "more than one who never had that row wrong.",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="Financial Modeling Prep"),
    ),
    Page(
        slug="best/sec-xbrl-api",
        updated="2026-09-18",
        h1="The Best SEC XBRL API: What Actually Separates Them",
        seo_title="Best SEC XBRL API \u2014 What Separates Them (2026)",
        description=(
            "Choosing an SEC XBRL API: the duplicate-tag problem, what "
            "as-reported really means, and the four checks that tell you "
            "whether a provider resolves a filing or guesses at it."
        ),
        rival="the alternatives",
        kind="best",
        lede=(
            "Every provider in this category reads the same free filings from "
            "the same free source. What you are paying for is what happens "
            "when one of those filings is ambiguous \u2014 and that is the one "
            "thing the marketing pages do not talk about."
        ),
        rows=(
            Row("Resolves duplicate XBRL tags", "Yes \u2014 by A = L + E, not by tag name", "Ask. Most do not document it"),
            Row("Publishes its exception count", "Yes, on /methodology, updated each load", "Rare"),
            Row("Names which filings failed", "Yes \u2014 by ticker, with the reason", "Rare"),
            Row("Says when a figure is missing and why", "Yes — published as its own category, including where the line was never tagged", "Very rare"),
            Row("Handles mezzanine equity", "Yes, as its own block", "Ask \u2014 this is where SPACs and biotechs break"),
            Row("Handles noncontrolling interest", "Yes, added explicitly and said out loud", "Ask"),
            Row("As-reported, not restated", "Yes, with filing dates", "Ask"),
            Row("Free tier without a call", "{FREE_CALLS}/month, no card", "Varies"),
        ),
        choose_us=(
            "<b>Check 1: the twenty-three-tags test.</b> JPMorgan&rsquo;s 10-Q "
            "carries more than twenty facts tagged <code>Assets</code>. Ask a "
            "provider for total assets and check it against the filing. A "
            "segment looks exactly like a company until you look.",
            "<b>Check 2: the identity test.</b> Pull assets, liabilities and "
            "equity for fifty filers and test A = L + E on each. Whatever "
            "fails tells you the shape of that provider&rsquo;s error \u2014 "
            "and whether they knew about it.",
            "<b>Check 3: the SPAC test.</b> Find a filer with redeemable "
            "equity \u2014 <a href=\"/company/LCID\">Lucid</a> will do. If "
            "the sheet does not close, the provider read permanent equity and "
            "stopped.",
            "<b>Check 4: the admission test.</b> Ask a provider how many "
            "filings their own pipeline currently cannot read. A provider who "
            "cannot answer has not measured it; one who answers zero has not "
            "looked.",
        ),
        choose_them=(
            "<b>You need income statements and cash flow too.</b> This is "
            "balance sheets. That is a real limit, not modesty.",
            "<b>You need filings themselves, not figures.</b> Full-text "
            "search, exhibits, 8-K monitoring \u2014 different product entirely.",
            "<b>You need an SLA and a vendor questionnaire.</b>",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>Why &ldquo;accuracy&rdquo; is not the question</h2>

<p>Every vendor in this category will tell you their data is accurate, and
almost none will tell you how they know. An accuracy percentage with no method
behind it is a marketing number: it cannot be reproduced, it cannot be audited,
and it is never wrong in a way anybody can point at.</p>

<p>The useful question is the opposite one. <em>What happens on the filings you
cannot read?</em> A provider who answers that honestly has measured it. This
site answers it with a count that moves every time the data reloads, including
the category where the fault is ours rather than the filer&rsquo;s.</p>
""" + _HONESTY.format(rival="the alternatives"),
    ),
    # ------------------------------------------------------------------
    # Added 2026-09-19. Same two rules as the 2026-09-18 batch, both now
    # enforced by tests: every `theirs` cell is read off that vendor's own
    # page or says "check their site", and NO page prints a competitor's
    # price -- `_HONESTY`, rendered below each of these, promises exactly
    # that.
    # ------------------------------------------------------------------
    Page(
        slug="compare/balanceproof-vs-finnhub",
        h1="BalanceProof vs Finnhub: SEC Fundamentals Compared",
        seo_title="BalanceProof vs Finnhub \u2014 SEC Fundamentals (2026)",
        description=(
            "BalanceProof vs Finnhub for SEC balance sheet data: what each "
            "publishes about how a figure is chosen, and which fits a "
            "reconciliation job."
        ),
        rival="Finnhub",
        updated="2026-09-19",
        lede=(
            "Finnhub leads with breadth and a free tier: its own front page "
            "offers \u201creal-time stock, forex and cryptocurrency\u201d "
            "alongside fundamentals, economic and alternative data. That is a "
            "different product shape to this one, and for most projects it is "
            "the more useful shape."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Broad \u2014 equities, forex, crypto, fundamentals, estimates, alternative data", False),
            Row("Selection method", "A = L + E, published and testable", "Check their site \u2014 not found published"),
            Row("Tells you when it is unsure", "Yes \u2014 every exception flagged with its reason, counted at /methodology", "Check their site"),
            Row("Duplicate-tag resolution", "By the identity, not by tag name", "Check their site"),
            Row("Coverage", "{COVERAGE}", "Wider \u2014 many asset classes", False),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Yes \u2014 their front page leads with it; check their site for limits", False),
            Row("Bulk download", "One CSV, no contract", "Check their site"),
        ),
        choose_us=(
            "<b>The balance sheet is the deliverable, not an input.</b> If you "
            "are reconciling, auditing, or defending a figure, a service whose "
            "whole method is published beats one with more endpoints.",
            "<b>You want the exceptions counted in public.</b> "
            "<a href=\"/methodology\">How we verify</a> names every category "
            "and its count, and the count moves with every data load.",
            "<b>Mezzanine and noncontrolling interests are handled out loud.</b> "
            "A filing that only closes once redeemable equity is counted says "
            "so on its own page \u2014 "
            "<a href=\"/company/LCID\">Lucid</a> is the clean example.",
        ),
        choose_them=(
            "<b>You need more than one statement.</b> Prices, forex, crypto, "
            "estimates, alternative data. One endpoint that does one thing is "
            "the wrong shape for that, and this is one endpoint.",
            "<b>Free matters more than reconciled.</b> Finnhub is unusually "
            "generous at the free end and that is a real reason to start there.",
            "<b>You are building a dashboard, not an audit trail.</b>",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="Finnhub"),
    ),
    Page(
        slug="compare/balanceproof-vs-eodhd",
        h1="BalanceProof vs EODHD: SEC Balance Sheets Compared",
        seo_title="BalanceProof vs EODHD \u2014 SEC Balance Sheets (2026)",
        description=(
            "BalanceProof vs EODHD for SEC balance sheet data: global breadth "
            "against a single filing source read carefully, and what each "
            "publishes about its method."
        ),
        rival="EODHD",
        updated="2026-09-19",
        lede=(
            "EODHD is the widest product in this comparison set: 45+ APIs, "
            "global exchanges, and its own site says it aggregates pricing "
            "from \u201c100+ sources\u201d. It also ships a SEC Filings API, "
            "marked beta on their own documentation. Breadth is the whole "
            "proposition, and it is a real one."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "45+ APIs \u2014 stocks, forex, options, indices, fundamentals, filings", False),
            Row("Markets", "US SEC filers", "Global, many exchanges", False),
            Row("SEC filings product", "The whole service", "A SEC Filings API, marked beta on their own site"),
            Row("Selection method", "A = L + E, published and testable", "Check their site \u2014 not found published"),
            Row("Tells you when it is unsure", "Yes \u2014 exception and reason, counted in public", "Check their site"),
            Row("Coverage", "{COVERAGE}", "Wider by every measure", False),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site"),
        ),
        choose_us=(
            "<b>You need one filing read properly, not many markets read "
            "broadly.</b> Aggregating 100+ sources is the right answer to a "
            "coverage problem and no answer at all to a reconciliation one.",
            "<b>Beta is a fine place for a filings product and a poor place "
            "for your audit trail.</b> Their SEC filings API carries that label "
            "on their own documentation; this site has done nothing else since "
            "it started.",
            "<b>You want to check the work.</b> Take any company, call both, "
            "and compare each against EDGAR \u2014 not against each other.",
        ),
        choose_them=(
            "<b>You need non-US markets.</b> Decisive, and nothing here helps.",
            "<b>You need options, forex or indices in the same account.</b>",
            "<b>You want Google Sheets and no-code paths.</b> They ship those; "
            "this is an API and a CSV.",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="EODHD"),
    ),
    Page(
        slug="compare/balanceproof-vs-tiingo",
        h1="BalanceProof vs Tiingo: Two Kinds of Data Quality",
        seo_title="BalanceProof vs Tiingo \u2014 Data Quality (2026)",
        description=(
            "BalanceProof vs Tiingo for SEC fundamentals. Both check their "
            "data; they check different things, and the difference decides "
            "which one you want."
        ),
        rival="Tiingo",
        updated="2026-09-19",
        lede=(
            "This is the most interesting comparison in the set, because "
            "Tiingo also takes data quality seriously \u2014 their site "
            "describes a \u201cproprietary error-checking framework\u201d "
            "with anomaly monitoring and redundant feeds. The question is not "
            "who checks. It is what each one checks FOR."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Prices, news, crypto, forex, fundamentals", False),
            Row("What the quality process targets", "Whether the statement reconciles: A = L + E on each filing", "Error-checking, anomaly monitoring, redundant feeds \u2014 their words"),
            Row("Published as a testable rule", "Yes \u2014 the identity is arithmetic you can rerun", "Described, not specified \u2014 check their site"),
            Row("Per-figure exception reason", "Yes, named on the company's own page", "Check their site"),
            Row("Stated coverage", "{COVERAGE}", "80,000+ assets, 20+ years of fundamentals \u2014 their figures", False),
            Row("News", "None", "70M+ articles, 20+ years \u2014 their figures", False),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site"),
        ),
        choose_us=(
            "<b>Anomaly detection and an identity test answer different "
            "questions.</b> Anomaly detection finds a figure that looks wrong "
            "against its own history. The accounting identity finds one that is "
            "inconsistent with the rest of the same statement \u2014 including "
            "a wrong figure that looks perfectly normal.",
            "<b>You want the rule, not the reassurance.</b> A = L + E is "
            "arithmetic you can rerun on the response; a framework described in "
            "prose is something you have to take on trust.",
            "<b>You need the exception, not the average.</b> Every flagged "
            "filing is named individually, with its reason.",
        ),
        choose_them=(
            "<b>You need prices, news, crypto or forex.</b> Genuinely broad, "
            "and this is a single statement from a single regulator.",
            "<b>Cross-source redundancy is what you are buying.</b> They run "
            "redundant feeds; this site has one source by design, because SEC "
            "filings are the authority rather than a vendor of them.",
            "<b>You want a long track record on price data.</b>",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>Two honest answers to the same worry</h2>

<p>Almost nobody in this category publishes a quality method at all, so a
comparison with one that does is worth being careful about. Tiingo&rsquo;s
approach &mdash; error-checking, anomaly monitoring, redundant feeds &mdash; is
a real discipline aimed at a real failure: a figure that arrives corrupted, or
does not arrive at all.</p>

<p>The accounting identity is aimed at a different failure, and it is the one
this site exists for. A balance sheet that does not satisfy
<strong>A = L + E</strong> is internally inconsistent regardless of whether any
individual number looks unusual &mdash; and the commonest cause is not
corruption but SELECTION: the filing offered twenty tags for total assets and
something picked the wrong one. That figure is clean, plausible, in range, and
wrong, so anomaly detection has nothing to catch.</p>

<p>Neither method subsumes the other. If you are worried about data arriving
damaged, redundancy is the answer. If you are worried about a balance sheet
being read on the wrong terms, the identity is.</p>
""" + _HONESTY.format(rival="Tiingo"),
    ),
    Page(
        slug="compare/balanceproof-vs-nasdaq-data-link",
        h1="BalanceProof vs Nasdaq Data Link: Fundamentals",
        seo_title="BalanceProof vs Nasdaq Data Link \u2014 Fundamentals",
        description=(
            "BalanceProof vs Nasdaq Data Link for SEC fundamentals: a catalogue "
            "of datasets against one statement reconciled, and what to ask "
            "before choosing."
        ),
        rival="Nasdaq Data Link",
        updated="2026-09-19",
        lede=(
            "Nasdaq Data Link is a catalogue rather than a single feed \u2014 "
            "its own page title is \u201cFinancial, Economic and Alternative "
            "Data\u201d. That shape changes the question: you are not choosing "
            "a method so much as choosing a publisher within it, and the method "
            "is then whatever that publisher does."
        ),
        rows=(
            Row("Shape", "One service, one method", "A catalogue of datasets from multiple publishers", False),
            Row("Who guarantees the method", "This site, in public, at /methodology", "Whichever publisher the dataset comes from \u2014 ask them"),
            Row("Selection method", "A = L + E, published and testable", "Varies by dataset \u2014 check the dataset's own documentation"),
            Row("Tells you when it is unsure", "Yes \u2014 exception, reason, and a public count", "Varies by dataset"),
            Row("Coverage", "{COVERAGE}", "Very wide across economics, alternative data and markets", False),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site \u2014 varies by dataset"),
        ),
        choose_us=(
            "<b>You want one answerable party.</b> On a catalogue the method is "
            "the publisher's and the question goes to them; here there is one "
            "method, written down, and one place to complain.",
            "<b>The question you are really asking is about a filing.</b> "
            "SEC balance sheets are not an alternative dataset \u2014 they are "
            "a primary document, free to everyone, where the work is in reading "
            "them correctly.",
            "<b>You need the exceptions, not just the rows.</b>",
        ),
        choose_them=(
            "<b>You need economic or alternative data.</b> Whole categories "
            "this site does not touch and never will.",
            "<b>A catalogue is the right shape for exploration.</b> Trying five "
            "datasets to see which fits is a genuine advantage of a marketplace.",
            "<b>You already buy through Nasdaq.</b> One procurement path has "
            "real value and it is not a technical argument.",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="Nasdaq Data Link"),
    ),
    Page(
        slug="compare/balanceproof-vs-financial-datasets-ai",
        h1="BalanceProof vs Financial Datasets AI: Sampling vs Every Filing",
        seo_title="BalanceProof vs Financial Datasets AI (2026)",
        description=(
            "Both publish a verification method. One audits a sample against "
            "EDGAR; the other tests every filing against the accounting "
            "identity. What that difference actually buys."
        ),
        rival="Financial Datasets AI",
        updated="2026-09-19",
        lede=(
            "The only rival here that publishes a verification method in as "
            "much detail as this site does, so the comparison is unusually "
            "concrete. Their own page describes sampling 1,000 companies across "
            "75 sectors and checking 20,000 data points per audit cycle against "
            "SEC EDGAR. That is a serious process, and it is a different one."
        ),
        rows=(
            Row("Positioning", "A verification layer over SEC balance sheets", "\u201cThe first platform designed for AI agents\u201d \u2014 their words"),
            Row("Verification method", "Every filing tested against A = L + E", "Sampling: 1,000 companies, 20,000 data points per audit cycle, checked against EDGAR \u2014 their figures"),
            Row("What the check covers", "Every balance sheet served, every load", "A sample, per cycle"),
            Row("Result published per company", "Yes \u2014 pass, or the exception and its reason", "Check their site"),
            Row("Stated scope", "{COVERAGE}", "27,530 stocks, 75 sectors, 30+ years \u2014 their figures", False),
            Row("Statements covered", "Balance sheets only", "Balance sheets, cash flow, earnings, insider trades, segments", False),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site"),
        ),
        choose_us=(
            "<b>A sample tells you a rate; a test tells you about YOUR "
            "company.</b> Auditing 1,000 companies establishes that the data is "
            "good in general. It cannot tell you whether the filing you are "
            "about to publish a number from reconciles, because your company "
            "may not be in the sample. Every filing here is tested, and the "
            "answer travels with the response.",
            "<b>The identity needs no sampling because it is free.</b> Checking "
            "A = L + E costs one subtraction per filing, so there is no reason "
            "to do it on a subset \u2014 the economics that make sampling "
            "sensible for a human-audited comparison do not apply.",
            "<b>You need the failures named.</b> This site publishes what it "
            "cannot reconcile, by ticker, with the reason.",
        ),
        choose_them=(
            "<b>You are building for agents and need the whole statement "
            "set.</b> Cash flow, earnings, insider trades, segment breakdowns. "
            "This is balance sheets, and that is a real limit.",
            "<b>Their verification is against the filing itself.</b> That is a "
            "stronger check than an identity test on the points it covers "
            "\u2014 a human reading a 10-Q catches things arithmetic cannot. "
            "The trade is coverage for depth, and it can go either way.",
            "<b>You want one vendor for an AI product.</b>",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>Why this comparison is worth reading carefully</h2>

<p>Most pages like this compare a published method against silence, which is an
easy argument to win and not a very useful one. This one does not. Financial
Datasets AI publishes what it does, in numbers, and those numbers describe real
work.</p>

<p>So the difference is worth stating precisely rather than spun. A sampled
audit against the source document is <em>deeper</em> than an identity test: a
person comparing a figure to the filing catches a misread label, a wrong
period, a footnote that changes the meaning &mdash; none of which arithmetic
notices. An identity test is <em>wider</em>: it runs on every filing, every
load, at no cost, and it catches the one failure that is invisible to
inspection at scale, which is a plausible figure selected from the wrong tag.</p>

<p>If you are choosing between them on this axis alone, the question is whether
you need to know the general quality of a dataset or the specific status of the
filing in front of you. Those are different questions and they have different
right answers.</p>
""" + _HONESTY.format(rival="Financial Datasets AI"),
    ),
    Page(
        slug="alternatives/polygon-io",
        h1="Polygon.io Alternatives for SEC Fundamentals",
        seo_title="Polygon.io Alternatives for SEC Fundamentals",
        description=(
            "Looking for a Polygon.io alternative for fundamentals? Start with "
            "what polygon.io now redirects to, then with whether fundamentals "
            "were ever the product."
        ),
        rival="Polygon.io",
        kind="alternatives",
        updated="2026-09-19",
        lede=(
            "Two things to check before you migrate anything. First: as of "
            "18 September 2026, polygon.io returns a 301 to massive.com \u2014 "
            "verify that yourself, it is one request. Second: fundamentals were "
            "an add-on there rather than the product, so \u201calternative\u201d "
            "may be the wrong frame."
        ),
        rows=(
            Row("If you need market data", "Not the answer \u2014 this is SEC fundamentals only", "Stay, or follow the redirect", False),
            Row("If you need fundamentals", "This is the whole product", "It was an add-on \u2014 check what the destination site offers now"),
            Row("If a balance sheet did not reconcile", "This is what the service is for", "Check their site for a published method"),
            Row("Migration cost", "Low \u2014 add alongside, do not replace", "n/a"),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site"),
        ),
        choose_us=(
            "<b>Do not migrate \u2014 add.</b> If you were using them for "
            "prices and fundamentals, only half of that is replaceable here, "
            "and the half that is costs nothing to trial.",
            "<b>A per-minute cap is the wrong shape for a filings backfill.</b> "
            "Market-data free tiers are metered per minute because quotes "
            "stream; walking a universe of filings is a different access pattern.",
        ),
        choose_them=(
            "<b>Prices, options, indices, futures.</b> Nothing here touches any "
            "of it.",
            "<b>You need real-time anything.</b> Balance sheets change four "
            "times a year.",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>About the redirect</h2>

<p>On 18 September 2026, <code>https://polygon.io</code> and
<code>https://polygon.io/pricing</code> both returned <code>301 Moved
Permanently</code> to <code>massive.com</code>. The destination does not mention
Polygon anywhere and describes itself as &ldquo;modernizing Wall St. one market
at a time&rdquo;.</p>

<p>That is the whole of what was verified, so it is the whole of what is said
here. This page asserts no rebrand, acquisition or shutdown, because none of
those were checked. Confirm it yourself before it informs a procurement
decision &mdash; <code>curl -I https://polygon.io</code> settles it.</p>
""" + _HONESTY.format(rival="Polygon.io"),
    ),
    Page(
        slug="alternatives/alpha-vantage",
        h1="Alpha Vantage Alternatives for SEC Balance Sheets",
        seo_title="Alpha Vantage Alternatives for Balance Sheets",
        description=(
            "Outgrown Alpha Vantage's free tier, or hit a figure you could not "
            "reconcile? Those are different problems with different answers."
        ),
        rival="Alpha Vantage",
        kind="alternatives",
        updated="2026-09-19",
        lede=(
            "People search this for one of two reasons, and they do not lead to "
            "the same place. Either you ran out of free calls, or you found a "
            "number you could not tie back to the filing. Only the second one "
            "is an argument for this site."
        ),
        rows=(
            Row("If you ran out of free calls", "{FREE_CALLS}/month, no card \u2014 a different budget shape", "Their free tier is metered per DAY \u2014 check their site", False),
            Row("If a figure would not tie to the filing", "This is the entire product", "Check their site for a published selection method"),
            Row("If you need prices, FX or crypto", "Not the answer", "Stay \u2014 that is what it is for", False),
            Row("Paid tiers sold in", "Calls per month", "Requests per minute", False),
            Row("Migration cost", "Low \u2014 one endpoint, add alongside", "n/a"),
        ),
        choose_us=(
            "<b>Your problem was a number, not a quota.</b> If a balance sheet "
            "would not tie out, more calls of the same data does not fix it.",
            "<b>Per-month suits a backfill; per-minute suits a dashboard.</b> "
            "Walking every filer once is a monthly-budget job.",
            "<b>You want the failures named.</b> Every exception is on the "
            "company's own page, with its reason.",
        ),
        choose_them=(
            "<b>Your problem WAS the quota.</b> Then the cheapest fix is their "
            "next tier, and nothing here is relevant.",
            "<b>You need technical indicators, FX or crypto.</b>",
            "<b>Your integration already works.</b> A working integration has "
            "real value; \u201cit reconciles\u201d is not on its own a reason "
            "to rip one out.",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="Alpha Vantage"),
    ),
    Page(
        slug="alternatives/sec-api",
        h1="sec-api.io Alternatives: Filings, or the Figures Inside Them",
        seo_title="sec-api.io Alternatives: Filings vs Figures",
        description=(
            "An sec-api.io alternative depends on whether you want the document "
            "or the number. They are different products and only one of them is "
            "this."
        ),
        rival="sec-api.io",
        kind="alternatives",
        updated="2026-09-19",
        lede=(
            "The closest thing to a like-for-like in this category, and the "
            "comparison that most often gets framed wrongly. They give you the "
            "filing. This gives you one statement out of it, resolved and "
            "checked. If you need the document, no part of this page helps."
        ),
        rows=(
            Row("Primary object", "One balance sheet, resolved", "A filing, and everything in it", False),
            Row("Full-text search", "None", "Yes, and good at it", False),
            Row("Form coverage", "Periodic filings only", "Every form type \u2014 8-K, S-1, 13F, Form 4 and more", False),
            Row("Duplicate-tag resolution", "Done for you, by the identity", "Yours to do \u2014 the tags arrive as filed", False),
            Row("Tells you when a statement does not reconcile", "Yes, with the reason", "Not applicable \u2014 it is not selecting for you", False),
            Row("If you already have a reconciler you trust", "You want their raw access, not this", "Stay", False),
            Row("Free tier", "{FREE_CALLS} calls/month, no card", "Check their site"),
        ),
        choose_us=(
            "<b>You do not want to write the resolver.</b> JPMorgan's 10-Q "
            "carries more than twenty facts tagged <code>Assets</code>. Picking "
            "the right one is the work, and it is the work this site does.",
            "<b>You want the identity checked before you see the number.</b>",
            "<b>You want the exceptions counted in public rather than "
            "discovered in your own reconciliation three weeks later.</b>",
        ),
        choose_them=(
            "<b>You need the document.</b> Exhibits, 8-K monitoring, full-text "
            "search, forms this site never touches. Different product, and a "
            "good one.",
            "<b>You want raw tags because you resolve them yourself.</b> That "
            "is a legitimate and often correct choice \u2014 then a service "
            "that has already chosen for you is in the way.",
            "<b>You need filings the moment they land.</b>",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="sec-api.io"),
    ),
    Page(
        slug="best/balance-sheet-api",
        h1="The Best Balance Sheet API: What to Test Before You Buy",
        seo_title="Best Balance Sheet API \u2014 What to Test (2026)",
        description=(
            "Choosing a balance sheet API: four tests you can run on any "
            "provider in an afternoon, including this one, and what each "
            "failure tells you."
        ),
        rival="the alternatives",
        kind="best",
        updated="2026-09-19",
        lede=(
            "Nobody is ranked here. Every provider in this category reads the "
            "same free filings from the same free source, so the only question "
            "worth asking is what each one does when a filing is ambiguous "
            "\u2014 and that is testable in an afternoon."
        ),
        rows=(
            Row("Balance sheet reconciles as served", "Yes \u2014 or the exception is named", "Test it: pull A, L and E for fifty filers"),
            Row("Tells you the basis it reconciled on", "Yes \u2014 plain, NCI, mezzanine, or both", "Ask"),
            Row("Handles redeemable equity", "Yes, as its own block", "Test it on a SPAC or a biotech"),
            Row("As-reported, not restated", "Yes, with filing dates", "Ask \u2014 many serve restated silently"),
            Row("Names the filings it cannot read", "Yes, by ticker, with the reason", "Rare"),
            Row("Coverage", "{COVERAGE}", "Ask \u2014 and ask whether it is SEC-only"),
            Row("Free tier without a sales call", "Yes", "Varies"),
        ),
        choose_us=(
            "<b>Test 1: does it add up?</b> Pull assets, liabilities and equity "
            "for fifty companies and check A = L + E on each. Whatever fails "
            "tells you the shape of that provider's error. If nothing fails, "
            "check whether they adjusted something to make it so.",
            "<b>Test 2: the redeemable-equity test.</b> Pick a filer with "
            "mezzanine equity \u2014 <a href=\"/company/LCID\">Lucid</a> "
            "works. A provider reading permanent equity alone is short the "
            "whole block, and nothing raises an error.",
            "<b>Test 3: the restatement test.</b> Find a company that revised a "
            "figure and ask for the original period. A revised number with no "
            "filing date attached cannot be backtested against \u2014 you would "
            "be trading on information that did not exist yet.",
            "<b>Test 4: ask what they cannot read.</b> A provider who cannot "
            "answer has not measured it. One who answers zero has not looked.",
        ),
        choose_them=(
            "<b>You need the income statement and cash flow too.</b> This is "
            "balance sheets. That is a limit, not modesty.",
            "<b>You need non-US filers.</b>",
            "<b>You need an SLA and a vendor questionnaire.</b>",
        ),
        body=_TRANSPARENCY + _METHOD + _HONESTY.format(rival="the alternatives"),
    ),
    Page(
        slug="best/free-financial-statement-api",
        h1="The Best Free Financial Statement API: Reading the Fine Print",
        seo_title="Best Free Financial Statement API (2026)",
        description=(
            "Free financial statement APIs compared on what free actually means: "
            "how the quota is metered, what a card requirement signals, and what "
            "you give up."
        ),
        rival="the alternatives",
        kind="best",
        updated="2026-09-19",
        lede=(
            "Every provider in this category has a free tier, and they are not "
            "comparable. What matters is how the quota is METERED, because that "
            "decides whether the tier fits the job you actually have."
        ),
        rows=(
            Row("Metered by", "Calls per month \u2014 suits a backfill", "Often per minute or per day \u2014 check before you plan around it"),
            Row("Card required", "No", "Varies \u2014 a card requirement is a signal, not a cost"),
            Row("Sales call required", "No", "Varies"),
            Row("What the free tier omits", "Nothing \u2014 same data, same checks, fewer calls", "Ask: free tiers often serve delayed or reduced data"),
            Row("Reconciliation included free", "Yes \u2014 it is not a paid feature", "Rare"),
            Row("Bulk export", "Paid, one-off, no contract", "Varies"),
            Row("Coverage on the free tier", "{COVERAGE} \u2014 the full universe", "Ask \u2014 some restrict the free tier's symbol list", False),
        ),
        choose_us=(
            "<b>Per-month is the shape a backfill needs.</b> A per-minute cap "
            "is built for streaming quotes. Walking every filer once, slowly, is "
            "a monthly-budget job, and a per-minute tier makes it take days for "
            "no reason.",
            "<b>Check what the free tier quietly removes.</b> Delayed data, a "
            "reduced symbol list, or verification held back for paying users are "
            "all common. Here the free tier is the same data with the same "
            "checks and fewer calls.",
            "<b>A card on a free tier is information.</b> It usually means the "
            "tier is a trial. Worth knowing before you build on it.",
        ),
        choose_them=(
            "<b>You need breadth more than depth.</b> A free tier covering "
            "prices, FX and crypto beats one covering balance sheets, if "
            "breadth is the job.",
            "<b>You need a higher rate, not a higher total.</b> Per-minute tiers "
            "exist because some jobs are genuinely rate-shaped.",
            "<b>Free forever matters more than free tier.</b> SEC filings are "
            "public: EDGAR is free and always will be. Everything any provider "
            "here charges for is the reading, not the data.",
        ),
        body=_TRANSPARENCY + _METHOD + """
<h2>The honest bottom of this page</h2>

<p>The filings themselves cost nothing. SEC EDGAR is free, public, and
downloadable in bulk by anyone, which means every provider in this category
&mdash; including this one &mdash; is charging for the reading rather than the
data. That is worth saying on a page about free tiers, because it tells you what
you are actually evaluating.</p>

<p>If your volume is low and your patience is high, EDGAR plus a weekend is a
real option and nobody should pretend otherwise. What you would be building is
the resolver: the thing that decides which of twenty-odd facts tagged
<code>Assets</code> is the company. That is the job, and it is the only reason
to pay anybody for this.</p>
""" + _HONESTY.format(rival="the alternatives"),
    ),
)

BY_SLUG: dict[str, Page] = {p.slug: p for p in PAGES}


def _plain(html: str) -> str:
    """Markup out, entities decoded. For text going into structured data.

    This stripped `<b>` and nothing else, which held exactly as long as
    `choose_us` contained nothing else. The moment a test cited a company page
    the JSON-LD started carrying a raw anchor tag as its item text, and a
    search engine reads that literally -- the structured data said
    `<a href="/company/LCID">Lucid</a>` where it meant "Lucid".
    """
    import html as _html
    import re as _re

    return _html.unescape(_re.sub(r"<[^>]+>", "", html)).strip()


def _ld(page: Page) -> str:
    from src.report.schema import breadcrumb_ld, pricing_ld

    trail = [("Home", "/")]
    head, _, tail = page.slug.partition("/")
    trail.append((head.title(), f"/{head}"))
    trail.append((page.h1, f"/{page.slug}"))
    crumbs = breadcrumb_ld(trail)

    if page.kind == "best":
        # An ItemList of the tests, not of products. Ranking other companies in
        # structured data would be asserting an order I have not measured.
        from src.report.schema import itemlist_ld

        # `name` comes from the page, not a literal. It used to be hardcoded to
        # the quants roundup, which was right while that was the only `best`
        # page and became wrong the moment there were three -- search engines
        # would have been handed one identical list name for three different
        # lists.
        return itemlist_ld(
            name=page.h1,
            items=[_plain(t) for t in page.choose_us],
        ) + pricing_ld() + crumbs
    return pricing_ld() + crumbs


# The sentence that makes each cross-link worth following, per page.
#
# Hand-written and not derived from `description`: a nav block where every
# line is the first sentence of the target's meta description reads as
# machinery, and a reader skips machinery. This is the shortest honest answer
# to "why would I click that instead of staying here".
_WHY_THAT_ONE: dict[str, str] = {
    "compare/balanceproof-vs-intrinio":
        "A broad multi-asset data vendor. The comparison is scope against "
        "depth.",
    "compare/balanceproof-vs-sec-api":
        "The closest thing to a like-for-like: another SEC-only API, "
        "compared on how each one resolves a filing.",
    "compare/balanceproof-vs-xignite":
        "Enterprise market data with fundamentals attached. Different buyer, "
        "different contract.",
    "best/sec-filings-api-for-quants":
        "The roundup rather than a head-to-head, if you have not shortlisted "
        "yet.",
    "alternatives/intrinio":
        "If Intrinio is the incumbent you are replacing, this is the "
        "migration-shaped version of the question.",
    "compare/balanceproof-vs-financial-modeling-prep":
        "The feed under a lot of retail tooling. Breadth against a referee.",
    "compare/balanceproof-vs-polygon":
        "Market data first, fundamentals as an add-on \u2014 and polygon.io "
        "now redirects elsewhere.",
    "compare/balanceproof-vs-alpha-vantage":
        "The default free answer. Worth reading for where free stops being "
        "the constraint.",
    "alternatives/financial-modeling-prep":
        "If FMP is what you already have, this is the add-it-alongside "
        "version rather than a migration.",
    "best/sec-xbrl-api":
        "Four checks to run against any provider in this category, this one "
        "included.",
    "compare/balanceproof-vs-finnhub":
        "Breadth and a generous free tier. Different product shape.",
    "compare/balanceproof-vs-eodhd":
        "The widest of the set \u2014 global markets, 45+ APIs, SEC filings "
        "in beta.",
    "compare/balanceproof-vs-tiingo":
        "They check their data too. Worth reading for WHAT each one checks for.",
    "compare/balanceproof-vs-nasdaq-data-link":
        "A catalogue rather than a feed, so the method is the publisher's.",
    "compare/balanceproof-vs-financial-datasets-ai":
        "The only rival that publishes a verification method in comparable "
        "detail. Sampling against every filing.",
    "alternatives/polygon-io":
        "Check where polygon.io now redirects before you migrate anything.",
    "alternatives/alpha-vantage":
        "Ran out of free calls, or found a number that would not tie out? "
        "Different answers.",
    "alternatives/sec-api":
        "The document or the figures inside it \u2014 only one of those is "
        "this site.",
    "best/balance-sheet-api":
        "Four tests you can run on any provider in an afternoon.",
    "best/free-financial-statement-api":
        "What \u201cfree\u201d actually means once you read how the quota is "
        "metered.",
}


def _other_comparisons(page: Page) -> str:
    """Every other page in this family, from every page in this family.

    Five pages that answer one question between them, and until this existed
    each was reachable only from the sitemap: a reader who landed on the
    Intrinio comparison from search had no way to discover the roundup that
    would actually have answered them. Cross-linking them is the cheapest fix
    for the worst version of that, which is a dead end on the page with the
    most commercial intent on the site.
    """
    others = [p for p in PAGES if p.slug != page.slug]
    if not others:
        return ""
    items = "".join(
        f'<li><a href="/{p.slug}">{escape(p.h1)}</a> &mdash; '
        f"{escape(_WHY_THAT_ONE.get(p.slug, ''))}</li>"
        for p in others
    )
    return f"""
  <section class="sec" id="compare-others">
    <div class="sec-head"><h2>Compare</h2></div>
    <p class="sec-sub">The same question from the other directions. Every one
      of these compares on method, for the reason at the top of this page.</p>
    <ul class="notelist">{items}</ul>
  </section>"""


def live_counts() -> dict[str, str]:
    """The coverage phrases, counted from the table at render time.

    Whole phrases and not bare numbers, so that a count which cannot be read
    degrades into a sentence rather than into a hole: "SEC filers" is true on
    the worst day this can have, and "" is not.
    """
    from src.dataset import facts_label
    from src.report.home_page import companies_label

    companies = companies_label()
    facts = facts_label()
    filers = f"{companies} SEC filers" if companies else "SEC filers"
    # THE FREE TIER, READ FROM SETTINGS RATHER THAN TYPED. It was typed --
    # "{FREE_CALLS} calls/month, no card" on all twenty of these pages and in the
    # hero -- while production runs FREE_TIER_MONTHLY_CALLS=100. The number in
    # the copy was the code's DEFAULT, not the deployment's value, so the site
    # advertised ten times the free tier it actually grants. A figure a
    # deployment can change must never be written by hand.
    from src.config.settings import get_settings

    free_calls = get_settings().free_tier_monthly_calls
    return {
        "{FREE_CALLS}": f"{free_calls:,}",
        "{FILERS}": filers,
        "{COVERAGE}": f"{filers}, {facts} facts" if facts else filers,
        "{COVERAGE_PROSE}": (
            f"{filers}, {facts} as-reported facts" if facts
            else f"{filers}, as reported"
        ),
    }


def _fill_counts(html: str) -> str:
    """Substituted on the finished page, so the table, the prose and the
    metadata are all filled by the one pass and none of them can be missed."""
    for token, value in live_counts().items():
        html = html.replace(token, value)
    return html


def render(page: Page, *, nav: str = "") -> str:
    from src.report.nav import render_footer

    # Built outside the f-string: an apostrophe needs an escape, and Python
    # 3.11 f-strings do not allow a backslash inside the expression part.
    footer = render_footer(
        "Comparisons describe METHOD, not another company’s prices — "
        "those change and this page does not. Verify any competitor detail on "
        "their own site."
    )

    body = f"""{nav}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <h1 class="htitle">{escape(page.h1)}</h1>
      <p class="hlede">{escape(page.lede)}</p>
    </header>

    <section class="sec" id="at-a-glance">
      <div class="sec-head"><h2>At a glance</h2></div>
      <p class="sec-sub">Prices and features on the right-hand column change,
        and this page does not — check their site before you decide anything on
        it. The rows about method are the ones that stay true.</p>
      {_table(page)}
    </section>

    <div class="prose">{page.body}</div>
  </article>

  {_choose(page)}

  <aside class="sec cta">
    <div class="sec-head"><h2>Check it yourself</h2></div>
    <p class="sec-sub">Free tier, no card. Take a company where you already
      know the answer and compare the response against the filing.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a free API key</a>
      <a class="btn ghost" href="/pricing">Pricing</a>
      <a class="btn ghost" href="/api">API docs</a>
      <a class="btn ghost" href="/dataset">Full dataset</a>
    </div>
  </aside>

  <section class="sec" id="the-method">
    <div class="sec-head"><h2>The method, written out</h2></div>
    <p class="sec-sub">These pages compare on METHOD because method is the part
      that stays true. Both of these are the method itself rather than an
      argument about it, so you can judge the claim rather than take it.</p>
    <ul class="notelist">
      <li><a href="/blog/sec-xbrl-data-wrong-one-in-five">Why SEC XBRL data is
        wrong one time in five</a> — the measurement behind the duplicate-tag
        problem, on JPMorgan's own filing.</li>
      <li><a href="/blog/understanding-the-accounting-identity">Understanding
        the accounting identity</a> — why A = L + E works as a test on data you
        did not produce, and what happens when a filing genuinely fails it.</li>
    </ul>
  </section>

{_other_comparisons(page)}

{footer}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>"""

    return _fill_counts(shell(
        page.seo_title,
        body,
        description=page.description,
        canonical=f"/{page.slug}",
        ld=_ld(page),
    ))
