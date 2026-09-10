"""The front door: a search box, a sentence, and five real balance sheets.

The hero is not a headline about the product -- it IS the product, five times,
at thumbnail size. A bank, a software company, a retailer, a miner and an
airline drawn to their own true proportions look nothing like each other, and
that contrast is the entire argument. Saying "companies are shaped differently"
and drawing five differently-shaped companies are not the same claim, and only
one of them is evidence.

Every thumbnail comes from the same `build_view1` the full page uses, so a
thumbnail can never show a shape the page then contradicts. A ticker whose
drawing cannot be built is offered without one rather than with a placeholder.
"""

from __future__ import annotations

from html import escape

import structlog

from src.company.suggest import Suggestion
from src.company.view1 import View1
from src.report.company_page import (
    _band,
    _legend_rows,
    asset_version,
    block_colour,
    money,
)

log = structlog.get_logger(__name__)

# Thumbnail height in px. Tall enough that a 5%-of-assets band is still a
# visible sliver, short enough that five of them fit on a phone screen.
_THUMB_PX = 88.0


def _mini_stack(blocks: list[dict]) -> str:
    """One column of a thumbnail. No labels -- at this size a label would be a
    smudge, and the shape is the whole point."""
    out = []
    for b in blocks:
        h = max(b["pct"] / 100.0 * _THUMB_PX, 1.5)
        cls = "mband rem" if b["is_remainder"] else "mband"
        out.append(
            f'<i class="{cls}" style="height:{h:.1f}px;'
            f'background-color:{block_colour(b)}"></i>'
        )
    return "".join(out)


def _mini_claims(blocks: list[dict]) -> tuple[str, str]:
    """Claims split at the baseline, exactly as the full drawing splits them.

    Negative equity hangs below the line on the page; a thumbnail that quietly
    stacked it on top would show a company owing more than it owns as if it
    balanced, which is the one shape that must not be smoothed over.
    """
    above = [b for b in blocks if not (b["kind"] == "equity" and b["value"] < 0)]
    below = [b for b in blocks if b["kind"] == "equity" and b["value"] < 0]
    return _mini_stack(above), _mini_stack(below)


def _distinctiveness(block: dict, coverage: dict[str, float]) -> float:
    """How much this line says about THIS company rather than companies.

    Two terms, both read straight off the data:

      rarity   1 - (share of companies that file this metric at all). Almost
               nobody files loans or deposits, so filing them is most of what
               there is to say. Almost everybody files property & equipment.
      size     the block's share of the balance sheet, so a company that is
               70% property still leads with property even though property is
               a common line.

    Added rather than multiplied: a rare line stays interesting when it is
    small, and a common line becomes interesting when it is enormous. Picking
    the largest block instead gave four of the five cards "Property &
    equipment", which contradicted the sentence directly above them.
    """
    rarity = 1.0 - coverage.get(block["key"], 1.0)
    return rarity + min(block["pct"], 100.0) / 100.0


def _headline_fact(view: View1, coverage: dict[str, float] | None = None) -> str:
    """The most distinctive line on one drawing, from either column.

    Read off the drawing rather than composed, and named blocks only: the
    remainder is a leftover, not a line anyone filed. A filer that breaks
    nothing out is told so plainly instead.
    """
    d = view.as_dict()
    blocks = d["assets"] + d["claims"]

    # Owing more than you own is categorical, not a magnitude, and it is the
    # single most unusual thing a balance sheet can say. It wins outright.
    for b in blocks:
        if b["kind"] == "equity" and b["value"] < 0:
            return f"Owes {money(abs(b['value']))} more than it owns"

    named = [b for b in blocks if not b["is_remainder"]]
    if not named:
        rem = max(d["assets"], key=lambda b: b["pct"], default=None)
        return f"{rem['pct']:.0f}% not broken out" if rem else ""

    cov = coverage or {}
    # Ties break toward the larger block, so the choice is never arbitrary.
    best = max(named, key=lambda b: (_distinctiveness(b, cov), b["pct"]))
    return f"{escape(best['label'])} {best['pct']:.0f}%"


def headline_facts(
    views: list[View1 | None], coverage: dict[str, float] | None = None
) -> list[str]:
    """One fact per card, and no two the same.

    Scoring each card independently is not enough: several companies can share
    a most-distinctive line, and the page then prints "Property & equipment"
    four times directly under a sentence promising they look nothing alike.

    So the labels are assigned across the whole set at once. Every
    (card, block) pairing is scored, the strongest pairing anywhere is settled
    first, and each label is then spent -- so property goes to the company that
    is most made of property, and the next one down moves to its own second
    line rather than repeating. Nothing is invented: each card still shows a
    real block off its own drawing, with its own filed share.
    """
    cov = coverage or {}
    facts: list[str] = [""] * len(views)
    pairings: list[tuple[float, int, dict]] = []

    for i, view in enumerate(views):
        if view is None:
            continue
        d = view.as_dict()
        blocks = d["assets"] + d["claims"]
        negative = next(
            (b for b in blocks if b["kind"] == "equity" and b["value"] < 0), None
        )
        if negative is not None:
            facts[i] = f"Owes {money(abs(negative['value']))} more than it owns"
            continue
        named = [b for b in blocks if not b["is_remainder"]]
        if not named:
            rem = max(d["assets"], key=lambda b: b["pct"], default=None)
            facts[i] = f"{rem['pct']:.0f}% not broken out" if rem else ""
            continue
        for b in named:
            pairings.append((_distinctiveness(b, cov), i, b))

    # Strongest pairing anywhere first. The index breaks score ties so the
    # result is stable rather than dependent on dict ordering.
    pairings.sort(key=lambda p: (-p[0], p[1], p[2]["label"]))
    taken_labels: set[str] = set()
    for _score, i, b in pairings:
        if facts[i] or b["label"] in taken_labels:
            continue
        facts[i] = f"{escape(b['label'])} {b['pct']:.0f}%"
        taken_labels.add(b["label"])

    # A card whose every line was claimed by a stronger card still gets its own
    # best line. A duplicate label beats a blank one.
    for i, view in enumerate(views):
        if facts[i] or view is None:
            continue
        facts[i] = _headline_fact(view, cov)
    return facts


