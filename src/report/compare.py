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
# These are constants rather than a live read of `stats.counts()`, and that is
# a deliberate trade. `counts()` is an uncached COUNT(*) plus COUNT(DISTINCT)
# over the 1.24M-row fundamentals table -- it is already the single largest
# cost on the home page, and putting it on five more would be spending a page
# render on a number that moves four times a year.
#
# The price of that trade is drift, so: every value below was read off the LIVE
# SITE on 2026-09-09 and matches what the home page, /api and llms.txt print
# today. When the quarterly load moves them, they move here too.
COMPANIES = "6,201"
DATA_POINTS = "1.7 million"
ACCURACY = "99.9%"
NAIVE_ACCURACY = "78.6%"

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


_METHOD = f"""
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
approach agrees with the consolidated figure about <strong>{NAIVE_ACCURACY}</strong> of
the time. Roughly one filing in five.</p>

<p>To Scale resolves it with arithmetic rather than a heuristic: pull every
candidate for assets, liabilities and equity, and keep the combination that
satisfies <strong>Assets = Liabilities + Equity</strong>. The consolidated
figures balance against each other. A segment's assets do not balance against
the whole company's liabilities. The identity is a test, not a guideline, and
it is the reason the number is <strong>{ACCURACY}</strong> rather than
{NAIVE_ACCURACY}.</p>

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
        <caption class="vh">To Scale compared with {escape(page.rival)}</caption>
        <thead>
          <tr><th scope="col">&nbsp;</th>
              <th scope="col">To Scale</th>
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
      {half("Choose To Scale", "If the number has to be right.", page.choose_us)}
      {half(f"Choose {page.rival}", "If any of these is you.", page.choose_them)}
    </div>
  </section>"""


