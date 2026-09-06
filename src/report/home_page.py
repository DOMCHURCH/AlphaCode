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
from src.report.company_page import asset_version, block_colour, money

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
      consolidated row. <a href="/dashboard">Get the data</a>.</p>
  </div>"""


def _pricing(stats: dict) -> str:
    """Three cards: free, the dataset, Pro.

    Every number is read from settings and from the live fact count rather than
    written into the copy. A price quoted in HTML is a price that disagrees with
    the one the API enforces the first time either changes, and on this page the
    disagreement would be with the figure a buyer is about to act on.
    """
    from src.config.settings import get_settings

    s = get_settings()
    rows = _compact(stats.get("facts") or 0) if stats.get("facts") else None
    dataset_line = (
        f"Download all {rows} rows as CSV" if rows else "Download the whole table as CSV"
    )

    def card(name: str, price: str, per: str, line: str, cta: str, feature: bool) -> str:
        return f"""
    <div class="plan{' feature' if feature else ''}">
      <span class="plan-name">{escape(name)}</span>
      <p class="plan-price">{escape(price)}<small>{escape(per)}</small></p>
      <p class="plan-line">{escape(line)}</p>
      <a class="plan-cta" href="/dashboard">{escape(cta)}</a>
    </div>"""

    return f"""
  <section class="sec" id="pricing">
    <div class="sec-head"><h2>Pricing</h2></div>
    <p class="sec-sub">The drawings are free and always will be. The machine-readable
      version is what costs money.</p>
    <div class="plans">
      {card("Free", "$0", "", f"{s.free_tier_monthly_calls} API calls per month", "Get a key", False)}
      {card("Full dataset", f"${s.dataset_price_usd}", " once", dataset_line, "Buy the data", True)}
      {card("Pro", f"${s.pro_price_usd}", "/month", f"{_compact(s.pro_tier_monthly_calls)} API calls per month", "Go Pro", False)}
    </div>
    <p class="plan-note">No card is taken on this site. Payment is by e-transfer or
      PayPal and access is unlocked by hand — usually within 24 hours.</p>
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
    return f"""
  <section class="sec" id="demo">
    <div class="sec-head"><h2>Live demo</h2></div>
    <p class="sec-sub">The real endpoint, the real data, no key needed.
      {limit} {plural(limit, "company", "companies")} a day from one address.</p>
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


def shell(title: str, body: str) -> str:
    return _shell(title, body)


def _shell(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<meta name="description" content="Filed financial statements, drawn at true proportion.">
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/company.css?v={asset_version()}">
<!-- Second sheet rather than more of the first: the accuracy banner and the
     dashboard's controls are the only things that use it, and keeping them out
     of company.css keeps the drawing's stylesheet about the drawing. -->
<link rel="stylesheet" href="/static/dashboard.css?v={asset_version()}">
</head>
<body>
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


def render_home(
    pairs: list[tuple[Suggestion, View1 | None]],
    stats: dict | None = None,
    *,
    nav: str = "",
) -> str:
    """`pairs` is (suggestion, its view or None), in the order to show them."""
    from src.company.stats import component_coverage

    facts = headline_facts([v for _s, v in pairs], component_coverage())
    cards = "".join(
        _card(s, v, fact) for (s, v), fact in zip(pairs, facts, strict=True)
    )

    if pairs:
        gallery = f"""
  <section class="sec">
    <div class="sec-head"><h2>Five companies, same scale rules</h2></div>
    <p class="sec-sub">Each drawing is that company's own balance sheet at its
      own proportions. They look nothing alike because they are nothing alike.</p>
    <div class="cards">{cards}</div>
  </section>"""
    else:
        # Empty is a state to explain, not a blank page to leave.
        gallery = """
  <section class="sec">
    <div class="empty">
      <h1>No filed statements loaded yet</h1>
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
  <header class="hero">
    <h1 class="htitle">To Scale</h1>
    <p class="hlede">Every US public company's balance sheet, drawn at true
      proportion, from what they filed with the SEC.</p>
    {search_form()}
  </header>
  {_accuracy_banner()}
  {_pricing(stats or {})}
  {_summary_line(stats or {})}
  {gallery}
  {_demo_section()}

  <div class="fold" id="how"></div>
  {_example_figure()}
  {_numbers(stats or {})}
  {_hard()}
  {_limits()}

  <footer>
    Source: <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"
      rel="noopener">SEC Financial Statement Data Sets</a>. Built by Dominique
    Church — <a href="https://github.com/domchurch/alphacode"
      rel="noopener">source on GitHub</a>.
    <span class="foot-admin"><a href="/admin">Admin</a></span>
  </footer>
</main>

<!-- In the body, not the shell: the shell is shared with /dashboard and the
     search pages, and none of those have a demo box to drive. -->
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/home.js?v={asset_version()}" defer></script>"""
    return _shell("To Scale — filed financial statements, drawn to scale", body)


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
    return _shell(f"{query} — To Scale", body)


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
    return _shell("Search by ticker — To Scale", body)


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
    return _shell("To Scale — search", body)