def _card(s: Suggestion, view: View1 | None, fact: str = "") -> str:
    kind = f'<span class="ckind">{escape(s.kind)}</span>' if s.kind else ""
    if view is None:
        # Offered, but honestly: no drawing rather than an empty frame.
        return (
            f'<a class="card bare" href="/company/{escape(s.ticker)}">'
            f'<span class="ctick">{escape(s.ticker)}</span>{kind}</a>'
        )
    d = view.as_dict()
    above, below = _mini_claims(d["claims"])
    under = f'<span class="munder">{below}</span>' if below else ""
    return (
        f'<a class="card" href="/company/{escape(s.ticker)}">'
        f'<span class="chead2"><span class="ctick">{escape(s.ticker)}</span>{kind}</span>'
        f'<span class="thumb">'
        f'<span class="mcol">{_mini_stack(d["assets"])}</span>'
        f'<span class="mcol">{above}{under}</span>'
        f"</span>"
        f'<span class="cfact">{fact}</span>'
        f'<span class="csize">{money(d["total_assets"])} of assets</span>'
        "</a>"
    )


def fmt_int(n: int) -> str:
    return f"{n:,}"


def plural(n: int, one: str, many: str) -> str:
    """These counts are read live, so "1 companies" is a real state to reach --
    on a fresh database, or the first load after a wipe."""
    return one if n == 1 else many


def companies_count() -> int:
    """How many filers the site holds. 0 when it cannot be read.

    The int, for callers that need one -- `dataset_ld(companies=...)` took a
    literal `6201` as an ARGUMENT, which is the hardest kind of stale figure to
    find: it is not a string, so no grep for "6,201" in the copy turns it up,
    and it lands in structured data that engines quote back verbatim.
    """
    try:
        from src.company.stats import counts

        return int(counts().get("companies") or 0)
    except Exception:  # noqa: BLE001 - copy must not take a page down
        return 0


def companies_label() -> str:
    """How many filers the site holds, formatted for prose. "" if unreadable.

    Beside `dataset.facts_label` for the same reason: "6,201 companies" was
    typed into six files by hand and every one of them is a claim about a
    number that moves every quarter.
    """
    n = companies_count()
    return f"{n:,}" if n > 0 else ""