PAGES: tuple[Page, ...] = (
    Page(
        slug="compare/to-scale-vs-intrinio",
        h1="To Scale vs Intrinio: SEC Filings API Comparison",
        seo_title="To Scale vs Intrinio — SEC Filings API Comparison (2026)",
        description=(
            "An honest comparison of To Scale and Intrinio for SEC XBRL "
            "balance sheet data: reconciliation method, coverage, pricing "
            "model and when each is the right choice for a quant team."
        ),
        rival="Intrinio",
        lede=(
            "Intrinio is a broad financial data platform. To Scale does one "
            "thing. That is the entire comparison, and which way it cuts "
            "depends on what you are building."
        ),
        rows=(
            Row("Scope", "SEC balance sheets, reconciled", "Broad: fundamentals, prices, options, news, ESG", False),
            Row("Selection method", "A = L + E, published and testable", "Not publicly documented"),
            Row("Stated accuracy", f"{ACCURACY}, measured against the identity", "Not published as a figure"),
            Row("Coverage", f"{COMPANIES} SEC filers, {DATA_POINTS} facts", "Wider — many asset classes and vendors", False),
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
        body=_METHOD + _HONESTY.format(rival="Intrinio"),
    ),
    Page(
        slug="compare/to-scale-vs-sec-api",
        h1="To Scale vs sec-api.io: Which SEC XBRL API Should You Use?",
        seo_title="To Scale vs sec-api.io — SEC XBRL API Comparison",
        description=(
            "To Scale vs sec-api.io for SEC filings data: filing access versus "
            "reconciled financials, what each returns, and which one fits a "
            "screener, a backtest or a filings pipeline."
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
            Row("Stated accuracy", f"{ACCURACY} against the identity", "Not applicable — it is not selecting for you"),
            Row("Coverage", f"{COMPANIES} SEC filers, {DATA_POINTS} facts", "Every EDGAR filing, all form types", False),
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
        body=_METHOD + _HONESTY.format(rival="sec-api.io"),
    ),
    Page(
        slug="compare/to-scale-vs-xignite",
        h1="To Scale vs Xignite: Financial Data API Comparison",
        seo_title="To Scale vs Xignite — Financial Data API for Fundamentals",
        description=(
            "To Scale vs Xignite for fundamentals: an independent SEC XBRL "
            "reconciler against an enterprise market-data platform. Pricing "
            "model, coverage, accuracy method and who each one is built for."
        ),
        rival="Xignite",
        lede=(
            "Xignite sells market data to institutions at institutional scale. "
            "To Scale sells one reconciled dataset to whoever wants it. If you "
            "are choosing between these two you probably already know which "
            "side of that line you are on."
        ),
        rows=(
            Row("Built for", "Individual quants and small teams", "Enterprises, brokerages, banks", False),
            Row("Buying process", "Card, self-serve, thirty seconds", "Sales cycle, contract, procurement", False),
            Row("Asset classes", "SEC balance sheets only", "Equities, FX, rates, ETFs, indices and more", False),
            Row("Selection method", "A = L + E, published and testable", "Not publicly documented"),
            Row("Stated accuracy", f"{ACCURACY} against the identity", "Not published as a figure"),
            Row("Coverage", f"{COMPANIES} SEC filers, {DATA_POINTS} facts", "Global, many asset classes", False),
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
        body=_METHOD + _HONESTY.format(rival="Xignite"),
    ),
    Page(
        slug="best/sec-filings-api-for-quants",
        h1="The Best SEC Filings API for Quants: How to Actually Choose One",
        seo_title="Best SEC Filings API for Quants — How to Choose (2026)",
        description=(
            "How to evaluate an SEC filings API for quantitative work: the "
            "duplicate-tag problem, as-reported versus restated, point-in-time "
            "correctness, and the four tests to run before you buy."
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
            Row("Publishes an accuracy figure", f"Yes — {ACCURACY}", "Rare"),
            Row("Free tier without a call", "Yes", "Varies"),
            Row("Coverage", f"{COMPANIES} SEC filers, {DATA_POINTS} facts", "Ask — and ask whether it is SEC-only"),
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
<strong>""" + NAIVE_ACCURACY + """</strong> of the time. One filing in five is
wrong, silently, by whatever the largest segment happens to be.</p>

<h2>The three questions that actually separate providers</h2>

<p><strong>How do you pick which tag is the right one?</strong> If the answer
is vague, that is your answer. This is the entire difficulty of the category
and a provider who has solved it will tell you how. To Scale uses the
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

<h2>What To Scale is, plainly</h2>

<p>One reconciled dataset: """ + COMPANIES + """ SEC filers, """ + DATA_POINTS + """
as-reported facts, every one checked against A = L + E before it is stored.
""" + ACCURACY + """ against that test. Free tier with no card, $49 a month for
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
            "Looking for an Intrinio alternative for SEC fundamentals? What to "
            "look for in a replacement, when a single-purpose XBRL reconciler "
            "is the better fit, and when it is not."
        ),
        rival="Intrinio",
        kind="alternatives",
        lede=(
            "People usually go looking for an alternative for one of three "
            "reasons: price, scope, or they want to know how the number was "
            "chosen. Only the third one is a reason to pick this."
        ),
        rows=(
            Row("Scope", "SEC balance sheets only", "Broad platform, many feeds", False),
            Row("Pricing model", "Published, self-serve, card", "Quote-based, tiered — check their site"),
            Row("Selection method", "A = L + E, published", "Not publicly documented"),
            Row("Entry price", "Free, then $49/mo", "Check their site"),
            Row("Bulk file", "$79.99 one-off", "Higher plans", False),
            Row("Stated accuracy", f"{ACCURACY} against the identity", "Not published as a figure"),
            Row("Latency", LATENCY, "Varies by feed — check their site"),
            Row("Coverage", f"{COMPANIES} SEC filers", "Wider, including non-SEC", False),
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
""" + _METHOD + """
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
)

BY_SLUG: dict[str, Page] = {p.slug: p for p in PAGES}


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

        return itemlist_ld(
            name="How to choose an SEC filings API for quantitative work",
            items=[t.replace("<b>", "").replace("</b>", "") for t in page.choose_us],
        ) + pricing_ld() + crumbs
    return pricing_ld() + crumbs


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
    <p class="plan-note">More on the method:
      <a href="/blog/sec-xbrl-data-wrong-one-in-five">why SEC XBRL data is
      wrong one time in five</a>.</p>
  </aside>

{footer}
</main>
<script src="/static/nav.js?v={asset_version()}" defer></script>"""

    return shell(
        page.seo_title,
        body,
        description=page.description,
        canonical=f"/{page.slug}",
        ld=_ld(page),
    )