def _compact(n: int) -> str:
    """1,237,331 -> "1.2M". The summary line is read at a glance; the exact
    digits are further down the page, where there is room to say what they
    count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:,}"


def _accuracy_banner() -> str:
    """The differentiator, stated as two numbers side by side.

    The claim being made is a comparison, so both halves have to be on screen:
    "99.9% accurate" alone is a number with nothing to be better than. The
    second line names the specific bug behind the gap, because a reader who has
    handled XBRL knows exactly what "the same tag 23 times in one filing" means
    and a reader who has not learns what the problem even was.

    Deliberately not the same figure as the live reconcile rate in the stat line
    below it: that one is computed from the database every hour and moves, this
    one is the fixed claim about the extraction method.
    """
    return """
  <div class="acc">
    <div class="acc-pair"><span class="acc-k">Industry standard</span>
      <span class="acc-v">78.6%</span></div>
    <div class="acc-pair us"><span class="acc-k">To Scale</span>
      <span class="acc-v">99.9%</span></div>
    <p class="acc-why">Accounting-identity accuracy. The gap is the SEC
      duplicate-tag problem — JPMorgan reports “Total Assets” 23 times in one
      filing, once per segment and subsidiary — solved by isolating the
      consolidated row.
      <a href="/blog/sec-xbrl-data-wrong-one-in-five">Why XBRL data is wrong one
      time in five</a>, and <a href="/dashboard">get the data</a>.</p>
    <!-- A = L + E is an identity, so a number below 100% is a claim that needs
         a reason rather than a rounding flourish. Saying what the 0.1% IS, next
         to the figure, is the difference between a measurement and a boast. -->
    <p class="acc-why">Every valid filing we ingest reconciles to that identity.
      The 0.1% that do not are flagged with the exact reason — noncontrolling
      interests, mezzanine equity, rounding, or a genuinely broken filing —
      never silently fudged. We surface the reason; we don’t hide it.
      <a href="/methodology">How the check works</a>.</p>
  </div>"""


def _pricing(stats: dict) -> str:
    """Four cards, rendered by `pricing_page.plan_cards`.

    The markup used to live here. It moved because the home page and /pricing
    must not be able to disagree about what a plan is -- and they would, the
    first time one was edited and the other was not. There is now exactly one
    place a price or a promise about a plan is written, and both pages render
    it.

    Every number is read from settings and from the live database rather than
    typed into the copy. A price quoted in HTML is a price that disagrees with
    the one Stripe charges the first time either changes, and on this page the
    disagreement would be with the figure a buyer is about to act on. The
    annual saving is the same: worked out from the two prices, so it cannot
    survive a change to either one as a claim that is no longer true.
    """
    from src.config.settings import get_settings, price_label
    from src.report.pricing_page import plan_cards, snapshot_date

    s = get_settings()
    rows = _compact(stats.get("facts") or 0) if stats.get("facts") else None
    saving = max(0, s.pro_price_usd * 12 - s.pro_annual_price_usd)

    return f"""
  <section class="sec" id="pricing">
    <div class="sec-head"><h2>Pricing</h2></div>
    <p class="sec-sub">The drawings are free and always will be. The
      machine-readable version is what costs money — and it comes two ways. The
      <b>dataset is a photograph</b>: one CSV, downloaded once, fixed forever.
      The <b>API is a window</b>: live data, current every time you call it.
      <a href="/pricing">Full comparison and FAQ</a>.</p>
    {plan_cards(
        free_limit=s.free_tier_monthly_calls,
        pro_limit=s.pro_tier_monthly_calls,
        pro_price=price_label(s.pro_price_usd),
        pro_annual_price=price_label(s.pro_annual_price_usd),
        annual_saving=price_label(saving),
        dataset_price=price_label(s.dataset_price_usd),
        dataset_rows=rows or "Every",
        dataset_as_of=snapshot_date(stats.get("generated")),
    )}
    <p class="formnote" id="plan-note" role="status" aria-live="polite"></p>
    <p class="plan-note">Paid plans go through Stripe. Your card details are
      entered on Stripe's page and never reach this site.
      <a href="/pricing#faq">What is the difference between the dataset and the
      API?</a></p>
  </section>"""


def _demo_section() -> str:
    """A ticker box wired to the live API, for someone who wants to see the JSON
    before they take a key.

    The markup renders whether or not the demo is configured and whether or not
    the script arrives; the script only fills the output panel. Nothing on this
    page's critical path (search, the drawings) depends on it.
    """
    from src.config.settings import get_settings

    # Read, not written into the copy -- the panel below reports "1 of 3" from
    # the same setting, and a sentence promising five above a counter that stops
    # at three is the sort of small lie that costs a reader their trust in the
    # numbers this whole site is about.
    limit = get_settings().demo_calls_per_ip_per_day
    allowance = (
        f"{limit} {plural(limit, 'company', 'companies')} a day from one address."
        if limit
        else "As many companies as you like — there is no limit on looking."
    )
    return f"""
  <section class="sec" id="demo">
    <div class="sec-head"><h2>Live demo</h2></div>
    <p class="sec-sub">The real endpoint, the real data, no key needed.
      {allowance}</p>
    <form class="search" id="demo-form">
      <label class="slabel" for="demo-ticker">Ticker</label>
      <div class="sfield">
        <input id="demo-ticker" name="ticker" type="text" value="JPM"
          placeholder="JPM" autocomplete="off" autocapitalize="characters"
          spellcheck="false" maxlength="16" enterkeyhint="go">
        <button type="submit" id="demo-btn">Call the API</button>
      </div>
    </form>
    <p class="formnote" id="demo-note" role="status" aria-live="polite"></p>
    <pre class="code json" id="demo-out" hidden><code></code></pre>
  </section>"""


def _summary_line(stats: dict) -> str:
    """One line of scale, and a jump to the explanation further down.

    Each clause is dropped rather than zeroed when its number is not available,
    so an empty database gets a short line instead of a boastful one about
    nothing.
    """
    parts: list[str] = []
    if stats.get("facts"):
        parts.append(f"{_compact(stats['facts'])} facts from SEC filings")
    if stats.get("companies"):
        parts.append(
            f"{fmt_int(stats['companies'])} "
            f"{plural(stats['companies'], 'company', 'companies')}"
        )
    ident = stats.get("identity") or {}
    if ident.get("pass_rate_pct") is not None:
        parts.append(f"{ident['pass_rate_pct']}% reconcile")
    if not parts:
        return '<p class="summary"><a href="#how">How this works</a></p>'
    return (
        f'<p class="summary">{escape(" · ".join(parts))}'
        f' <a href="#how">How this works</a></p>'
    )


# Fallback description. Used only where a page passes none, and every page
# that matters passes one -- a single sentence repeated across eight URLs is
# one page to a search engine and eight near-duplicates to a crawler.
DEFAULT_DESCRIPTION = (
    "Filed SEC balance sheets, drawn at true proportion. Reconciled with the "
    "accounting identity so the figures agree with the filing."
)
SITE_ORIGIN = "https://toscale.pro"


def _head_meta(
    title: str, description: str, canonical: str, noindex: bool = False
) -> str:
    """Description, canonical and the social cards, in one place.

    These were missing site-wide: one hardcoded description shared by every
    shell-rendered page, no canonical anywhere, and no Open Graph or Twitter
    card at all -- so a link shared into Slack, iMessage or a group chat
    rendered as a bare URL with no card.

    `og:title` deliberately drops the " - To Scale" suffix that the <title>
    carries: the card shows the site name on its own line already, and a card
    reading "To Scale - To Scale" is the kind of detail that makes a product
    look unfinished at exactly the moment somebody is deciding whether to
    click it.
    """
    desc = description or DEFAULT_DESCRIPTION
    url = canonical if canonical.startswith("http") else f"{SITE_ORIGIN}{canonical}"
    social = title.split(" — ")[0] if " — " in title else title
    tags = [
        # `noindex,follow`, never a robots.txt Disallow: a blocked crawl never
        # SEES the noindex, so anything already indexed stays indexed forever.
        # Follow is kept so the links out of the page still carry weight.
        '<meta name="robots" content="noindex,follow">' if noindex else "",
        f'<meta name="description" content="{escape(desc)}">',
        f'<link rel="canonical" href="{escape(url)}">' if canonical else "",
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="To Scale">',
        f'<meta property="og:title" content="{escape(social)}">',
        f'<meta property="og:description" content="{escape(desc)}">',
        f'<meta property="og:url" content="{escape(url)}">' if canonical else "",
        f'<meta property="og:image" content="{SITE_ORIGIN}/static/media/backdrop-1200.webp">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{escape(social)}">',
        f'<meta name="twitter:description" content="{escape(desc)}">',
        f'<meta name="twitter:image" content="{SITE_ORIGIN}/static/media/backdrop-1200.webp">',
    ]
    return "\n".join(t for t in tags if t)


def shell(
    title: str,
    body: str,
    film: str = "hero",
    *,
    description: str = "",
    canonical: str = "",
    ld: str = "",
    noindex: bool = False,
) -> str:
    return _shell(title, body, film, description=description,
                  canonical=canonical, ld=ld, noindex=noindex)


def _shell(
    title: str,
    body: str,
    film: str = "hero",
    *,
    description: str = "",
    canonical: str = "",
    ld: str = "",
    noindex: bool = False,
) -> str:
    """`film` says how much of the backdrop this page may spend.

    "hero"  the full backdrop: light veil, and the film on desktop
    "still" the still only, veiled hard
    "calm"  as "still"
    "none"  no backdrop at all

    **The site now uses "hero" everywhere**, which is why it is the default and
    why nothing passes anything else. It used to be decided per page on the
    theory that a page glanced at can afford a sunset and a page worked in for
    ten minutes cannot -- and the theory is sound, but the RESULT was that the
    home page showed a sunset and every other page was nearly black, so the
    site read as two different products depending which link you followed. One
    background everywhere is worth more than a per-page optimisation nobody
    asked for.

    The other values are kept, working, and deliberately unused: the whole
    treatment goes back to per-page by passing one of them again, and "none"
    is still the escape hatch for a page that must carry no backdrop at all.
    """
    from src.report.backdrop import render_backdrop

    backdrop = "" if film == "none" else render_backdrop()
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{_head_meta(title, description, canonical, noindex)}{ld}
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/company.css?v={asset_version()}">
<!-- Second sheet rather than more of the first: the accuracy banner and the
     dashboard's controls are the only things that use it, and keeping them out
     of company.css keeps the drawing's stylesheet about the drawing. -->
<link rel="stylesheet" href="/static/dashboard.css?v={asset_version()}">
<!-- Last, and only token overrides: this is what turns the whole site dark,
     drawings included, without a rule being rewritten. -->
<link rel="stylesheet" href="/static/backdrop.css?v={asset_version()}">
<link rel="stylesheet" href="/static/dark.css?v={asset_version()}">
<!-- Last of all, and token overrides again: this is what turns the flat
     surfaces into glass and the corners soft. Swap this one line back to
     terminal.css to return to the ruled treatment; nothing else changes. -->
<link rel="stylesheet" href="/static/glass.css?v={asset_version()}">
<meta name="theme-color" content="#0a0a0a">
</head>
<body data-film="{film}">
{backdrop}
<a class="skip" href="#main">Skip to content</a>
{body}
</body>
</html>"""


def search_form(value: str = "", autofocus: bool = False) -> str:
    """A plain GET form. No JavaScript: the page's one interaction must not
    depend on a script arriving.

    Autofocus is reserved for the pages a reader reaches BY searching -- an
    empty query, or a ticker with nothing behind it. On the home page it would
    throw up the keyboard over the five drawings that are the reason to stay.
    """
    af = " autofocus" if autofocus else ""
    return f"""
  <form class="search" action="/search" method="get" role="search">
    <label class="slabel" for="q">Ticker or company name</label>
    <div class="sfield">
      <input id="q" name="q" type="text" value="{escape(value)}"
        placeholder="JPM or Walmart" autocomplete="off" autocapitalize="none"
        spellcheck="false" maxlength="64" enterkeyhint="go"{af}>
      <button type="submit">Draw it</button>
    </div>
  </form>"""


def _example_figure() -> str:
    """A schematic of the drawing, for someone who has never read one.

    Deliberately NOT a real company. The teaching point is the shape -- two
    columns, one height, the same money counted twice -- and round numbers make
    it in one glance where a real filing's would not. It is labelled as an
    example on the page, because an unlabelled illustration sitting among real
    figures is the kind of thing this project exists to avoid.
    """
    owns = (("Cash", 20, "--a1"), ("Stock", 25, "--a3"), ("Buildings", 55, "--a5"))
    owed = (("Debt", 60, "--l1"), ("Owners", 40, "--yellow"))
    px = 150.0

    def col(rows):
        return "".join(
            f'<i class="exband" style="height:{p / 100 * px:.0f}px;'
            f'background:var({v})"><b>{escape(n)}</b> {p}%</i>'
            for n, p, v in rows
        )

    return f"""
  <section class="sec">
    <div class="sec-head"><h2>What you're looking at</h2></div>
    <p class="sec-sub">Both columns are the same height because they are the
      same money, counted twice. The left column is what the company owns,
      sorted by what it is. The right column is who has a claim on it — lenders
      and suppliers first, then whatever is left over for the owners. Every
      band is drawn at the size the company reported, so a bar twice as tall is
      twice the money.</p>
    <div class="example">
      <div class="excol">
        <span class="excap">Owns</span>
        <span class="exstack">{col(owns)}</span>
      </div>
      <div class="excol">
        <span class="excap">Owed &amp; owned</span>
        <span class="exstack">{col(owed)}</span>
      </div>
    </div>
    <p class="exnote">An example with round numbers, not a real company.</p>
  </section>"""


def _numbers(stats: dict) -> str:
    """The scale of the data, counted live so the page cannot go stale.

    Any figure that cannot be counted right now is omitted rather than
    estimated -- the same rule the drawings follow.
    """
    if not stats or not stats.get("facts"):
        return ""

    rows = [
        (fmt_int(stats["facts"]),
         plural(stats["facts"], "as-reported fact", "as-reported facts"),
         "from SEC quarterly Financial Statement Data Sets"),
        (fmt_int(stats["companies"]),
         plural(stats["companies"], "company", "companies"),
         f"{fmt_int(stats['drawable'])} with enough detail to draw"),
    ]
    # Filing dates, not a count of period ends. Seven quarterly downloads
    # contain ninety-odd distinct period ends, because filers close their books
    # on different days -- so counting those and calling them quarters
    # overstates the load by an order of magnitude.
    if stats.get("earliest_filing") and stats.get("latest_filing"):
        span = (
            "every figure carries the date it became public"
            if stats["earliest_filing"] == stats["latest_filing"]
            else f"earliest in the load: {stats['earliest_filing']} — every "
                 f"figure carries the date it became public"
        )
        rows.append((stats["latest_filing"], "most recent filing", span))

    cells = "".join(
        f'<div class="stat"><b>{escape(big)}</b>'
        f'<span class="statk">{escape(label)}</span>'
        f'<span class="statn">{escape(note)}</span></div>'
        for big, label, note in rows
    )

    ident = stats.get("identity") or {}
    headline = ""
    if ident.get("pass_rate_pct") is not None:
        headline = f"""
    <div class="bignum">
      <b>{ident['pass_rate_pct']}%</b>
      <span>of the {fmt_int(ident['checkable'])}
        {plural(ident['checkable'], 'company', 'companies')} with a complete
        balance sheet satisfy assets = liabilities + equity to within 1%</span>
    </div>"""

    return f"""
  <section class="sec">
    <div class="sec-head"><h2>The numbers behind it</h2></div>
    <div class="stats">{cells}</div>
    {headline}
  </section>"""


_HARD = (
    (
        "Filings don't say things once",
        "SEC's data carries the same figure many times per company per quarter "
        "— broken out by segment, by geography, by legal entity, by fair-value "
        "level. JPMorgan reports “total assets” twenty-three separate "
        "times in one filing. Exactly one of those is the company. Take the "
        "wrong one and you get $641 billion instead of $4.4 trillion, and "
        "nothing about it looks wrong.",
    ),
    (
        "Balance sheet items and income items are different kinds of fact",
        "One is a photograph, the other is a film. A balance sheet figure is "
        "an instant — what was there on one day. Revenue is a duration — what "
        "happened over three months. Read a duration where you needed an "
        "instant and you get the change in assets rather than assets, which is "
        "how a company ends up with a negative total.",
    ),
    (
        "Every industry files differently",
        "A bank doesn't report inventory; it reports loans and deposits. Look "
        "for the retail tags on a bank and you get a grey rectangle. The tag "
        "names move too — the short names most people use are deprecated, and "
        "the modern bank tags carry an “ExcludingAccruedInterest” "
        "suffix from a 2020 accounting standard.",
    ),
)


def _hard() -> str:
    blocks = "".join(
        f'<div class="hardrow"><h3>{escape(t)}</h3><p>{escape(b)}</p></div>'
        for t, b in _HARD
    )
    return f"""
  <section class="sec">
    <div class="sec-head"><h2>Why it's harder than it looks</h2></div>
    <div class="hard">{blocks}</div>
    <p class="method">Every tag was confirmed against the raw filing data
      before being used. Nothing was mapped on the strength of it sounding
      right.</p>
  </section>"""


def _limits() -> str:
    items = (
        "No predictions.",
        "No scores.",
        "No recommendations.",
        "Nothing estimated — where a company doesn't report something, the "
        "page says so rather than showing zero.",
        "Every figure traces to a filing, with the date it was filed.",
    )
    lis = "".join(f"<li>{escape(i)}</li>" for i in items)
    return f"""
  <section class="sec">
    <div class="sec-head"><h2>What this doesn't do</h2></div>
    <ul class="limits">{lis}</ul>
  </section>"""


# The hero drawing is full size, not a thumbnail. The brief for this page is
# "real output above the fold", and 300px is the column the company page uses
# -- so what a visitor sees first is literally the product, at the size the
# product is served at.
_HERO_COLUMN_PX = 300.0


def _pipeline_age() -> str:
    """How long ago the loader last succeeded, as a person would say it.

    Read from the updater's own state rather than from a build-time constant,
    so it cannot claim freshness the data does not have. Any failure to read it
    returns "", and the cell is simply not rendered -- an unknown age must not
    be shown as a confident one.
    """
    import datetime as dt

    try:
        from src import scheduler

        newest = None
        for job in scheduler.report().get("jobs", []):
            when = job.get("last_success_at")
            if not when:
                continue
            stamp = dt.datetime.fromisoformat(when)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.UTC)
            if newest is None or stamp > newest:
                newest = stamp
        if newest is None:
            return ""
        mins = (dt.datetime.now(dt.UTC) - newest).total_seconds() / 60.0
        if mins < 2:
            return "just now"
        if mins < 90:
            return f"{int(mins)}m ago"
        hours = mins / 60.0
        if hours < 36:
            return f"{int(hours)}h ago"
        return f"{int(hours / 24)}d ago"
    except Exception as exc:  # noqa: BLE001 - the home page must still render
        log.warning("pipeline_age_failed", error=str(exc)[:200])
        return ""


def _status_strip(stats: dict, freshness: str = "") -> str:
    """The line across the top: what is loaded, how fresh, and how well it adds up.

    Cells with rules between them rather than one sentence with middle dots in
    it. Every figure is counted live, and any figure that cannot be counted is
    dropped -- a strip reporting "0" for something it merely failed to read
    would be worse than a shorter strip.

    `data-countup` marks the figures the single animation on this site touches.
    """
    cells: list[str] = []

    def cell(key: str, value: str, live: bool = False, count: str = "") -> None:
        attr = f' data-countup="{count}"' if count else ""
        cls = "sv live" if live else "sv"
        cells.append(
            f'<div class="scell"><span class="sk">{escape(key)}</span>'
            f'<span class="{cls}"{attr}>{value}</span></div>'
        )

    cell("Source", "SEC EDGAR")
    if stats.get("facts"):
        cell("Facts", escape(_compact(stats["facts"])), count=str(stats["facts"]))
    if stats.get("companies"):
        cell("Companies", escape(fmt_int(stats["companies"])),
             count=str(stats["companies"]))
    if stats.get("latest_filing"):
        cell("Latest filing", escape(str(stats["latest_filing"])))
    ident = stats.get("identity") or {}
    if ident.get("pass_rate_pct") is not None:
        # The number this whole product is an argument about, and so the one
        # thing on the page that is allowed the accent colour.
        cell("Reconciles", f"{ident['pass_rate_pct']}%", live=True)
    if freshness:
        cells.append(
            '<div class="scell"><span class="sk">Pipeline</span>'
            '<span class="sv"><i class="pulse" aria-hidden="true"></i>'
            f"{escape(freshness)}</span></div>"
        )
    return f'<div class="strip">{"".join(cells)}</div>'


def _hero_drawing(view: View1 | None, kind: str = "") -> str:
    """A real company's real balance sheet, drawn and tabulated, above the fold.

    There is no placeholder when there is nothing to draw: the page falls back
    to the strip and the search box, which is a smaller page rather than a page
    showing a picture of nothing and calling it an example.
    """
    if view is None:
        return ""

    d = view.as_dict()
    total = d["total_assets"]
    px = _HERO_COLUMN_PX / 100.0

    assets_html = "".join(_band(b, px) for b in d["assets"])
    above = [b for b in d["claims"]
             if not (b["kind"] == "equity" and b["value"] < 0)]
    below = [b for b in d["claims"] if b["kind"] == "equity" and b["value"] < 0]
    claims_html = "".join(_band(b, px) for b in above)
    below_html = "".join(_band(b, px) for b in below)

    liab = sum(b["value"] for b in d["claims"] if b["kind"] != "equity")
    eq = sum(b["value"] for b in d["claims"] if b["kind"] == "equity")
    name = escape(d["company_name"] or d["ticker"])
    ticker = escape(d["ticker"])
    # The card this hero replaces said in one word why the company was on the
    # list. Dropping that would make the choice look arbitrary.
    why = f'<span class="ckind">{escape(kind)}</span>' if kind else ""
    below_block = (
        f'<div class="baseline"></div><div class="stack">{below_html}</div>'
        if below_html else ""
    )

    return f"""
  <section class="sec hero-bs">
    <div class="sec-head">
      <h2>Live from the filings</h2>
      <span class="bs-meta">{ticker} {why} quarter ended {escape(d["period_end"])},
        filed {escape(d["filing_date"])}</span>
    </div>
    <div class="bs">
      <div class="bs-cols">
        <div class="bs-cap"><span>{name} owns</span><b>{money(total)}</b></div>
        <div class="bs-cap"><span>Owed &amp; owned</span>
          <b>{money(liab)} + {money(eq)}</b></div>
        <div class="bs-col" role="img" aria-label="What {name} owns, drawn to scale &mdash; total assets {money(total)}. Line-by-line amounts in the table below.">
          <div class="stack">{assets_html}</div>
        </div>
        <div class="bs-col" role="img" aria-label="Who has a claim on it, drawn to scale &mdash; liabilities {money(liab)}, equity {money(eq)}. Line-by-line amounts in the table below.">
          <div class="stack">{claims_html}</div>
          {below_block}
        </div>
      </div>
      <table class="legend"><caption class="vh">Assets, line by line</caption>
        <tbody>{_legend_rows(d["assets"], total)}</tbody></table>
      <table class="legend"><caption class="vh">Liabilities and equity, line by line</caption>
        <tbody>{_legend_rows(d["claims"], total)}</tbody></table>
    </div>
    <p class="plan-note">Both columns are the same height because they are the
      same money, counted twice: once by what it is, once by who it belongs to.
      <a href="/company/{ticker}">Open the full page for {ticker}</a>.</p>
  </section>"""


def _trust(stats: dict) -> str:
    """Where the data comes from, how fresh it is, and how it is derived.

    Pulled into one section near the top rather than left implied across four
    sections further down. For a technical reader deciding whether to trust a
    financial dataset this is the section that does the work -- so it states
    the unflattering parts too, because a methodology note that lists only
    strengths is marketing wearing a lab coat.
    """
    ident = stats.get("identity") or {}
    rate = ident.get("pass_rate_pct")
    checkable = ident.get("checkable")

    identity_line = (
        f"{rate}% of the {fmt_int(checkable)} companies with a complete balance "
        "sheet satisfy assets = liabilities + equity to within 1%."
        if rate is not None and checkable
        else "Every drawing is checked against assets = liabilities + equity."
    )
    span = (
        f"Most recent filing in the load: {escape(str(stats['latest_filing']))}."
        if stats.get("latest_filing") else ""
    )

    return f"""
  <section class="sec" id="trust">
    <div class="sec-head"><h2>Data &amp; method</h2></div>
    <div class="trust">
      <div class="tblock">
        <h3>Source</h3>
        <p>SEC's own
          <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"
             rel="noopener">Financial Statement Data Sets</a>, plus the XBRL
          frames API for anything filed since the last quarterly dataset
          published. Nothing is scraped, bought, or estimated.</p>
      </div>
      <div class="tblock">
        <h3>Freshness</h3>
        <p>The loader decides it is behind by asking the database, not by
          watching a clock, so a container that was down for a week closes the
          gap on its next tick instead of waiting for a schedule. {span}</p>
      </div>
      <div class="tblock">
        <h3>As reported, never restated</h3>
        <p>Where a figure has been filed more than once, the earliest filing
          wins. You see what the company said at the time, not what it said
          later about the same quarter.</p>
      </div>
      <div class="tblock">
        <h3>The check</h3>
        <p>{identity_line} The rest are drawn with what is missing named,
          rather than quietly balanced.</p>
      </div>
      <div class="tblock">
        <h3>One row, not twenty-three</h3>
        <p>SEC's data carries the same figure many times per filing &mdash; by
          segment, by geography, by legal entity. Exactly one of them is the
          consolidated company, and isolating it is most of what this does.</p>
      </div>
      <div class="tblock">
        <h3>What it doesn't do</h3>
        <p>It draws what was filed and says so when it cannot. Nothing here
          is an opinion about what a company is worth, or about what it is
          going to do next.</p>
      </div>
    </div>
  </section>"""


def render_home(
    pairs: list[tuple[Suggestion, View1 | None]],
    stats: dict | None = None,
    *,
    nav: str = "",
) -> str:
    """`pairs` is (suggestion, its view or None), in the order to show them."""
    from src.company.stats import component_coverage
    from src.report.nav import render_disclaimer, render_footer

    # The page keeps what it always said; the shared part is added around it.
    footer = render_footer(
        'Source: <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"'
        ' rel="noopener">SEC Financial Statement Data Sets</a>. Built by '
        'Dominique Church.'
        # `nofollow`, and on the home page ONLY. It used to sit on every
        # company page too -- 6,167 crawlable links to a URL robots.txt
        # disallows, from the page type search traffic actually lands on. It
        # is the operator's way in, so one link from the front door is the
        # whole requirement.
        '<span class="foot-admin">'
        '<a href="/admin" rel="nofollow">Admin</a></span>'
    )
    disclaimer = render_disclaimer()

    # The first company that actually has something to draw becomes the hero.
    # Not simply pairs[0]: that one may have failed to build, and the fold is
    # the last place to show a gap.
    hero_pair = next(((sg, v) for sg, v in pairs if v is not None), None)
    hero_view = hero_pair[1] if hero_pair else None
    hero_bs = _hero_drawing(hero_view, hero_pair[0].kind if hero_pair else "")
    freshness = _pipeline_age()

    facts = headline_facts([v for _s, v in pairs], component_coverage())
    # The gallery is the OTHER companies -- showing the hero's own drawing
    # again a screen below itself reads as a bug rather than as a set. Unless
    # it is the only one there is, in which case dropping it would leave an
    # empty section and no way to reach the one page that exists.
    #
    # `skip` is compared by identity, so it must never be None: a card whose
    # view failed to build is also None, and `v is not skip` would then drop
    # every undrawable company from the gallery -- which is exactly the set
    # that most needs to stay offered, since a bare card is the only way to
    # reach a page that can say what is missing.
    drawable = sum(1 for _s, v in pairs if v is not None)
    skip = hero_view if (drawable > 1 and hero_view is not None) else object()
    cards = "".join(
        _card(s, v, fact)
        for (s, v), fact in zip(pairs, facts, strict=True)
        if v is not skip
    )

    if pairs:
        gallery = f"""
  <section class="sec">
    <div class="sec-head"><h2>Same scale rules, other companies</h2></div>
    <p class="sec-sub">Each drawing is that company's own balance sheet at its
      own proportions. They look nothing alike because they are nothing alike.</p>
    <div class="cards">{cards}</div>
  </section>"""
    else:
        # Empty is a state to explain, not a blank page to leave.
        gallery = """
  <section class="sec">
    <div class="empty">
      <h2>No filed statements loaded yet</h2>
      <p>Nothing can be drawn until the fundamentals table has data in it.
        Load it from the admin page, then search for any ticker.</p>
      <div class="sugg"><a href="/admin">Open admin</a></div>
    </div>
  </section>"""

    # One page, in the order someone actually uses it: search, then the five
    # shapes, then -- only for whoever is still reading -- the explanation.
    body = f"""
<!-- No nav link to the explanation. It is further down THIS page, and a nav
     item reads as a separate destination -- which is exactly the confusion
     that made it a separate page in the first place. The jump under the stat
     line is enough; the rest is scrolling. -->
{nav}

<main class="wrap" id="main">
  <!-- Above the fold, in this order: what is loaded, what you can ask it, and
       one real company's real balance sheet. No marketing headline standing
       between the reader and the output -- the output IS the argument, and a
       sentence claiming the drawings are accurate is weaker than a drawing. -->
  {_status_strip(stats or {}, freshness)}
  <header class="hero">
    <h1 class="htitle">Every balance sheet, drawn to scale</h1>
    <p class="hlede">Filed figures from SEC EDGAR, at true proportion. Search
      any US public company by ticker or by name.</p>
    {search_form(autofocus=True)}
    <p class="summary"><a href="#how">How this works</a></p>
  </header>
  {hero_bs}
{disclaimer}
  {_trust(stats or {})}
  {_accuracy_banner()}
  {gallery}
  {_pricing(stats or {})}
  {_demo_section()}

  <div class="fold" id="how"></div>
  {_example_figure()}
  {_numbers(stats or {})}
  {_hard()}
  {_limits()}

{footer}
</main>

<!-- In the body, not the shell: the shell is shared with /dashboard and the
     search pages, and none of those have a demo box to drive. -->
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/home.js?v={asset_version()}" defer></script>
<script src="/static/countup.js?v={asset_version()}" defer></script>"""
    # Read live rather than written down. This line said "1.7M data points"
    # while /dataset -- which counts the same table -- said "1.8M rows".
    from src.dataset import facts_label
    from src.report.schema import organization_ld, software_ld

    facts = facts_label()
    scale = f" {companies_label()} companies, {facts} data points." if facts else ""

    return _shell(
        "To Scale — 99.9% Accurate SEC Balance Sheet API",
        body,
        description=(
            "99.9% accurate reconciled balance sheet data from SEC EDGAR "
            "filings. Most providers pick the wrong XBRL tag for Total Assets "
            "— JPMorgan reports it 23 ways. We use A = L + E to select the "
            f"right one.{scale}"
        ),
        canonical="/",
        ld=organization_ld() + software_ld(),
    )


def render_matches(
    query: str, matches, suggestions: list[Suggestion], did_you_mean: bool = False
) -> str:
    """Several companies matched the name -- or none did, and these are close.

    The two states share a layout and differ in what they claim. "3 companies
    match" is a statement about the data; "did you mean" is a question, and
    a near-miss must never be dressed as a match.
    """
    rows = "".join(
        f'<a class="mrow" href="/company/{escape(m.ticker)}">'
        f'<span class="mtick">{escape(m.ticker)}</span>'
        f'<span class="mname">{escape(m.name or "")}</span>'
        + (f'<span class="msec">{escape(m.sector)}</span>' if m.sector else "")
        + "</a>"
        for m in matches
    )
    sugg = "".join(
        f'<a href="/company/{escape(s.ticker)}">{escape(s.ticker)}'
        + (f" <small>{escape(s.kind)}</small>" if s.kind else "")
        + "</a>"
        for s in suggestions
    )
    body = f"""
<nav><div class="wrap nav">
  <a class="back" href="/"><span aria-hidden="true">←</span> Search</a>
  <span class="spacer"></span>
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
</div></nav>
<main class="wrap" id="main">
  <div class="empty">
    <h1>{
      f'Nothing matches “{escape(query)}”'
      if did_you_mean
      else f'{len(matches)} companies match “{escape(query)}”'
    }</h1>
    <p>{
      "Did you mean one of these?" if did_you_mean
      else "Pick one, or narrow the search."
    }</p>
    {search_form(query)}
    <div class="matches">{rows}</div>
  </div>
  <p class="tryline">Or start with one of these:</p>
  <div class="sugg">{sugg}</div>
</main>"""
    return _shell(
        f"{query} — To Scale",
        body,
        # A query string makes an unbounded set of thin, near-duplicate
        # pages under one route. `noindex,follow` rather than a
        # robots.txt Disallow: a blocked crawl never sees the noindex.
        noindex=True,
    )


def render_no_names(query: str, suggestions: list[Suggestion]) -> str:
    """Name search asked for, but no names are stored.

    Said plainly rather than as "no match": the two have completely different
    causes, and only one of them is the reader's problem.
    """
    sugg = "".join(
        f'<a href="/company/{escape(s.ticker)}">{escape(s.ticker)}'
        + (f" <small>{escape(s.kind)}</small>" if s.kind else "")
        + "</a>"
        for s in suggestions
    )
    body = f"""
<nav><div class="wrap nav">
  <a class="back" href="/"><span aria-hidden="true">←</span> Search</a>
  <span class="spacer"></span>
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
</div></nav>
<main class="wrap" id="main">
  <div class="empty">
    <h1>Search by ticker for now</h1>
    <p>Company names are not loaded on this instance, so “{escape(query)}”
      can only be read as a ticker symbol — and there is no company with that
      symbol.</p>
    {search_form()}
    <p class="tryline">These five are loaded:</p>
    <div class="sugg">{sugg}</div>
  </div>
</main>"""
    return _shell("Search by ticker — To Scale", body, noindex=True)


def render_search_empty(suggestions: list[Suggestion]) -> str:
    """Someone submitted the search with nothing in it."""
    sugg = "".join(
        f'<a href="/company/{escape(s.ticker)}">{escape(s.ticker)}'
        + (f" <small>{escape(s.kind)}</small>" if s.kind else "")
        + "</a>"
        for s in suggestions
    )
    body = f"""
<nav><div class="wrap nav">
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
</div></nav>
<main class="wrap" id="main">
  <div class="empty">
    <h1>Type a ticker</h1>
    <p>The search takes one ticker symbol and draws that company's most
      recent filed balance sheet.</p>
    {search_form(autofocus=True)}
    <p class="tryline">Or start with one of these:</p>
    <div class="sugg">{sugg}</div>
  </div>
</main>"""
    return _shell("To Scale — search", body, noindex=True)
