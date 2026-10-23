"""The front door: a free API key, a real request and response, and the proof.

The page leads with what a developer came to do -- get a key and see a real
call -- and keeps the evidence that follows: the stat strip, one real filing
drawn to scale, a live demo, three short steps, the plans, and the sign-up
again. Every figure and every code sample is produced by the same calls the
API and the MCP server make, so none of it describes an answer the product
does not give.

The drawing still comes from the same `build_view1` the full company page
uses, so it can never show a shape the page then contradicts. A ticker whose
drawing cannot be built is offered without one rather than with a
placeholder.
"""

from __future__ import annotations

import threading
import time
from html import escape

import structlog

from src.company.suggest import Suggestion
from src.company.view1 import View1
from src.report.backdrop import media_version
from src.report.cssbundle import stylesheet_link
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


# The sentences an answer engine should be able to lift whole.
#
# Google's AI Overview was answering "what is balanceproof.dev" by assembling a
# description out of three company pages -- /company/IDT, /company/LLPS,
# /company/OBIO -- because the home page led with a headline and a lede written
# to persuade a reader, and neither one is a definition. This is the
# definition: name, category, method, then what happens when the method fails.
# It is deliberately flat prose. Nothing here is a claim about quality, so
# there is nothing in it that goes stale the way "99.9% accurate" did.
#
# The category was "a financial data API", which is the shelf FMP, Finnworlds
# and fin2dev already own with more statements, more history and a larger free
# tier. Naming it was conceding the comparison. The category is the
# verification, so that is what the first sentence now says, and it agrees
# with the H1 and the lede rather than arguing with them.
#
# Shared with /about, which needs to say the same thing in the same words --
# two pages that define the product differently give an engine a reason to
# trust neither.
# The METHOD only. The naming clause that used to open this string is
# `schema.PRODUCT_CLAUSE`, prepended by `product_definition()` for the pages
# that need the paragraph to stand alone, and left off by the home page, where
# the lede directly above it says the same words. Rendered on a phone the two
# paragraphs read as the same paragraph printed twice, because the first
# screenful of both was identical.
# The method, in three sentences, kept as parts so the hero can take a subset
# without a second copy of the words drifting away from this one.
_METHOD_IDENTITY = (
    "Each filing is checked against Assets = Liabilities + Equity when it "
    "reports the lines needed; where it does not, it is marked not testable "
    "instead of passing."
)
_METHOD_AS_FILED = (
    "Every figure is pulled as filed, and the check runs before it is stored."
)
_METHOD_FLAGGED = (
    # 2026-10-19: back to "instead of adjusting the numbers". The hero subhead
    # (owner's wording) makes this promise in those words, and the same page
    # carrying "never silently fudged" a few inches lower was one promise in
    # two phrasings, the second colloquial. /methodology still has its
    # "0 silently fudged" tile and meta line; aligning it is a follow-up.
    "When a filing does not balance, the page says so with the reason "
    "instead of adjusting the numbers."
)
_DEFINITION_METHOD = f"{_METHOD_IDENTITY} {_METHOD_AS_FILED} {_METHOD_FLAGGED}"

# What the HERO carries: the two sentences that make the argument. The
# as-filed line is true, useful and the least distinctive of the three -- it
# is what every vendor claims -- so it stays on /about and /methodology where
# there is room, and the hero keeps the definition and the promise.
#
# The full paragraph used to sit in the hero and closed on the same four
# figures the stat strip shows a few inches above it. Those six lines were
# most of the reason the strip, the search box and the button could not share
# one screen; the counts are the strip's job and this is the method's.
_HERO_METHOD = f"{_METHOD_IDENTITY} {_METHOD_FLAGGED}"


def product_definition(with_name: bool = True) -> str:
    """The definition, with the four counts read live rather than typed in.

    `with_name` prepends `schema.PRODUCT_CLAUSE`, so the paragraph names the
    product it is defining. /about needs that: the definition is the whole of
    its lede and a paragraph that opens on the method defines
    nothing. The home page passes False, because the lede immediately above
    opens on the very same clause and printing it twice is what made the two
    paragraphs read as one paragraph rendered twice on a phone.

    The counts are the same four `identity_breakdown()` gives /methodology,
    formatted into the same four sentences. Written down instead, they would be
    the third generation of the bug this module already documents twice: a
    literal `6201` sat in `dataset_ld` as an argument, and `companies_label`
    exists because "6,201 companies" had been typed into six files by hand.
    A definition an engine is invited to quote is the worst place yet for a
    figure that moves every quarter.

    When the breakdown cannot be read -- fresh database, first boot -- the
    method sentences are returned alone. Four sentences reading "0 companies
    covered" would be a false definition, and no definition beats a wrong one.
    """
    try:
        from src.company.stats import identity_breakdown

        b = identity_breakdown()
    except Exception:  # noqa: BLE001 - copy must not take a page down
        b = None
    from src.report.schema import PRODUCT_CLAUSE

    method = f"{PRODUCT_CLAUSE}. {_DEFINITION_METHOD}" if with_name else (
        _DEFINITION_METHOD
    )
    if not b:
        return method
    companies = int(b["companies"] or 0)
    return (
        f"{method} "
        f"{fmt_int(companies)} "
        f"{plural(companies, 'company', 'companies')} covered. "
        f"{fmt_int(b['reconciled'])} reconcile directly. "
        f"{fmt_int(b['flagged'])} flagged with a reason. "
        f"{fmt_int(b['counts']['broken'])} hidden."
    )


def _definition_para() -> str:
    """The definition as the home page renders it: one paragraph, plus the one
    visible route to /about.

    Carries `hlede` as well as `hdef` so it inherits the lede's type in all
    four stylesheets rather than needing a new rule in each of them.
    """
    return (
        f'<p class="hlede hdef">{product_definition(with_name=False)} '
        '<a href="/about">Learn more about how BalanceProof works</a>.</p>'
    )


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
        # No DAILY ration, but this endpoint is the paid API and there is an
        # hourly per-address window in front of it, so "no limit" is not a
        # sentence this page can honestly print. No number quoted: the gate
        # lives in `api._demo_ip_gate` and a figure repeated here would be one
        # more thing to keep in step with it.
        else "Generous: plenty to evaluate with. A free key gives you more."
    )
    return f"""
  <section class="sec" id="demo">
    <div class="sec-head"><h2>Live demo</h2></div>
    <p class="sec-sub">The real endpoint, the real data, no key needed.
      {allowance}</p>
    <p class="sec-sub">Want the drawing instead of the JSON?
      <a href="/companies">Browse every company</a>.</p>
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
    bd = None
    try:
        from src.company.stats import identity_breakdown

        bd = identity_breakdown()
    except Exception:  # noqa: BLE001 - a count must never take the page
        bd = None
    if bd:
        parts.append(f"{fmt_int(bd['reconciled'])} reconciled")
        parts.append(f"{fmt_int(bd['flagged'])} flagged with a reason")
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
SITE_ORIGIN = "https://balanceproof.dev"


def _head_meta(
    title: str, description: str, canonical: str, noindex: bool = False
) -> str:
    """Description, canonical and the social cards, in one place.

    These were missing site-wide: one hardcoded description shared by every
    shell-rendered page, no canonical anywhere, and no Open Graph or Twitter
    card at all -- so a link shared into Slack, iMessage or a group chat
    rendered as a bare URL with no card.

    `og:title` deliberately drops the " - BalanceProof" suffix that the <title>
    carries: the card shows the site name on its own line already, and a card
    reading "BalanceProof - BalanceProof" is the kind of detail that makes a product
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
        '<meta property="og:site_name" content="BalanceProof">',
        f'<meta property="og:title" content="{escape(social)}">',
        f'<meta property="og:description" content="{escape(desc)}">',
        f'<meta property="og:url" content="{escape(url)}">' if canonical else "",
        f'<meta property="og:image" content="{SITE_ORIGIN}/static/media/backdrop-glass-2000.jpg?v={media_version()}">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{escape(social)}">',
        f'<meta name="twitter:description" content="{escape(desc)}">',
        f'<meta name="twitter:image" content="{SITE_ORIGIN}/static/media/backdrop-glass-2000.jpg?v={media_version()}">',
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
    backdrop: str = "",
) -> str:
    # Every page reads as the same site as the home page (2026-10-21): the
    # light paper theme, its type, no backdrop film. The home page passes its
    # own theme="light" (which adds the motion layer and the climb).
    return _shell(title, body, "none", description=description,
                  canonical=canonical, ld=ld, noindex=noindex,
                  backdrop=backdrop, theme="site")


def _shell(
    title: str,
    body: str,
    film: str = "hero",
    *,
    description: str = "",
    canonical: str = "",
    ld: str = "",
    noindex: bool = False,
    backdrop: str = "",
    theme: str = "",
) -> str:
    """`film` says how much of the backdrop this page may spend.

    `theme="light"` (the home page only, 2026-10-19) loads `home-light.css`
    after the bundle, sets `data-theme="light"` on <body> and uses the
    self-hosted type; pass it with `film="none"`.

    `backdrop` names a variant picture (see `backdrop.render_backdrop`); only
    the home page passes one.

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

    bd_attr = f' data-bd="{escape(backdrop)}"' if backdrop else ""
    light = theme in ("light", "site")
    if theme:
        bd_attr += ' data-theme="light"' + (' data-site' if theme == "site" else '')
    fonts = (_self_hosted_fonts() if backdrop == "fluted" or light
             else _GOOGLE_FONTS)
    from src.report.company_page import asset_version as _av

    # The motion switch, before first paint so nothing animates in after it was
    # already drawn: on by default, off only when the reader turned it off with
    # the footer toggle (the OS reduce-motion setting is not read; see home-motion.js).
    # No class, no animation: the page is complete without this line.
    motion_js = ("<script>(function(){try{var p=localStorage.getItem('bp-motion'),"
                 "r=matchMedia('(prefers-reduced-motion: reduce)').matches;"
                 "if(p!=='off')document.documentElement.classList.add('motion');"
                 "if(window.CSS&&CSS.supports('animation-timeline: view()'))document.documentElement.classList.add('sda')"
                 "}catch(e){}})();</script>")
    theme_css = (f'<link rel="stylesheet" href="/static/home-light.css?v={_av()}">{motion_js}'
                 f'{_climb_preload()}' if theme == "light" else
                 f'<link rel="stylesheet" href="/static/home-light.css?v={_av()}">' if theme == "site"
                 else "")
    backdrop = "" if film == "none" else render_backdrop(backdrop)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
{_head_meta(title, description, canonical, noindex)}{ld}
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
{fonts}
<!-- The five layered sheets, as one request. Their order, and the one-line
     revert to terminal.css, live in `cssbundle.LAYERS`. -->
{stylesheet_link()}{theme_css}
<meta name="theme-color" content="{'#ffffff' if light else '#0a0a0a'}">
</head>
<body data-film="{film}"{bd_attr}>
{backdrop}
<a class="skip" href="#main">Skip to content</a>
{body}
</body>
</html>"""


# ---------------------------------------------------------------------------
# The home page's type: self-hosted, three voices
# ---------------------------------------------------------------------------
# Zodiak (Fontshare) is the display serif: headlines and prices, set light, with
# one italic. Satoshi (Fontshare) is the quiet sans for everything read. Geist
# Mono (SIL OFL, via the Fontsource mirror) is the voice of data and labels.
# Three roles that are easy to tell apart at a glance is the whole idea: the
# old pairing was a geometric sans and a mono, and both lived at one weight.
#
# SELF-HOSTED, so the home page makes no request to Google or anyone else for
# type: nothing to be slow, blocked or logged, and the first paint does not
# wait on a third party. The files sit in static/media/ because
# `backdrop.media_version()` stamps that directory, and `/static` is served
# immutable for a year: an unversioned font URL would never update. Other
# pages still load Jost and Space Mono as before.
_FONT_FILES = (
    ("Zodiak", 300, "normal", "font-zodiak-300.woff2"),
    ("Zodiak", 400, "normal", "font-zodiak-400.woff2"),
    ("Zodiak", 400, "italic", "font-zodiak-400i.woff2"),
    ("Satoshi", 400, "normal", "font-satoshi-400.woff2"),
    ("Satoshi", 500, "normal", "font-satoshi-500.woff2"),
    ("Satoshi", 700, "normal", "font-satoshi-700.woff2"),
    ("Geist Mono", 400, "normal", "font-geistmono-400.woff2"),
    ("Geist Mono", 500, "normal", "font-geistmono-500.woff2"),
    ("Geist Mono", 700, "normal", "font-geistmono-700.woff2"),
)
# What the first screen is set in; the rest load when something uses them.
# Satoshi 700 is the headline weight on the bold home page; without the preload
# the heading reflowed by 4px when it arrived (a measured layout shift).
_FONT_PRELOAD = ("font-zodiak-300.woff2", "font-zodiak-400i.woff2", "font-satoshi-400.woff2", "font-satoshi-500.woff2",
                 "font-satoshi-700.woff2", "font-geistmono-400.woff2")


def _self_hosted_fonts() -> str:
    v = media_version()
    preload = "".join(
        f'<link rel="preload" href="/static/media/{f}?v={v}" as="font" '
        'type="font/woff2" crossorigin>\n'
        for f in _FONT_PRELOAD
    )
    faces = "".join(
        f'@font-face{{font-family:"{fam}";font-weight:{w};font-style:{st};'
        f'font-display:swap;src:url("/static/media/{f}?v={v}") format("woff2")}}'
        for fam, w, st, f in _FONT_FILES
    )
    return f"{preload}<style>{faces}</style>"


_GOOGLE_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800'
    '&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">'
)


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
      <button type="submit">Search</button>
    </div>
  </form>"""


# ---------------------------------------------------------------------------
# The API front door
# ---------------------------------------------------------------------------
# The page used to lead with a company search and a drawing, which is a good
# answer to "what is this" and a poor one to "can I build on it". It now leads
# with the thing a developer came to do -- get a key and see a real request
# and a real response -- and keeps the drawing, the strip and the demo as the
# evidence that follows. Every figure and every code sample on this page is
# produced by the same calls the API and the MCP server make, at render time,
# so none of it can describe an answer the product does not give.

# ---------------------------------------------------------------------------
# Memoised for fifteen minutes, like everything else the home page reads
# ---------------------------------------------------------------------------
# The card's sample, the MCP verdict and the flagged-filing check each read a
# company's balance sheet. Done per request they bring back the exact problem
# `api.home` documents: "1.35s TTFB against 0.23s for /pricing, on the one page
# every visitor sees first and the one Google measures". They are figures that
# only move when a quarter loads, so they share `suggest.PANELS_TTL_S` and are
# cleared by the same call (`stats.clear_page_caches` -> "panels"), which is
# what the ingest already runs when new rows land, and by the test fixture that
# stops one test's database being served to the next.
_extras_lock = threading.Lock()
_extras: dict[str, tuple[float, object]] = {}


def _memo(key: str, build):
    from src.company.suggest import PANELS_TTL_S

    now = time.monotonic()
    with _extras_lock:
        hit = _extras.get(key)
    if hit is not None and now - hit[0] < PANELS_TTL_S:
        return hit[1]
    value = build()  # outside the lock: it reads the database
    with _extras_lock:
        _extras[key] = (now, value)
    return value


def reset_home_extras_cache() -> None:
    with _extras_lock:
        _extras.clear()


# The company the examples use. AAPL, because it is the sample every other page
# (/mcp, /api) already uses, so a reader who clicks through sees the same one.
_SAMPLE_TICKER = "AAPL"

# What /api/demo stamps on its answer. Repeated here rather than imported
# because the endpoint writes it inline; tests/test_home_and_search.py compares
# this page's sample against the endpoint's own response, so a change to either
# one is caught there rather than on a reader's screen.
_DEMO_SOURCE = "SEC Financial Statement Data Sets (as reported)"


def _api_sample(ticker: str) -> dict | None:
    """What `/api/demo/{ticker}` answers, built by the call the endpoint makes.

    None when there is nothing to show -- no database, no such company -- and
    the caller then renders no card at all. A card with an invented response
    in it would be the one thing on this page that is not evidence.
    """
    import json
    from dataclasses import asdict

    from src.company.balancesheet import get_balance_sheet

    try:
        sheet = get_balance_sheet(ticker)
    except Exception as exc:  # noqa: BLE001 - a sample must never take a page down
        log.warning("home_sample_failed", ticker=ticker, error=str(exc)[:200])
        return None
    if sheet is None:
        return None
    payload = json.loads(json.dumps(asdict(sheet), default=str))
    payload["source"] = _DEMO_SOURCE
    return payload


def _sample_is_filed(ticker: str) -> bool:
    """True only when none of the company's rows are the `source='demo'`
    illustrative seed. A failed read answers False: say less, not more."""
    try:
        from sqlalchemy import func, select

        from src.storage.db import session_scope
        from src.storage.models import Fundamental

        with session_scope() as s:
            n = s.execute(
                select(func.count()).select_from(Fundamental)
                .where(Fundamental.ticker == ticker, Fundamental.source == "demo")
            ).scalar_one()
        return n == 0
    except Exception:  # noqa: BLE001 - a label must never take the page down
        return False


def _pick_sample(hero_view: View1 | None) -> tuple[str, dict | None]:
    """The first company that has a sample: AAPL, else the drawing's own."""
    candidates = [_SAMPLE_TICKER]
    if hero_view is not None:
        candidates.append(str(hero_view.as_dict().get("ticker") or ""))
    for ticker in candidates:
        if not ticker:
            continue
        sample = _api_sample(ticker)
        if sample is not None:
            return ticker, sample
    return _SAMPLE_TICKER, None


def _excerpt(sample: dict) -> tuple[str, int, int]:
    """The response, trimmed to the fields that carry the check.

    Returns the JSON text and how many concept fields were shown out of how
    many exist, so the caption can say what was left out. Every value is the
    endpoint's own: nothing is rounded, renamed or rewritten, only omitted.
    """
    import json

    def pick(section: str, keys: tuple[str, ...]) -> dict:
        have = sample.get(section) or {}
        return {k: have[k] for k in keys if k in have}

    shown = {
        "ticker": sample.get("ticker"),
        "company_name": sample.get("company_name"),
        "period_end": sample.get("period_end"),
        "filing_date": sample.get("filing_date"),
        "assets": pick("assets", ("total_assets",)),
        "liabilities": pick(
            "liabilities", ("total_liabilities", "liabilities_and_equity")
        ),
        "equity": pick("equity", ("shareholders_equity",)),
        "data_quality_issues": sample.get("data_quality_issues") or [],
        "source": sample.get("source"),
    }
    sections = ("assets", "liabilities", "equity")
    total = sum(len(sample.get(s) or {}) for s in sections)
    kept = sum(len(shown[s]) for s in sections)
    return json.dumps(shown, indent=2), kept, total


def _code_card(ticker: str, sample: dict | None) -> str:
    """A request and its real response, four ways: curl, PowerShell, Python, MCP.

    PowerShell has its own tab because in Windows PowerShell `curl` is an alias
    for Invoke-WebRequest, which does not take curl's flags: the curl tab's
    command fails there as typed. `curl.exe` reaches the real curl Windows has
    shipped since 2018, and Invoke-RestMethod is the native alternative.
    home.js opens this tab by default on Windows; without the script, curl stays
    the default.

    Radio inputs and sibling selectors do the tabbing, so the three panels are
    all in the DOM and the switch works with the script blocked. The Copy
    buttons are the only part that needs JavaScript and ship `hidden`.
    """
    if sample is None:
        return ""
    from src.report.landing import mcp_check_sample

    origin = SITE_ORIGIN
    symbol = escape(ticker)
    # "real" is a claim about where the figures came from, so it is checked, not
    # assumed: a database seeded with illustrative rows says so on the card.
    label = "real" if _sample_is_filed(ticker) else "illustrative"
    name = str(sample.get("company_name") or ticker)
    response, kept, total = _excerpt(sample)
    assets = ((sample.get("assets") or {}).get("total_assets") or {}).get("value")

    curl_req = (
        "# No key needed for the demo endpoint\n"
        f"curl {origin}/api/demo/{ticker}\n"
        "\n"
        "# Your own key opens the full API\n"
        f'curl -H "X-API-Key: YOUR_KEY" {origin}/api/company/{ticker}'
    )
    ps_req = (
        "# No key needed for the demo endpoint\n"
        f"curl.exe {origin}/api/demo/{ticker}\n"
        "\n"
        "# Your own key opens the full API\n"
        f'curl.exe -H "X-API-Key: YOUR_KEY" {origin}/api/company/{ticker}\n'
        "\n"
        "# Or PowerShell's own command, no curl.exe needed\n"
        f"Invoke-RestMethod -Uri {origin}/api/company/{ticker} "
        '-Headers @{"X-API-Key"="YOUR_KEY"}'
    )
    py_req = (
        "import requests\n"
        "\n"
        f'r = requests.get("{origin}/api/demo/{ticker}", timeout=30)\n'
        "r.raise_for_status()\n"
        "sheet = r.json()\n"
        'print(sheet["company_name"], sheet["assets"]["total_assets"]["value"])\n'
        "\n"
        "# With your key, the full API:\n"
        f'# requests.get("{origin}/api/company/{ticker}",\n'
        '#              headers={"X-API-Key": "YOUR_KEY"}, timeout=30)'
    )
    py_out = f"{name} {assets}"
    mcp_req = (
        f"claude mcp add --transport http balanceproof {origin}/mcp\n"
        "\n"
        f"# Then ask: Does {name}'s latest balance sheet balance?"
    )
    mcp = mcp_check_sample(ticker)
    mcp_block = ""
    if mcp is not None:
        import json

        text, fields = mcp
        mcp_block = f"""
      <p class="cc-cap">Response <span>what the model is handed</span></p>
      <pre class="code json" tabindex="0"><code>{escape(text)}

{escape(json.dumps(fields, indent=2))}</code></pre>"""

    def copy_btn(target: str) -> str:
        return (
            f'<button type="button" class="cc-copy" data-copy-from="#{target}" '
            'hidden>Copy</button>'
        )

    return f"""
  <section class="codecard" id="example" aria-label="A request and its response">
    <div class="cc">
      <input class="cc-radio" type="radio" name="cc-tab" id="cc-t-curl"
        aria-label="curl" checked>
      <input class="cc-radio" type="radio" name="cc-tab" id="cc-t-ps"
        aria-label="PowerShell">
      <input class="cc-radio" type="radio" name="cc-tab" id="cc-t-py"
        aria-label="Python">
      <input class="cc-radio" type="radio" name="cc-tab" id="cc-t-mcp"
        aria-label="MCP">
      <div class="cc-tabs">
        <label for="cc-t-curl">curl</label>
        <label for="cc-t-ps">PowerShell</label>
        <label for="cc-t-py">Python</label>
        <label for="cc-t-mcp">MCP</label>
      </div>

      <div class="cc-panel cc-p-curl">
        <p class="cc-cap">Request {copy_btn("cc-curl-req")}</p>
        <pre class="code" tabindex="0"><code id="cc-curl-req">{escape(curl_req)}</code></pre>
        <p class="cc-cap">Response <span>{label}, for {symbol} &middot; scrolls</span></p>
        <pre class="code json" tabindex="0"><code>{escape(response)}</code></pre>
        <p class="cc-foot">Trimmed to {kept} of the {total} line items, the
          ones the check uses. The whole answer is
          <a href="/api/demo/{symbol}">/api/demo/{symbol}</a>.</p>
      </div>

      <div class="cc-panel cc-p-ps">
        <p class="cc-cap">Request {copy_btn("cc-ps-req")}</p>
        <pre class="code" tabindex="0"><code id="cc-ps-req">{escape(ps_req)}</code></pre>
        <p class="cc-cap">Response <span>{label}, for {symbol} &middot; scrolls</span></p>
        <pre class="code json" tabindex="0"><code>{escape(response)}</code></pre>
        <p class="cc-foot">In Windows PowerShell, plain <code>curl</code> is an
          alias for <code>Invoke-WebRequest</code> and will not take these
          flags; <code>curl.exe</code> is the real curl that ships with Windows
          10 and 11.</p>
      </div>

      <div class="cc-panel cc-p-py">
        <p class="cc-cap">Request {copy_btn("cc-py-req")}</p>
        <pre class="code" tabindex="0"><code id="cc-py-req">{escape(py_req)}</code></pre>
        <p class="cc-cap">Output <span>{label}, for {symbol}</span></p>
        <pre class="code" tabindex="0"><code>{escape(py_out)}</code></pre>
        <p class="cc-foot">Needs <code>pip install requests</code> and nothing
          else. There is also a client:
          <a href="https://pypi.org/project/balanceproof/"
             rel="noopener">pip install balanceproof</a>.</p>
      </div>

      <div class="cc-panel cc-p-mcp">
        <p class="cc-cap">Request {copy_btn("cc-mcp-req")}</p>
        <pre class="code" tabindex="0"><code id="cc-mcp-req">{escape(mcp_req)}</code></pre>{mcp_block}
        <p class="cc-foot">Claude Code, Cursor and other MCP clients take the
          same URL. <a href="/mcp">Setup for each client</a>.</p>
      </div>
    </div>
  </section>"""


def _key_form(slot: str, ticker: str, button: str = "Get my free key") -> str:
    """One field and one button: the whole of signing up.

    `POST /api/auth/register` makes the account, and the key comes back in the
    answer. The form posts to /dashboard as a plain GET when the script is
    blocked, which is the sign-up form the dashboard already has, so a blocked
    script costs a click and not the sign-up.

    The terms line sits under the field in the hero and the closing call to
    action and is what "ask" means for `accept_terms`: the server refuses an
    account without it, and the script sends it only on a submit that happened
    next to that sentence.
    """
    return f"""
    <div class="keyslot" data-keyslot>
      <form class="search keyform" action="/dashboard" method="get" data-keyform>
        <label class="slabel" for="{slot}-email">Email, for a free API key</label>
        <div class="sfield">
          <input id="{slot}-email" name="email" type="email" required
            placeholder="you@email.com" autocomplete="email"
            autocapitalize="none" spellcheck="false" maxlength="254"
            enterkeyhint="go">
          <button type="submit">{escape(button)}</button>
        </div>
        <p class="formnote" role="status" aria-live="polite" data-keynote></p>
      </form>
      <div class="keyout" data-keyout data-ticker="{escape(ticker)}" hidden>
        <p class="keyout-h" tabindex="-1">Your key. It is shown once, so copy it
          now.</p>
        <div class="keyrow">
          <code class="keyval" data-keyvalue></code>
          <button type="button" class="keycopy" data-copy-from="[data-keyvalue]"
            hidden>Copy key</button>
        </div>
        <p class="keyout-h2">Try it:</p>
        <pre class="code" tabindex="0"><code data-keycurl></code></pre>
        <button type="button" class="keycopy" data-copy-from="[data-keycurl]"
          hidden>Copy command</button>
        <p class="keyout-next"><a href="/api">API docs</a> &middot;
          <a href="/dashboard">Your dashboard</a></p>
      </div>
    </div>"""


def _count_scope(bd: dict | None) -> str:
    """The reconciled and flagged counts, each with what it counts and when.

    Scope: the most recent balance sheet held for each company, read from the
    database (`identity_breakdown`), dated by the day it was computed. It is a
    different population from the 995-of-38,568 study, which is every filing in
    a two-year window on a frozen date and is quoted, dated, on /verification.
    Printing both without their scopes is what made "no filing genuinely
    fails" and "193 flagged" read as a contradiction. "" when unreadable.
    """
    if not bd or bd.get("reconciled") is None or not bd.get("computed_on"):
        return ""
    checked = (bd.get("reconciled") or 0) + (bd.get("flagged") or 0)
    return (
        f"Latest balance sheet of each company, read {escape(str(bd['computed_on']))} "
        f"(UTC): {fmt_int(checked)} could be tested. "
        f"{fmt_int(bd['reconciled'])} reconcile and {fmt_int(bd['flagged'])} "
        "are flagged with a reason."
    )


def _hero_proof() -> str:
    """The dated counts and the way to the evidence, under the subhead."""
    try:
        from src.company.stats import identity_breakdown

        scope = _count_scope(identity_breakdown())
    except Exception:  # noqa: BLE001 - a count must never take the page down
        scope = ""
    lead = f"{scope} " if scope else ""
    return (
        f'<p class="hproof">{lead}<a href="/verification">How the check works, '
        "what the flags mean, and a worked example</a>.</p>"
    )


def _hero(free_calls: int, ticker: str) -> str:
    """The claim, the sign-up, and the way to the docs.

    The subline says "when it reports the lines needed" because some companies
    publish no total to test against (the strip below counts them out, and
    /methodology names the case). An unqualified claim that all of them are
    checked would be the one sentence on this page that its own figures contradict. "They prove they balance" is
    the site's standing tagline, which used to be the H1; it opens the subline
    so the phrase the page is known by is still the first thing under the
    headline, and three tests pin it.
    """
    return f"""
  <header class="hero">
    <h1 class="htitle">Balance-checked SEC financials for production data pipelines.</h1>
    <p class="hlede">Each statement is checked against assets = liabilities +
      equity where the filing reports the lines needed, and returned with the
      SEC filing it came from. When a statement can't be verified, the API
      returns the reason instead of adjusting the numbers.</p>
    {_hero_proof()}
    {_key_form("hero", ticker)}
    <p class="summary">Free tier: {free_calls:,} calls a month. No card, no password.</p>
    <p class="fineprint">By getting a key you accept the
      <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.</p>
    <div class="api-cta hero-cta">
      <a class="btn ghost" href="/api">API docs</a>
    </div>
    <div class="hero-search">{search_form()}</div>
  </header>"""


# The hero drawing is full size, not a thumbnail. The brief for this page is
# "real output above the fold", and 300px is the column the company page uses
# -- so what a visitor sees first is literally the product, at the size the
# product is served at.
_HERO_COLUMN_PX = 300.0


def _status_strip(stats: dict) -> str:
    """The line across the top: what is loaded, how fresh, and how well it adds up.

    Cells with rules between them rather than one sentence with middle dots in
    it. Every figure is counted live, and any figure that cannot be counted is
    dropped -- a strip reporting "0" for something it merely failed to read
    would be worse than a shorter strip.

    `data-countup` marks the figures the single animation on this site touches.
    """
    cells: list[str] = []

    def cell(key: str, value: str, live: bool = False, count: str = "",
             kind: str = "") -> None:
        attr = f' data-countup="{count}"' if count else ""
        cls = "sv live" if live else "sv"
        cells.append(
            f'<div class="scell{kind}"><span class="sk">{escape(key)}</span>'
            f'<span class="{cls}"{attr}>{value}</span></div>'
        )

    cell("Source", "SEC EDGAR", kind=" txt")
    if stats.get("facts"):
        cell("Facts", escape(_compact(stats["facts"])), count=str(stats["facts"]))
    if stats.get("companies"):
        cell("Companies", escape(fmt_int(stats["companies"])),
             count=str(stats["companies"]))
    if stats.get("latest_filing"):
        cell("Latest filing", escape(str(stats["latest_filing"])), kind=" txt")
    # Verification, as three counts rather than one rate. A percentage here
    # would fall as coverage improves -- see /methodology -- so the strip
    # reports what was found, not what fraction of it passed.
    from src.company.stats import identity_breakdown

    b = identity_breakdown() or {}
    if b.get("reconciled") is not None:
        cell("Reconciled", escape(fmt_int(b["reconciled"])),
             count=str(b["reconciled"]), live=True, kind=" key")
    if b.get("flagged") is not None:
        cell("Flagged with a reason", escape(fmt_int(b["flagged"])),
             count=str(b["flagged"]), kind=" key")
    # "Hidden" was an ops label facing the public: a visitor reads it as
    # "things you are not showing me", which is the opposite of what the strip
    # is for. The count still exists and is still on /admin.
    #
    # No "Updated · Nd ago" cell. It showed the last time any scheduler job
    # loaded new rows, so it read as stale over a weekend or a holiday when the
    # data was complete, and as fresh after a price-bar run that changed no
    # balance sheet. "Latest filing" above says how current the data is.
    return f'<div class="strip">{"".join(cells)}</div>'


# The drawing is the page's second focal point, so it gets a stage: the real
# balance sheet at a size you can read, and beside it the thing the product is
# FOR -- a real filing that does not add up, drawn with the gap shown instead of
# hidden. The line-by-line tables that used to run for twelve rows under a
# single drawing are folded into a <details>: still in the page for search
# engines and screen readers, no longer the longest thing on it.
_STAGE_COLUMN_PX = 340.0

# Filings offered as "flagged, and here is why", tried in order. Each is
# VERIFIED when the page renders: the panel appears only if the filing still
# fails the plain check AND the filer's own stated total balances, which is the
# precise claim its caption makes (the gap is in what we read, not in the
# company). A candidate that now balances, or that fails for another reason,
# is skipped rather than shown under a caption that would be false.
_FLAGGED_CANDIDATES = ("AACB",)

# Companies the home page's story prefers to lead with, in order, when they
# are among the drawable suggestions: their real drawings carry several line
# items on both sides. Chosen for the shape, never for the verdict: the panel
# is captioned "Balances" only because `_hero_drawing` is given a view that
# balances (the gallery's suggestions do).
_STORY_LEAD = ("MSFT", "AAPL")


def _flagged_example() -> View1 | None:
    from src.company.view1 import build_view1
    from src.report.company_facts import stated_agrees

    for ticker in _FLAGGED_CANDIDATES:
        try:
            view = build_view1(ticker)
        except Exception as exc:  # noqa: BLE001 - the page must not fall over
            log.warning("home_flagged_failed", ticker=ticker, error=str(exc)[:200])
            continue
        if view is None or view.balances:
            continue
        if stated_agrees(view) and any(b.key == "unread_components" for b in view.claims):
            return view
    return None


def _drawing_panel(view: View1, *, tag: str, tone: str, caption: str, kind: str = "",
                   scene: int = 0) -> str:
    """One filing drawn to scale: both columns, a plain caption, and the
    line-by-line tables folded away.

    Each column's bands sit in a `.rise` wrapper so the motion layer can slide
    them up from the baseline inside the column (transform only, the bands keep
    their true heights the whole time). Without motion the wrapper is inert."""
    d = view.as_dict()
    total = d["total_assets"]
    px = _STAGE_COLUMN_PX / 100.0

    assets_html = "".join(_band(b, px) for b in d["assets"])
    above = [b for b in d["claims"]
             if not (b["kind"] == "equity" and b["value"] < 0)]
    below = [b for b in d["claims"] if b["kind"] == "equity" and b["value"] < 0]
    claims_html = "".join(
        _band(b, px).replace('class="band', 'class="band gapband', 1)
        if b.get("key") == "unread_components" else _band(b, px)
        for b in above
    )
    below_html = "".join(_band(b, px) for b in below)

    liab = sum(b["value"] for b in d["claims"] if b["kind"] != "equity")
    eq = sum(b["value"] for b in d["claims"] if b["kind"] == "equity")
    name = escape(d["company_name"] or d["ticker"])
    ticker = escape(d["ticker"])
    why = f'<span class="ckind">{escape(kind)}</span>' if kind else ""
    below_block = (
        f'<div class="baseline"></div><div class="stack below"><div class="rise">{below_html}</div></div>'
        if below_html else ""
    )
    # The level line: drawn only where the two columns really meet.
    level = ('<span class="lvl" aria-hidden="true"><i>A = L + E</i></span>'
             if tone == "ok" else "")
    return f"""
    <figure class="bs-panel {tone}" data-scene="{scene}">
      <figcaption class="bs-tag"><b>{escape(tag)}</b>
        <span class="bs-meta">{ticker} {why} quarter ended {escape(d["period_end"])},
          filed {escape(d["filing_date"])}</span></figcaption>
      <div class="bs">
        <div class="bs-cols">
          <div class="bs-cap"><span>{name} owns</span><b>{money(total)}</b></div>
          <div class="bs-cap"><span>Owed &amp; owned</span>
            <b>{money(liab)} + {money(eq)}</b></div>
          <div class="bs-col" role="img" aria-label="What {name} owns, drawn to scale, total assets {money(total)}. Line-by-line amounts in the table below.">
            {level}<div class="stack"><div class="rise">{assets_html}</div></div>
          </div>
          <div class="bs-col" role="img" aria-label="Who has a claim on it, drawn to scale, liabilities {money(liab)}, equity {money(eq)}. Line-by-line amounts in the table below.">
            <div class="stack"><div class="rise">{claims_html}</div></div>
            {below_block}
          </div>
        </div>
        <details class="bs-lines"><summary>Line by line</summary>
          <table class="legend"><caption class="vh">Assets, line by line</caption>
            <tbody>{_legend_rows(d["assets"], total)}</tbody></table>
          <table class="legend"><caption class="vh">Liabilities and equity, line by line</caption>
            <tbody>{_legend_rows(d["claims"], total)}</tbody></table>
        </details>
      </div>
      <p class="bs-note">{caption}</p>
    </figure>"""


def _not_testable_panel(scene: int) -> str:
    """The third case, drawn as what it is: two empty outlines and no figures.
    Nothing here is a company or a number we invented; the only figure is the
    live count of latest balance sheets that could not be tested, with its
    date, and it is dropped when it cannot be read."""
    from src.company.stats import identity_breakdown

    try:
        bd = identity_breakdown() or {}
    except Exception:  # noqa: BLE001 - a count must never take the page down
        bd = {}
    n = bd.get("not_testable")
    count = ""
    if n and bd.get("computed_on"):
        count = (f'<p class="nt-count">On {escape(str(bd["computed_on"]))} (UTC), '
                 f'{_num(fmt_int(n))} {plural(n, "latest balance sheet", "latest balance sheets")} '
                 "could not be tested. They are counted in neither the pass nor the "
                 "fail total.</p>")
    # What the check needs, as `stats.classify_identity` decides it: total
    # assets above zero, and liabilities and equity (or the filer's own stated
    # total for both). Either missing means not_testable.
    needs = (
        ("01", "Total assets", "reported, and above zero"),
        ("02", "Liabilities and equity", "each reported, or the filer's own stated total"),
    )
    rows = "".join(
        f'<li><span class="nn">{n}</span><b>{t}</b><span class="nd">{d}</span>'
        f'<span class="nx" aria-hidden="true"></span></li>'
        for n, t, d in needs)
    return f"""
    <figure class="ntpanel" data-scene="{scene}">
      <figcaption class="bs-tag"><b>Not testable</b>
        <span class="bs-meta">answered <code>not_testable</code>, with the reason</span></figcaption>
      <div class="nt-body">
        <div class="nt-cols" aria-hidden="true">
          <div class="nt-col"><span>Owns</span><i>no figure drawn</i></div>
          <div class="nt-col"><span>Owed &amp; owned</span><i>no figure drawn</i></div>
        </div>
        <div class="nt-side">
          <p class="nt-h">What the check needs</p>
          <ol class="nt-needs">{rows}</ol>
          <p class="nt-out"><code>not_testable</code> if either is missing: no verdict,
            the reason in the response, counted in neither total.</p>
        </div>
      </div>
      <p class="bs-note">Nothing is filled in and no total is estimated.</p>
      {count}
    </figure>"""


def _hero_drawing(view: View1 | None, kind: str = "",
                  flagged: View1 | None = None) -> str:
    """Real filings drawn to scale: one that balances, a real one that is
    flagged with its reason (when one can be verified), and what "not
    testable" looks like. With motion on, each drawing fills in as it comes
    into view: the bands rise from the baseline to their true heights and
    the labels appear in order. Without it, the finished drawings.

    There is no placeholder when there is nothing to draw: the page falls back
    to the strip and the demo, which is a smaller page rather than a page
    showing a picture of nothing and calling it an example.
    """
    if view is None:
        return ""

    d = view.as_dict()
    name = escape(d["company_name"] or d["ticker"])
    ticker = escape(d["ticker"])
    total = d["total_assets"]
    good = _drawing_panel(
        view, tag="Balances", tone="ok", kind=kind, scene=0,
        caption=f"The two columns agree. {name} owns {money(total)}, and every "
                "dollar of it is owed or owned.",
    )
    bad = ""
    if flagged is not None:
        f = flagged.as_dict()
        gap = next(b["value"] for b in f["claims"] if b["key"] == "unread_components")
        fname = escape(f["company_name"] or f["ticker"])
        bad = _drawing_panel(
            flagged, tag="Flagged, and here is why", tone="flag", scene=1,
            caption=f"{fname}: the filer's own stated total balances. {money(gap)} "
                    "of it did not reach us, so the drawing shows the gap instead "
                    "of hiding it.",
        )
        title = "Real filings, drawn to scale"
    else:
        title = "One real filing, drawn"
    nt = _not_testable_panel(2 if bad else 1)
    cls = "two" if bad else "one"
    flagged_link = (
        f' See the flagged one on its <a href="/company/{escape(flagged.ticker)}">'
        f"own page</a>." if flagged is not None else ""
    )
    return f"""
  <section class="sec hero-bs stage {cls}" id="story" aria-labelledby="story-h">
    <div class="draw-head">
      <div><div class="secno"><span>01</span> The check, drawn</div>
        <h2 class="bigh" id="story-h" data-reveal>{title}</h2></div>
      <div class="draw-notes">
        <p class="plan-note">Left is what the company owns. Right is who has a claim
          on it: lenders first, then the owners.</p>
        <p class="plan-note">Both columns are the same money, counted twice, so they
          are the same height. A filing where they differ is flagged with the reason.</p>
      </div>
    </div>
    <div class="draw-grid">{good}{bad}{nt}
    </div>
    <p class="plan-note story-links">
      <a href="/company/{ticker}">Open the full page for {ticker}</a>, or
      <a href="/companies">browse every company</a>.{flagged_link}
      A later filing can report a new value for the same period:
      <a href="/restatements">revisions from the last 90 days</a>.</p>
  </section>"""


def _steps(stats: dict) -> str:
    """Three short answers, and the only place the method is explained.

    How a filing is checked, what a flag means, where the data comes from.
    It replaces a schematic, a three-row "why this is hard" table, a four-block
    trust grid and a "what this doesn't do" list, which between them said the
    check twice, the flag taxonomy once more than /methodology does, and the
    source three times. The quotable sentence stays, once, as this section's
    opening line, because it is the one an assistant lifts when asked what the
    product does.

    The count is the strip's own (`identity_breakdown`): reconciled plus
    flagged. Two definitions of "checkable" once sat a few inches apart on this
    page, so this sentence reads the same function the strip does.
    """
    from src.company.stats import identity_breakdown

    bd = identity_breakdown() or {}
    checked = (
        (bd.get("reconciled") or 0) + (bd.get("flagged") or 0)
        if bd.get("reconciled") is not None
        else None
    )
    tested = (
        f"All {fmt_int(checked)} companies with a complete balance sheet are "
        "checked against assets = liabilities + equity."
        if checked
        else "Every balance sheet that reports its totals is checked against "
        "assets = liabilities + equity."
    )
    # Every flagged company is in exactly one of the four reasons, so the four
    # numbers add up to the flagged count printed beside them. This replaces
    # "Today no filing genuinely fails the check", which was true (the fourth
    # reason is 0) but read as a contradiction of the flagged count above it.
    c = bd.get("counts") or {}
    if bd.get("computed_on") and bd.get("flagged") is not None and c:
        broken_line = (
            f" On {escape(str(bd['computed_on']))} (UTC), across the latest "
            f"balance sheet of each company, {fmt_int(bd['flagged'])} are "
            f"flagged: {fmt_int(c.get('rounding', 0))} rounding, "
            f"{fmt_int(c.get('missing_tag', 0))} a line item that did not "
            f"reach us, {fmt_int(c.get('unexplained', 0))} with no stated "
            f"total to test against, and {fmt_int(c.get('broken', 0))} where "
            "the filer's own stated total disagrees with its own total assets."
        )
    else:
        broken_line = ""

    return f"""
  <section class="sec" id="how">
    <div class="sec-head"><h2>How it works</h2></div>
    <!-- THE QUOTABLE SENTENCE, and the only copy of it on the page. It is the
         sentence an assistant lifts when asked what this product does, and
         `_HERO_METHOD` keeps it in one place so the wording cannot drift from
         /about and /methodology. -->
    <p class="sec-sub">{_HERO_METHOD}</p>
    <ol class="steps trust">
      <li class="tblock">
        <h3><span class="stepno">1</span> How a filing is checked</h3>
        <p>{tested} The filer's own stated total is the referee: it tells us
          whether a gap is in the filing or in what we read from it, and no
          figure is adjusted to make a sheet fit.</p>
      </li>
      <li class="tblock">
        <h3><span class="stepno">2</span> What a flag means</h3>
        <p>A flag is a reason, not an accusation. There are four: rounding, a
          line item that did not reach us, no stated total to test against,
          and a filing that really does not balance.{broken_line}
          A separate count is of filings, not companies: in a snapshot
          frozen on 2026-10-09, 995 of 38,568 filings failed the check, and
          971 of those 995 are labelled a line item that did not reach us.
          <a href="/methodology">The four reasons, with examples</a> and
          <a href="/verification">the scope of each count</a>.</p>
      </li>
      <li class="tblock">
        <h3><span class="stepno">3</span> What you get back</h3>
        <p>JSON with every line item, its period and the filing it came from,
          a vendor-restated marker (FMP or Yahoo, not SEC revisions), and the reason when a sheet is flagged. The
          same answer over the API, the Python client or the MCP server.</p>
      </li>
    </ol>
  </section>"""


def _plan_bullets() -> dict[str, tuple[str, ...]]:
    """Two or three things each plan includes, read from the same tables the
    gates enforce (`plans.MATRIX`, the quota settings) so a tile cannot promise
    what the server refuses. Wording follows /pricing's own cards."""
    from src import plans

    def cell(tier: str, feature: str):
        try:
            return plans.allowance(tier, feature)
        except Exception:  # noqa: BLE001 - drop the number, keep the sentence
            return None

    def years(n) -> str:
        return "All loaded history" if n is None else f"{n} year{'s' if n != 1 else ''} of history"

    from src.dataset import facts_label

    try:
        rows = facts_label()
    except Exception:  # noqa: BLE001
        rows = ""
    feed, bulk = cell("pro", "exceptions_feed"), cell("pro", "bulk_verify")
    return {
        "Free": (
            "Balance sheet, income statement and cash flow, each period checked when the filing reports the lines (otherwise not_testable)",
            years(cell("free", "history_years")),
            "MCP server for Claude, ChatGPT and Cursor",
        ),
        "Starter": (
            "Multi-year history",
            "What changed, including restatements",
            f"Alerts on {cell('starter', 'watchlist')} companies",
        ),
        "Pro": (
            "Point-in-time figures for backtests (as_of)",
            f"Feed of failed checks, last {feed} days" if feed else "Feed of failed checks",
            f"Check {bulk} companies in one request" if bulk else "Bulk checks",
        ),
        "Business": (
            "Everything in Pro",
            f"Webhooks into your systems ({cell('business', 'webhooks')} endpoints)",
            "SEC filing link on every figure",
        ),
        "Dataset": (
            "One-time purchase, repeat downloads included",
            f"{rows} rows as one CSV file" if rows else "Every filed figure as one CSV file",
            "Open it in Excel or pandas",
        ),
        "Enterprise": (
            "Every paid-plan feature, plus the dataset",
            "A call quota set to what you use",
            "Invoicing and a signed agreement",
        ),
    }


# The badge is the owner's call, not something measured here: nothing in the
# code knows which plan sells most. One constant, so it is a one-line change.
_MOST_POPULAR = "Pro"


def _pricing_summary(stats: dict | None = None) -> str:
    """The plans, as the same cards /pricing shows, with the same buttons.

    `plan_cards()` is the one place a plan is written. Its Start buttons carry
    `data-plan`, which home.js turns into a Stripe Checkout Session (and a 401
    into a sign-in that comes back to the plan); its billing toggle swaps the
    price, unit, line and plan; `#plan-note` is where checkout reports. The
    home page passes three things /pricing does not: two or three bullets per
    plan read from `plans.MATRIX` (`_plan_bullets`), the "Recommended for production" badge,
    and the per-call price. Starter and Business appear only once their Stripe
    Price is set, exactly as on /pricing.
    """
    from src import billing, plans
    from src.config.settings import get_settings, price_label
    from src.report.pricing_page import plan_cards, snapshot_date

    stats = stats or {}
    s = get_settings()
    rows = _compact(stats.get("facts") or 0) if stats.get("facts") else None
    saving = max(0, s.pro_price_usd * 12 - s.pro_annual_price_usd)
    cards = plan_cards(
        free_limit=s.free_tier_monthly_calls,
        pro_limit=s.pro_tier_monthly_calls,
        pro_price=price_label(s.pro_price_usd),
        pro_annual_price=price_label(s.pro_annual_price_usd),
        annual_saving=price_label(saving),
        dataset_price=price_label(s.dataset_price_usd),
        dataset_rows=rows or "Every",
        dataset_as_of=snapshot_date(stats.get("generated")),
        bullets=_plan_bullets(),
        popular=_MOST_POPULAR,
        per_call=True,
        lead=("pro", "business"),
    )

    try:
        feed = plans.allowance("pro", "exceptions_feed")
        alerts = plans.allowance("pro", "watchlist")
        extras = (f"point-in-time figures for backtests, the {feed}-day feed of "
                  f"failed checks, and alerts on {alerts} companies")
    except Exception:  # noqa: BLE001 - say what is certain, drop the numbers
        extras = "point-in-time figures for backtests and the feed of failed checks"
    lead = ("Pro costs more per call than Starter because it is not only volume: "
            if billing.price_for("starter") else "Pro is not only volume: ")
    # What the code does at the limit (accounts.enforce_monthly_limit): the call
    # is refused with a 429 that names the count, and the count resets on the
    # 1st (UTC). Nothing in the code bills beyond the plan price.
    return f"""
  <section class="sec" id="pricing">
    <div class="sec-head"><h2>Pricing</h2></div>
    <p class="sec-sub">Reading the site is free. The key is what scales: more
      calls, more history, alerts. All prices are in Canadian dollars (CAD).</p>
    {cards}
    <p class="formnote" id="plan-note" role="status" aria-live="polite"></p>
    <p class="psum-note">{lead}it adds {extras}.</p>
    <p class="psum-limit"><b>At the limit:</b> further calls are refused with
      HTTP 429 until the 1st (UTC), or until you move to a larger plan. There
      is no overage billing.</p>
    <p class="plan-note"><a href="/pricing#compare-plans">Every plan, feature by
      feature</a>. Paid plans go through Stripe; card details never reach this
      site.</p>
  </section>"""


def _drawn_explained() -> str:
    """How to read the drawing above, in two sentences, attached to the REAL
    filing rather than to a schematic: an invented company with round numbers
    has no place beside real figures, so the explanation points at JPM's own
    columns instead of drawing a made-up pair."""
    return """
  <section class="sec drawn-explained" id="drawn">
    <h3 class="how-h">Both columns are the same money, counted twice</h3>
    <p>In the drawing above, the left column is what the company owns and the
      right is who has a claim on it: lenders first, then the owners. Every
      band is drawn at the size the company reported, so the two columns are
      the same height whenever the filing balances.</p>
  </section>"""


_HARD = (
    ("Filings don't say things once",
     "JPMorgan reports “total assets” twenty-three times in one filing: by "
     "segment, by entity, by fair-value level. Exactly one of them is the "
     "company, and the wrong one looks just as plausible."),
    ("Point-in-time figures",
     "A balance sheet is an instant; revenue covers three months. Read a "
     "duration where an instant belongs and assets can come out negative."),
    ("Every industry files differently",
     "A bank reports loans and deposits, not inventory. Look for the retail "
     "tags on a bank and you get an empty box."),
)


def _hard() -> str:
    blocks = "".join(
        f'<div class="hardrow"><h3>{escape(t)}</h3><p>{escape(b)}</p></div>'
        for t, b in _HARD
    )
    return f"""
  <section class="sec" id="hard">
    <div class="sec-head"><h2>Why this is hard</h2></div>
    <div class="hard">{blocks}</div>
  </section>"""


def _where_from(stats: dict) -> str:
    """Where the figures come from, as four short lines."""
    from src.company.stats import identity_breakdown

    bd = identity_breakdown() or {}
    checked = (
        (bd.get("reconciled") or 0) + (bd.get("flagged") or 0)
        if bd.get("reconciled") is not None else 0
    )
    check = (
        f"All {fmt_int(checked)} companies with a complete balance sheet are "
        "checked against assets = liabilities + equity; the rest are drawn with "
        "what is missing named."
        if checked else
        "Every balance sheet that reports its totals is checked against assets "
        "= liabilities + equity; the rest are drawn with what is missing named."
    )
    items = (
        ("Source", 'The SEC\'s own <a href="https://www.sec.gov/dera/data/'
         'financial-statement-data-sets" rel="noopener">Financial Statement Data '
         "Sets</a>, plus its XBRL frames API. Nothing scraped, bought or estimated."),
        ("Freshness", "Every load starts from the last filing already held, so "
         "a gap of any length closes on the next run."),
        ("As reported", "Figures are as reported to the SEC. When a later filing "
         "revises one, the default view shows the revised value under the later "
         "filing's date; as_of returns the value known on a past date."),
        ("The check", escape(check)),
    )
    lis = "".join(f"<li><b>{t}.</b> {b}</li>" for t, b in items)
    return f"""
  <section class="sec" id="sources">
    <div class="sec-head"><h2>Where the figures come from</h2></div>
    <ul class="shortlist">{lis}</ul>
    <p class="method"><a href="/methodology">How the check works</a> &middot;
      <a href="/best/sec-filings-api-for-quants">The SEC filings APIs worth
      considering for quant work</a></p>
  </section>"""


def _limits() -> str:
    items = (
        "No predictions, no scores, no recommendations.",
        "Nothing estimated: where a company doesn't report something, the page "
        "says so rather than showing zero.",
        "Every figure traces to a filing, with the date it was filed.",
    )
    lis = "".join(f"<li>{escape(i)}</li>" for i in items)
    return f"""
  <section class="sec" id="limits">
    <div class="sec-head"><h2>What this doesn't do</h2></div>
    <ul class="shortlist">{lis}</ul>
  </section>"""


def _plan_faq() -> str:
    """The four questions asked before a first key or a first payment, answered
    from what the code does: no card for Free (registration takes an email),
    cancellation through Stripe's portal (the dashboard's "Manage
    subscription"), the calls the meter counts (`accounts.record_call`), and the
    429 at the limit (`accounts.enforce_monthly_limit`)."""
    from src.accounts import tier_limit

    free = tier_limit("free")
    rows = (
        ("Do I need a card for the Free plan?",
         f"No. A free key needs only an email address: no card and no password. "
         f"It gives you {free:,} calls a month, and nothing is charged unless you "
         "choose a paid plan yourself."),
        ("Can I cancel any time?",
         "Yes. Paid plans are Stripe subscriptions and you cancel them yourself, "
         "from Manage subscription in your dashboard, with no email needed. Pro, "
         "monthly or annual, also has a 14-day refund window. The dataset is a "
         "file, so it is not refundable once downloaded."),
        ("What counts as a call?",
         "One request to a keyed endpoint (company, history, statements, changes "
         "or exceptions), one tool call through the MCP server, or one company "
         "inside a bulk check. Looking things up on the site, the live demo on "
         "this page and checking your own usage do not count."),
        ("What happens when I go over the limit?",
         "Further calls are refused with HTTP 429 and a message that says how "
         "many you have used. The count resets on the 1st (UTC), or you can move "
         "to a larger plan from the dashboard at any time. There is no overage "
         "billing, so a busy month never becomes a surprise bill."),
    )
    items = "".join(
        f'<details class="faq-item"><summary>{escape(q)}</summary>'
        f'<div class="faq-a"><p>{escape(a)}</p></div></details>'
        for q, a in rows
    )
    return f"""
  <section class="sec" id="plan-faq">
    <div class="sec-head"><h2>Plan questions</h2></div>
    <div class="faq">{items}</div>
  </section>"""


# The published report this note rests on (blog.py: "995 of 38,568 SEC Filings
# Failed Our Balance Sheet Check"). They are a frozen snapshot, not live counts,
# so they are named here once and a test checks them against the post itself.
_REPORT_SLUG = "which-sec-balance-sheets-fail-the-identity-check"
_REPORT_TESTED, _REPORT_FAILED, _REPORT_OURS = 38_568, 995, 971


def _cta_strip(free_calls: int) -> str:
    """A way in for the reader who has just understood the method and has not
    yet reached the pricing."""
    return f"""
  <aside class="sec cta-strip" id="try">
    <h2>Balance sheets that prove they balance.</h2>
    <p class="sec-sub">A free key is {free_calls:,} calls a month. No card, no
      password.</p>
    <div class="api-cta">
      <a class="btn" href="#start">Get free key</a>
      <a class="btn ghost" href="/api">API docs</a>
    </div>
  </aside>"""


def _why_note() -> str:
    """The reason this exists, in the company's voice ("we", no name, no age),
    using only what the published report says. The numbers are the report's;
    the sentence about the 971 is the report's own label (`missing_tag`) in
    plain words, and it is the honest half of the finding: most failures are a
    line we did not read, not a mistake by the company."""
    return f"""
  <section class="sec why" id="why">
    <div class="sec-head"><h2>Why we built this</h2></div>
    <p class="why-p">We built BalanceProof to check SEC balance sheets
      against Assets = Liabilities + Equity, and to say so when one doesn't add
      up or can't be tested.</p>
    <p class="why-p">When we ran that check across {fmt_int(_REPORT_TESTED)} filings
      in a frozen copy of our database, {fmt_int(_REPORT_FAILED)} failed. In
      {fmt_int(_REPORT_OURS)} of them the filer's own total agrees with its assets
      and a line on the credit side did not reach our sum, so the gap was in what
      we read and not in the company.
      <a href="/blog/{_REPORT_SLUG}">Read the report</a>: the method, the charts
      and the filings themselves.</p>
  </section>"""


def _closing_cta(free_calls: int, ticker: str) -> str:
    """The sign-up again, for the reader who scrolled the whole page first."""
    return f"""
  <aside class="sec cta" id="start">
    <div class="sec-head"><h2>Get your free API key</h2></div>
    <p class="sec-sub">Take a company where you know the answer, call it, and
      compare the response with EDGAR. {free_calls:,} calls a month, no card,
      no password.</p>
    {_key_form("cta", ticker)}
    <p class="fineprint">By getting a key you accept the
      <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.</p>
    <div class="api-cta">
      <a class="btn ghost" href="/api">API docs</a>
      <a class="btn ghost" href="/methodology">How we verify</a>
    </div>
  </aside>"""


# ---------------------------------------------------------------------------
# The home page, bold layout (2026-10-19, second pass): a saturated full-bleed
# hero, a proof strip with a ticker marquee, the drawings in one big card,
# "Built for" cards, why pipelines break, how the check works with a diagram,
# the demo on a coloured band, pricing, trust, FAQ, and a dark closing band.
#
# MOTION. Everything below renders complete and readable with no script: the
# final text, the final numbers and the final drawing are in the HTML. Motion
# is switched on by home-motion.js adding `motion` to <html> -- only when the
# reader has not asked for reduced motion and has not switched it off with the
# footer toggle -- and every animation rule in home-light.css is scoped under
# `.motion`. No script ever writes a number: figures animate by position and
# opacity of the final glyphs, so a stalled frame can only show the right
# number, partly moved.
# ---------------------------------------------------------------------------

_TALK_HREF = ("mailto:support@balanceproof.dev"
              "?subject=BalanceProof%20for%20our%20pipeline")
_HEADLINE = "Balance-checked SEC financials for production data pipelines."

def _num(value: str) -> str:
    """A figure the motion layer may slide into place. The text is the final
    value; nothing ever rewrites it."""
    return f'<span class="num">{value}</span>'


def _lines(json_text: str) -> str:
    """A JSON excerpt as one span per line, for the line-by-line reveal. The
    text is complete in the HTML; the reveal only moves and fades it."""
    return "\n".join(
        f'<span class="ln" style="--i:{i}">{escape(line)}</span>'
        for i, line in enumerate(json_text.split("\n"))
    )


def _hero_ledger() -> str:
    """The hero's object: one real check, drawn to scale. The figures are the
    captured AAPL `check_balance_sheet` response (samples.CHECK_AAPL), read
    from it here rather than retyped; heights are their true proportions, so
    the two columns meet because the filing balances. The motion layer only
    slides the bars up and draws the level line."""
    import json

    from src.report.samples import CAPTURED, CHECK_AAPL

    c = json.loads(CHECK_AAPL)
    a, li, eq = c["total_assets"], c["total_liabilities"], c["total_equity"]
    lp, ep = li / a * 100.0, eq / a * 100.0
    verdict = "balanced, as filed" if c.get("balances") else "flagged"
    return f"""
      <figure class="hledger" aria-labelledby="hl-cap">
        <div class="hl-head"><span class="hl-tk">{escape(c["ticker"])}</span>
          <span>10-Q &middot; quarter ended {escape(c["period_end"])} &middot; filed {escape(c["filing_date"])}</span></div>
        <div class="hl-cols">
          <div class="hl-col" role="img" aria-label="Total assets {money(a)}">
            <div class="hl-rise"><div class="hl-bar a" style="flex:1 1 0">
              <span class="hl-l">Total assets<b>{money(a)}</b></span></div></div>
          </div>
          <div class="hl-col" role="img" aria-label="Liabilities {money(li)} plus equity {money(eq)}">
            <div class="hl-rise">
              <div class="hl-bar e" style="flex:{ep:.3f} 1 0"><span class="hl-l">Equity<b>{money(eq)}</b></span></div>
              <div class="hl-bar l" style="flex:{lp:.3f} 1 0"><span class="hl-l">Liabilities<b>{money(li)}</b></span></div>
            </div>
          </div>
          <span class="hl-level" aria-hidden="true"></span>
        </div>
        <div class="hl-verdict"><span class="hl-eq">A = L + E</span>
          <code>"identity_status": "{escape(c["identity_status"])}"</code>
          <span class="hl-ok">{escape(verdict)}</span></div>
        <figcaption id="hl-cap"><code>check_balance_sheet</code> for AAPL: the real
          response, captured from production {CAPTURED}, drawn to scale.</figcaption>
      </figure>"""


def _bold_hero(free_calls: int, ticker: str) -> str:
    """Hero. Left: the headline, one subhead, the email row, "Talk to us",
    one line of small print. Right: one real check, drawn to scale."""
    return f"""
  <section class="bhero" aria-label="BalanceProof">
    <div class="bg" aria-hidden="true"><canvas class="bg-gl" data-tone="light"></canvas></div>
    <div class="bhero-in wrap">
      <header class="hero">
        <p class="hero-eyebrow"><span class="edot" aria-hidden="true"></span>SEC EDGAR &middot; every sheet checked against A = L + E</p>
        <h1 class="htitle">Balance-checked SEC financials for <em>production</em> data pipelines.</h1>
        <p class="hlede">Each statement is checked against assets = liabilities +
          equity where the filing reports the lines needed, and returned with the
          SEC filing it came from. When a statement can't be tested, the API
          returns the reason instead of adjusting the numbers.</p>
        {_key_form("hero", ticker, button="Get free key")}
        <div class="hero-actions"><a class="btn-talk" href="{_TALK_HREF}">Talk to us</a>
          <a class="hero-how" href="/verification">How the check works</a></div>
        <p class="summary">Free tier: {free_calls:,} calls a month. No card, no password.
          By getting a key you accept the <a href="/terms">Terms</a> and
          <a href="/privacy">Privacy Policy</a>.</p>
      </header>
      {_hero_ledger()}
    </div>
  </section>"""


# The climb: a layered scene above the hero (2026-10-20; v3 layer set the same
# evening). Plates in static/media/hero/: a tall sky (night, brand blue, cream),
# misty stone ridges, the towers (lapis and travertine under a brass beam), dark
# blocks at both edges and two clouds. home-light.css puts the beam's top-centre
# (50.15% / 11.72% of the towers plate) at the point the camera zooms on and the
# bubble's hairline points to. Without motion (no script, reduced motion, the
# footer toggle, or a landscape phone) the scene is one finished still: a single
# composite of the opening frame, the bubble on the beam and the copy readable,
# and none of the layer files is fetched. The display line is a paragraph: the
# page's one <h1> stays in the hero below.
_HERO_MEDIA = "/static/media/hero"

# Every candidate as (file, width, height). The first one is the `src`.
_HERO_FILES = {
    "still": (("still-1280", 1280, 720), ("still-2560", 2560, 1440)),
    "sky": (("sky-640", 640, 1280), ("sky-1600", 1600, 3200)),
    "ridges": (("ridges-v3-1280", 1280, 549), ("ridges-v3-2560", 2560, 1098)),
    "towers": (("towers-1024", 1024, 1280), ("towers-1639", 1639, 2048)),
    "fg-left": (("fg-left-824", 824, 1322),),
    "fg-right": (("fg-right-825", 825, 1298),),
    "cloud-a": (("cloud-a-1280", 1280, 562), ("cloud-a-1600", 1600, 703)),
    "cloud-b": (("cloud-b-1280", 1280, 549), ("cloud-b-1600", 1600, 686)),
}
# `sizes` per layer, shared by the <img> and its preload (they must match, or
# the browser fetches twice). The plate width is max(100vw, 160vh); the towers
# are 36% of it. On a portrait phone with motion the plate is 217vw instead
# (home-light.css), so the towers are ~78vw and the clouds 1.1-1.2 plates;
# phones without motion see only the still, at the 1280 tier.
_HERO_SIZES = {
    "still": "(max-width: 899px) 640px, max(100vw, 178vh)",
    "sky": "100vw",
    "ridges": "(max-width: 899px) 217vw, max(100vw, 160vh)",
    "towers": "(max-width: 899px) 78vw, max(36vw, 58vh)",
    "cloud-a": "(max-width: 899px) 239vw, max(100vw, 160vh)",
    "cloud-b": "(max-width: 899px) 260vw, max(100vw, 160vh)",
}
# Which screens play the climb: the same test home-story.js runs, plus the
# reader not asking for reduced motion (the footer toggle can still override
# it either way; that reader gets the right image a moment later).
_CLIMB_WIDE = "(min-width: 900px) and (min-height: 700px)"
_CLIMB_STILL = "(max-width: 899px), (max-height: 699px)"


def _hero_srcset(name: str) -> str:
    v = media_version()
    return ", ".join(f"{_HERO_MEDIA}/{f}.webp?v={v} {w}w" for f, w, _h in _HERO_FILES[name])


def _hero_img(name: str, *, cls: str = "", priority: bool = False) -> str:
    """Every layer is lazy: a hidden one (the scene on a phone, the still on a
    screen that plays the climb) is then never fetched, and the first frame's
    image is preloaded from <head> instead (`_climb_preload`)."""
    v = media_version()
    cands = _HERO_FILES[name]
    f, w, h = cands[0]
    extra = (f' srcset="{_hero_srcset(name)}" sizes="{_HERO_SIZES[name]}"'
             if len(cands) > 1 else "")
    c = f' class="{cls}"' if cls else ""
    pri = ' fetchpriority="high"' if priority else ""
    return (f'<img{c} src="{_HERO_MEDIA}/{f}.webp?v={v}"{extra} alt="" width="{w}" '
            f'height="{h}" decoding="async" loading="lazy"{pri}>')


def _climb_preload() -> str:
    """The first frame's image, fetched before layout: the towers where the
    climb plays, the still everywhere else (phones: the LCP image)."""
    v = media_version()
    out = []
    for name, media in (("towers", _CLIMB_WIDE), ("still", _CLIMB_STILL)):
        f = _HERO_FILES[name][0][0]
        out.append(f'<link rel="preload" as="image" href="{_HERO_MEDIA}/{f}.webp?v={v}" '
                   f'imagesrcset="{_hero_srcset(name)}" imagesizes="{_HERO_SIZES[name]}" '
                   f'media="{media}" fetchpriority="high">')
    return "".join(out)


def _climb() -> str:
    return f"""
  <section class="climb" id="climb" aria-labelledby="climb-line">
    <div class="climb-stage">
      <div class="climb-view"><div class="climb-art">
        {_hero_img("still", cls="climb-still", priority=True)}
        <div class="climb-scene" aria-hidden="true">
          <div class="climb-sky">{_hero_img("sky")}</div>
          <div class="climb-cam">
            <div class="climb-plate climb-ridges">{_hero_img("ridges")}</div>
            <div class="climb-plate climb-towers"><span class="climb-glow"></span>{_hero_img("towers", priority=True)}</div>
            <div class="climb-mist"></div>
          </div>
          <div class="climb-fg climb-fg-l">{_hero_img("fg-left")}</div>
          <div class="climb-fg climb-fg-r">{_hero_img("fg-right")}</div>
          <div class="climb-clouds">
            <div class="climb-cloud climb-cloud-b">{_hero_img("cloud-b")}</div>
            <div class="climb-cloud climb-cloud-a">{_hero_img("cloud-a")}</div>
          </div>
        </div>
        <div class="climb-bubble" role="note">
          <p class="b1">Up here, assets equal liabilities plus equity, or we tell you why not.</p>
          <p class="b2">Climb past the noise.</p>
        </div>
      </div>
      <div class="climb-copy">
        <p class="climb-line" id="climb-line">Every number has to hold its ground.</p>
        <p class="climb-side">Thousands of filings climb onto the SEC each quarter.
          Most of them balance. Some don&rsquo;t. We check every one that can be
          tested before you build on it.</p>
      </div>
    </div></div>
    <div class="climb-next"><p class="wrap">Balance-checked SEC financials, delivered
      as an API. <a href="#how">Here&rsquo;s how it works.</a></p></div>
  </section>"""


def _proof_bold(stats: dict, items: list[tuple[str, str]] | None = None) -> str:
    """One sentence with the counts and their UTC date, then the plain facts.
    Every part that cannot be read is dropped, never zeroed."""
    from src.company.stats import identity_breakdown

    try:
        bd = identity_breakdown() or {}
    except Exception:  # noqa: BLE001 - a count must never take the page down
        bd = {}
    lead = ""
    companies = stats.get("companies")
    if bd.get("reconciled") is not None and bd.get("computed_on"):
        checked = (bd.get("reconciled") or 0) + (bd.get("flagged") or 0)
        cover = (f"{_num(fmt_int(companies))} companies covered. " if companies else "")
        lead = (
            f'<p class="proof-lead">{cover}On '
            f'{escape(str(bd["computed_on"]))} (UTC), across the latest balance sheet '
            f"of each: {_num(fmt_int(checked))} could be tested, "
            f'{_num(fmt_int(bd["reconciled"]))} reconcile and '
            f'{_num(fmt_int(bd["flagged"]))} '
            f'{plural(bd["flagged"] or 0, "is", "are")} flagged with a reason.</p>'
        )
    elif companies:
        lead = (f'<p class="proof-lead">{_num(fmt_int(companies))} '
                "companies covered.</p>")
    facts = (f" &middot; {_num(escape(_compact(stats['facts'])))} facts"
             if stats.get("facts") else "")
    tiles = [('Source', f'SEC EDGAR{facts}')]
    if stats.get("latest_filing"):
        tiles.append(("Latest filing loaded", escape(str(stats["latest_filing"]))))
    tiles += [("API", '<a href="/api">REST API</a>'),
              ("Python", '<a href="https://pypi.org/project/balanceproof/" rel="noopener">Python client</a>'),
              ("MCP", '<a href="/mcp">MCP server</a>')]
    cells = "".join(f'<div class="pcell"><span class="pk">{k}</span>'
                    f'<span class="pv">{v}</span></div>' for k, v in tiles)
    return f"""
  <section class="bproof" aria-label="Where the data comes from">
    <div class="bg" aria-hidden="true"><canvas class="bg-gl" data-tone="dark"></canvas></div>
    <div class="wrap">
      {lead}
      <div class="proof">{cells}</div>
    </div>
  </section>"""


def _drawing_band(hero_bs: str, gallery: str) -> str:
    """The story stage, then the other filings under the same check."""
    if not hero_bs and not gallery:
        return ""
    return f"""
  <div class="bsec storysec">
    <div class="wrap">
      {hero_bs}
      {gallery}
    </div>
  </div>"""


def _built_for() -> str:
    """Three buyer cards on a horizontal rail. Desktop with motion: the
    section holds while vertical scroll slides the cards sideways, one card
    highlighted and a progress line beside it. Phones, reduced motion and no
    script: a swipeable row (phone) or a three-column grid, nothing pinned.
    Each card: one sentence and a real captured response in an inspector."""
    from src.report.samples import AGENT_SHORT, CAPTURED, FINTECH_SHORT, QUANT_SHORT

    cards = (
        ("agents", "AI-finance and agent builders",
         "An agent can check a balance sheet before it answers: "
         "<code>check_balance_sheet</code> returns the verdict and the reason.",
         "check_balance_sheet (MCP), AACB", AGENT_SHORT),
        ("quant", "Quant research and backtesting",
         "Every line item carries its period end and the date of the filing that "
         "first reported it; <code>as_of</code> (Pro) returns what was public on a date.",
         "GET /api/demo/AAPL", QUANT_SHORT),
        ("fintech", "Fintech and reporting tools",
         "Show a figure next to the filing it came from and whether the sheet "
         "balances, without writing the parsing or the reconciliation.",
         "check_balance_sheet (MCP), AAPL", FINTECH_SHORT),
    )
    body = "".join(
        f"""
          <article class="rcard tcard" id="uc-{k}" data-card="{i}" data-reveal style="--i:{i}">
            <span class="rnum">{i + 1:02d}</span>
            <h3>{escape(t)}</h3><p>{copy}</p>
            {_inspector(sample)}
            <p class="uc-src">Real response, captured {CAPTURED}: <code>{escape(src)}</code>.
              Excerpt; nothing edited.</p>
          </article>"""
        for i, (k, t, copy, src, sample) in enumerate(cards))
    dots = "".join(f'<li data-dot="{i}"></li>' for i in range(len(cards)))
    return f"""
  <section class="band dark" id="use">
    <div class="rail-pin">
      <div class="rail-stage">
        <div class="bg" aria-hidden="true"><canvas class="bg-gl" data-tone="dark"></canvas></div>
        <div class="wrap">
          <div class="secno"><span>02</span> Who it is for</div>
          <h2 class="bigh" data-reveal>Built for quants, developers and AI-finance teams.
            <span class="tone">For anyone who needs to know when a number can't be trusted.</span></h2>
          <div class="rail" tabindex="0" aria-label="Three use cases, real responses">
            <div class="rail-track">{body}
            </div>
          </div>
          <div class="rail-meter" aria-hidden="true"><span class="rail-bar"><i></i></span>
            <ol class="rail-dots">{dots}</ol></div>
        </div>
      </div>
    </div>
  </section>"""


def _dotfield() -> str:
    """The 2026-10-09 study drawn as a field of dots: 400 dots, each standing
    for an equal share of the 38,568 filings tested; the red ones are the
    share that failed (995). Derived from the dated study figures only."""
    cols, rows = 40, 10
    n = cols * rows
    red = round(_REPORT_FAILED / _REPORT_TESTED * n)
    per = round(_REPORT_TESTED / n)
    # A fixed scatter (a small LCG), so the red dots read as spread through
    # the field rather than as a line; the count is what carries the fact.
    picks, seed = set(), 7
    while len(picks) < red:
        seed = (seed * 1103515245 + 12345) % 2147483648
        picks.add(seed % n)
    dots = []
    for i in range(n):
        x, y = 10 + (i % cols) * 20, 10 + (i // cols) * 20
        cls = "dr" if i in picks else "dg"
        dots.append((cls, f'<circle cx="{x}" cy="{y}" r="4.5"/>'))
    pin = min(picks) if picks else 0
    px, py = 10 + (pin % cols) * 20, 10 + (pin // cols) * 20
    return f"""
      <figure class="dotfield" data-reveal>
        <svg viewBox="0 0 800 200" role="img" aria-label="{fmt_int(_REPORT_FAILED)} of {fmt_int(_REPORT_TESTED)} filings failed the check on 2026-10-09; each dot is about {fmt_int(per)} filings">
          <g class="gg">{''.join(c for k, c in dots if k == 'dg')}</g><g class="gr">{''.join(c for k, c in dots if k == 'dr')}</g>
          <g class="dpin" aria-hidden="true"><line x1="{px}" y1="{py}" x2="{px}" y2="{py + 34}"/></g>
        </svg>
        <figcaption><span class="dkey r"></span><b>{fmt_int(_REPORT_FAILED)} failed</b>, of which
          {fmt_int(_REPORT_OURS)} were <code>missing_tag</code>: a line item did not reach us.
          <span class="dkey g"></span>{fmt_int(_REPORT_TESTED - _REPORT_FAILED)} passed.
          Each dot is about {fmt_int(per)} filings (2026-10-09 study).</figcaption>
      </figure>"""


def _pipelines() -> str:
    """The cost, first: why SEC data breaks pipelines, as three large figures
    (the dated study and JPMorgan's duplicate facts), then the study drawn as
    a dot field. Every number is a verified, dated figure already published."""
    cols = (
        ("b", "One figure, many facts", "23",
         "JPMorgan reports total assets twenty-three times in one filing. One of "
         "them is the company."),
        ("r", "Figures change after filing", "Revised",
         'A later 10-K or 10-Q can report a new value for the same period. '
         '<a href="/restatements">The last 90 days</a>.'),
        ("y", "Totals that do not add up", fmt_int(_REPORT_FAILED),
         f"On 2026-10-09, {fmt_int(_REPORT_FAILED)} of {fmt_int(_REPORT_TESTED)} filings "
         f"failed the check; in {fmt_int(_REPORT_OURS)} a line did not reach us. "
         f'<a href="/blog/{_REPORT_SLUG}">The report</a>.'),
    )
    body = "".join(
        f'<div class="pcol c-{c}" data-reveal style="--i:{i}"><span class="pnum">{i + 1:02d}</span>'
        f'<b class="pbig{" word" if not big[0].isdigit() else ""}">{escape(big)}</b>'
        f'<h3>{escape(t)}</h3><p>{b}</p></div>'
        for i, (c, t, big, b) in enumerate(cols))
    return f"""
  <section class="bsec probsec" id="pipelines">
    <div class="wrap">
      <div class="secno alert"><span>03</span> The cost of a wrong number</div>
      <h2 class="bigh" data-reveal>Why SEC data breaks pipelines.
        <span class="tone">Three things go wrong before a number reaches your model.</span></h2>
      <p class="prob-lede">A balance sheet that does not add up still loads without an
        error. Nothing stops it reaching a model, a backtest or a client report.</p>
      <div class="pcols">{body}</div>
      {_dotfield()}
    </div>
  </section>"""


_REASONS = (
    ("rounding", "Rounding", "The lines we hold miss total assets by half of one "
     "percent to one percent."),
    ("missing_tag", "A line item did not reach us", "The filer's own stated total "
     "agrees with its assets; our components fell short."),
    ("unexplained", "No stated total to test against", "The gap is one percent or "
     "more and there is no filer total to referee it."),
    ("broken", "The filing does not balance", "The filer's own stated total "
     "disagrees with its own total assets."),
)


def _reasons() -> str:
    """Every flag, by reason, with today's live count beside it (SafetyKit's
    large list rows). The counts are the breakdown the strip and
    /verification use; a count that cannot be read is dropped, not zeroed."""
    from src.company.stats import identity_breakdown

    try:
        bd = identity_breakdown() or {}
    except Exception:  # noqa: BLE001
        bd = {}
    counts = bd.get("counts") or {}
    rows = "".join(
        f'<li class="rrow" data-reveal style="--i:{i}"><code class="rk">{k}</code>'
        f'<b class="rt">{escape(t)}</b><span class="rd">{escape(d)}</span>'
        + (f'<span class="rc">{_num(fmt_int(counts[k]))}</span>' if k in counts else "")
        + '</li>'
        for i, (k, t, d) in enumerate(_REASONS))
    when = (f"Live counts, read on {escape(str(bd['computed_on']))} (UTC) across the "
            "latest balance sheet of each company." if counts and bd.get("computed_on")
            else "Each flag carries one of these reasons.")
    return f"""
  <section class="bsec" id="reasons">
    <div class="wrap">
      <div class="secno alert"><span>04</span> What a flag says</div>
      <h2 class="bigh" data-reveal>Every flag comes with its reason.</h2>
      <p class="sec-sub bsub">{when} <a href="/verification">Counts by reason</a>.</p>
      <ol class="rrows">{rows}</ol>
    </div>
  </section>"""


def _outcomes() -> str:
    """What changes for the reader, as four flat colour tiles (Petal's bento),
    each with a fragment of a real captured response as its proof."""
    from src.report.samples import CAPTURED

    tiles = (
        ("y", "Every figure arrives with its filing.",
         "Period end and filing date on each line, so a number can be traced to EDGAR.",
         '"filing_date": "2026-07-31"', "AAPL"),
        ("b", "A sheet that does not add up is labelled.",
         "Not smoothed, not dropped: the response says which check failed and why.",
         '"identity_status": "missing_tag"', "AACB"),
        ("c", "No lines, no verdict.",
         "A filing without the lines the check needs is answered not_testable.",
         "not_testable", ""),
        ("r", "Revisions carry their own date.",
         'A later filing\'s value is shown under that filing. '
         '<a href="/restatements">Revisions from the last 90 days</a>.',
         '"restated": false', "AAPL"),
    )
    body = "".join(
        f'<article class="otile o-{c} tcard" data-reveal style="--i:{i % 2}">'
        f'<h3>{escape(t)}</h3><p>{d}</p>'
        f'<code class="ochip">{escape(chip)}</code><span class="osrc">' + (f'{escape(tk)}, captured {CAPTURED}' if tk else 'a status the API returns') + '</span></article>'
        for i, (c, t, d, chip, tk) in enumerate(tiles))
    return f"""
  <section class="bsec" id="outcomes">
    <div class="wrap">
      <div class="secno"><span>06</span> What you get</div>
      <h2 class="bigh" data-reveal>Numbers you can <em>defend</em>.</h2>
      <div class="obento">{body}</div>
    </div>
  </section>"""


def _cta_mid(line: str) -> str:
    """A CTA between sections (the rhythm the references keep): one line, the
    key, and a person to talk to. Links only; the forms stay two."""
    return f"""
  <div class="ctamid"><div class="wrap ctamid-in" data-reveal>
    <p>{line}</p>
    <div class="ctamid-act"><a class="btn-key" href="#cta-email">Get free key</a>
      <a class="btn-talk" href="{_TALK_HREF}">Talk to us</a></div>
  </div></div>"""


def _how_lines(view: View1) -> str:
    """Step 1's visual: the filing's own lines, as read, with the filing they
    came from. Every label and value is the view's."""
    d = view.as_dict()

    def rows(blocks):
        return "".join(
            f'<li style="--i:{i}"><span>{escape(b.get("short_label") or b["label"])}</span>'
            f'<b>{money(b["value"])}</b></li>'
            for i, b in enumerate(blocks))
    return f"""
        <div class="hv-head"><span class="hv-tk">{escape(d["ticker"])}</span>
          quarter ended {escape(d["period_end"])}, filed {escape(d["filing_date"])}</div>
        <div class="hv-lines">
          <div><p class="hv-k">Owns</p><ol>{rows(d["assets"])}</ol></div>
          <div><p class="hv-k">Owed &amp; owned</p><ol>{rows(d["claims"])}</ol></div>
        </div>"""


def _how_bars(view: View1, *, flagged: bool = False) -> str:
    """Steps 2 and 3: the two totals as bars at their true proportion. For a
    flagged filing the shortfall is its own hatched segment, named, and a
    negative equity hangs below the baseline. Heights are flex ratios of the
    real values, so the drawing is exact without printing a percentage."""
    d = view.as_dict()
    total = d["total_assets"]
    above = [b for b in d["claims"] if not (b["kind"] == "equity" and b["value"] < 0)]
    below = [b for b in d["claims"] if b["kind"] == "equity" and b["value"] < 0]
    up = sum(b["value"] for b in above)
    scale = max(total, up)
    liab = sum(b["value"] for b in d["claims"] if b["kind"] != "equity")
    eq = sum(b["value"] for b in d["claims"] if b["kind"] == "equity")

    def seg(b):
        if b.get("key") == "unread_components":
            cls, lab = "gap", "Not read from this filing"
        elif b["kind"] == "equity":
            cls, lab = "e", "Equity"
        else:
            cls, lab = "l", b.get("short_label") or b["label"]
        return (f'<div class="hb-seg {cls}" style="flex:{b["value"]:.0f} 1 0">'
                f'<span>{escape(lab)} <b>{money(b["value"])}</b></span></div>')

    # Claims are drawn top-down in the order the filing gives them, as on the
    # company page; the gap sits where the missing lines would be.
    claims = "".join(seg(b) for b in above)
    pad_a = f'<div class="hb-pad" style="flex:{scale - total:.0f} 1 0"></div>' if scale > total else ""
    pad_c = f'<div class="hb-pad" style="flex:{scale - up:.0f} 1 0"></div>' if scale > up else ""
    neg = "".join(f'<div class="hb-neg"><span>Equity <b>{money(b["value"])}</b></span></div>' for b in below)
    verdict = (
        '<p class="hb-reason"><span class="hb-flag">flagged: a line item did not reach us</span>'
        " The filer's own stated total balances; the gap is what this dataset could not read.</p>"
        if flagged else
        '<p class="hb-reason"><span class="hb-ok">A = L + E</span> The two totals are the '
        "same height: the filing balances.</p>")
    name = escape(d["company_name"] or d["ticker"])
    return f"""
        <div class="hv-head"><span class="hv-tk">{escape(d["ticker"])}</span>
          quarter ended {escape(d["period_end"])}, filed {escape(d["filing_date"])}</div>
        <div class="hbars{' flag' if flagged else ''}" role="img"
          aria-label="{name}: total assets {money(total)}; liabilities {money(liab)} and equity {money(eq)}, drawn to scale">
          <div class="hb-col"><p class="hb-cap">Owns <b>{money(total)}</b></p>
            <div class="hb-bar a">{pad_a}<div class="hb-seg a" style="flex:{total:.0f} 1 0"><span>Total assets</span></div></div></div>
          <div class="hb-col"><p class="hb-cap">Owed &amp; owned <b>{money(liab)} + {money(eq)}</b></p>
            <div class="hb-bar c">{pad_c}{claims}</div>{neg}</div>
        </div>
        {verdict}"""


def _how_json() -> str:
    """Step 4's visual: a real captured response, one span per line so the
    motion layer can reveal it line by line. The text is complete here."""
    from src.report.samples import CAPTURED, FINTECH_SHORT

    return f"""
        <figure class="jcard">
          <figcaption><code>check_balance_sheet</code> &middot; AAPL &middot; real response,
            captured from production {CAPTURED}. Excerpt.</figcaption>
          <pre class="code" tabindex="0"><code>{_lines(FINTECH_SHORT)}</code></pre>
        </figure>"""


def _how_bold(ok_view: View1 | None = None, flag_view: View1 | None = None) -> str:
    """How the check works, told beside one pinned visual (desktop, motion on):
    the filing's lines, the two totals rising to the same height, a flagged
    filing's gap and its reason, then what comes back. Each step's visual
    follows its text in the HTML, so with no script, reduced motion or a
    phone it reads as four steps, each with its picture."""
    from src.company.stats import identity_breakdown

    try:
        bd = identity_breakdown() or {}
    except Exception:  # noqa: BLE001
        bd = {}
    checked = ((bd.get("reconciled") or 0) + (bd.get("flagged") or 0)
               if bd.get("reconciled") is not None else 0)
    tested = (f"All {fmt_int(checked)} companies with a complete balance sheet are "
              "checked against assets = liabilities + equity."
              if checked else "Every balance sheet that reports its totals is checked "
              "against assets = liabilities + equity.")
    steps = (
        ("read", "Read the filing",
         "Figures come from the SEC Financial Statement Data Sets and the XBRL "
         "frames API, as filed, each with its filing date. A filing repeats a figure "
         "by segment and entity; only the company-level fact is kept (JPMorgan "
         "reports total assets twenty-three times in one filing).",
         _how_lines(ok_view) if ok_view is not None else ""),
        ("test", "How a filing is checked",
         f"{tested} No figure is adjusted to make a sheet fit.",
         _how_bars(ok_view) if ok_view is not None else ""),
        ("flag", "What a flag means",
         "A reason, not an accusation: rounding, a line item that did not reach us, "
         "no stated total, or a filing that does not balance. "
         '<a href="/verification">Counts by reason</a>.',
         _how_bars(flag_view, flagged=True) if flag_view is not None else ""),
        ("back", "What you get back",
         "JSON with every line item, its period and its filing, over the API, the "
         "Python client or MCP. What the <code>restated</code> flag means is on "
         '<a href="/verification#restated">the verification page</a>.',
         _how_json()),
    )
    rail = "".join(
        f'<li><a href="#how-{k}" data-step="{k}"><span class="bn">{i + 1:02d}</span>{escape(t)}</a></li>'
        for i, (k, t, _c, _v) in enumerate(steps))
    body = "".join(
        f'<article class="hstep" id="how-{k}" data-step="{k}">'
        f'<span class="bn">{i + 1:02d}</span><h3>{escape(t)}</h3><p>{copy}</p></article>'
        + (f'<div class="hlayer" data-layer="{k}">{vis}</div>' if vis else "")
        for i, (k, t, copy, vis) in enumerate(steps))
    return f"""
  <section class="bsec" id="how">
    <div class="wrap">
      <div class="secno"><span>05</span> Method</div>
      <h2 class="bigh" data-reveal>How the check works</h2>
      <p class="sec-sub bsub">{_HERO_METHOD}</p>
      <ol class="hrail" aria-label="Steps">{rail}</ol>
      <div class="hstory">{body}
      </div>
    </div>
  </section>"""


def _demo_band(card: str = "") -> str:
    """The live demo, in a rounded card on a coloured band, with the request and
    response card, the ticker search and the API docs button."""
    inner = _demo_section().replace(
        "  </section>",
        f"""    <div class="demo-card">{card}</div>
    <div class="demo-more">
      <div class="hero-search">{search_form()}</div>
      <a class="btn ghost" href="/api">API docs</a>
    </div>
  </section>""", 1)
    return f"""
  <div class="cband c-y">
    <div class="wrap"><div class="secno"><span>07</span> Try it</div>
      <div class="bigcard democard">{inner}</div></div>
  </div>"""


def _trust() -> str:
    """What we can state about the data and about buying, and nothing else.
    No status page or security page exists, so neither is linked."""
    items = (
        ("What this doesn't do", "No predictions, no scores, no recommendations. "
         "Nothing estimated: where a company doesn't report something, the response "
         "says so rather than showing zero."),
        ("Where the figures come from", "The SEC Financial Statement Data Sets and "
         "the SEC's XBRL frames API. Every value is as filed, with the date of the "
         "filing it came from."),
        ("Enterprise", "Invoicing and a signed agreement, by email."),
        ("Keys", "An API key is stored as a hash and shown once. Regenerate it from "
         "the dashboard and the old one stops working."),
        ("Payments", "Paid plans go through Stripe; card details never reach this "
         "site. Paid plans have a 14-day refund window."),
        ("At the limit", "Calls past the monthly quota are refused with HTTP 429 "
         "until the 1st (UTC). There is no overage billing."),
    )
    lis = "".join(f'<li data-reveal style="--i:{i % 3}"><b>{escape(t, quote=False)}.</b> '
                  f"{escape(b, quote=False)}</li>"
                  for i, (t, b) in enumerate(items))
    return f"""
  <section class="bsec" id="trust">
    <div class="wrap">
      <div class="secno"><span>09</span> Trust</div>
      <h2 class="bigh" data-reveal>Before you depend on it</h2>
      <ul class="trustlist">{lis}</ul>
      <p class="method"><a href="/terms">Terms</a> &middot; <a href="/privacy">Privacy</a>
        &middot; <a href="/verification">How the check works</a> &middot;
        <a href="/methodology">Methodology</a></p>
    </div>
  </section>"""


def _closing_band(free_calls: int, ticker: str) -> str:
    return f"""
  <section class="band dark closing" id="start">
    <div class="bg" aria-hidden="true"><canvas class="bg-gl" data-tone="dark"></canvas></div>
    <div class="band-in wrap">
      <p class="kicker">Balance sheets that prove they balance.</p>
      <h2 class="bigh giant" data-reveal>{escape(_HEADLINE)}</h2>
      <p class="sec-sub">Take a company where you know the answer, call it, and
        compare the response with EDGAR. If the numbers you ship have to be right,
        start here. Free tier: {free_calls:,} calls a month, no card.</p>
      {_key_form("cta", ticker, button="Get free key")}
      <div class="hero-actions"><a class="btn-talk" href="{_TALK_HREF}">Talk to us</a></div>
      <p class="fineprint">By getting a key you accept the
        <a href="/terms">Terms</a> and <a href="/privacy">Privacy Policy</a>.</p>
    </div>
  </section>"""


def _inspector(json_text: str) -> str:
    """A captured response as rows of field, value and a tick: the same values,
    shown as a product panel rather than raw JSON. Nested objects are flattened
    to dotted names; nothing is reworded."""
    import json

    def flat(d, prefix=""):
        for k, v in d.items():
            if isinstance(v, dict):
                yield from flat(v, f"{prefix}{k}.")
            else:
                yield f"{prefix}{k}", json.dumps(v)

    rows = "".join(
        f'<li style="--i:{i}"><span class="ik">{escape(k)}</span>'
        f'<code class="iv">{escape(v)}</code><span class="it" aria-hidden="true"></span></li>'
        for i, (k, v) in enumerate(flat(json.loads(json_text)))
    )
    return f'<ul class="inspector">{rows}</ul>'

def _get_started() -> str:
    rows = (
        ("#demo", "Call the live demo", "No key: a real response in your browser."),
        ("/api", "Read the API docs", "Endpoints, plans and the response fields."),
        (f"/blog/{_REPORT_SLUG}", "Read the 995-filing report", "Method, charts and the data."),
    )
    lis = "".join(f'<li><a class="gsrow" href="{h}"><span><b>{escape(t)}</b>'
                  f'<small>{escape(s)}</small></span><span class="garrow" aria-hidden="true">&rarr;</span></a></li>'
                  for h, t, s in rows)
    return f"""
  <section class="bsec" id="get-started">
    <div class="wrap gs-grid">
      <div><div class="secno"><span>11</span> Get started</div>
        <h2 class="bigh" data-reveal>Get started easily.
          <span class="tone">Three ways in, none of them needs a card.</span></h2></div>
      <ul class="gslist">{lis}</ul>
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

    # The source attribution, in the company's voice: BalanceProof, not a
    # person (no name, no age, no "solo" -- the vault's voice rule). The
    # shared part, legal and contact links included, is added around it.
    footer = render_footer(
        'BalanceProof. Source: <a href="https://www.sec.gov/dera/data/'
        'financial-statement-data-sets" rel="noopener">SEC Financial Statement '
        'Data Sets</a>.'
        # No public link to /admin: the operator opens it by URL. The routes
        # themselves are unchanged and /admin.json and every action route
        # still require the admin secret.
    )
    disclaimer = render_disclaimer()

    # The first company that actually has something to draw becomes the hero.
    # Not simply pairs[0]: that one may have failed to build, and the fold is
    # the last place to show a gap.
    # The story leads with a filing that draws as several real line items
    # (_STORY_LEAD, in order) when one is among the drawable suggestions; a
    # bank like JPM draws as one large "Other" block, which teaches the shape
    # less well. Otherwise, the first drawable one, as before.
    drawn = [(sg, v) for sg, v in pairs if v is not None]
    hero_pair = next((p for t in _STORY_LEAD for p in drawn
                      if p[1].ticker == t and p[1].balances),
                     drawn[0] if drawn else None)
    hero_view = hero_pair[1] if hero_pair else None
    flagged_view = _memo("flagged", _flagged_example)
    # "How the check works" tells the check with JPM (a bank that balances)
    # beside the flagged filing; any other drawn filing that balances if JPM
    # is not among the suggestions.
    how_ok = next((v for _s, v in drawn if v.ticker == "JPM" and v.balances),
                  next((v for _s, v in drawn if v.balances), None))
    hero_bs = _hero_drawing(
        hero_view, hero_pair[0].kind if hero_pair else "",
        flagged_view,
    )
    sample_ticker, sample = _memo(
        "sample", lambda: _pick_sample(hero_view)
    )

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
    <div class="sec-head"><h2>Other filings, same check</h2></div>
    <p class="sec-sub">Every company is its own shape. A bank's
      <a href="/blog/why-bank-balance-sheets-are-different">inverts the one
      most people picture</a>.</p>
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

    # The order a first-time reader needs it in: the claim and the way in (hero),
    # a real request and response beside it, the strip as the evidence, one
    # real filing drawn, the live demo, three short steps, what it costs, and
    # the way in again. `#demo`, `#how` and `#pricing` keep that relative order
    # because tests/test_pricing_demo_recovery.py pins it. Read, never typed:
    # production runs its own FREE_TIER_MONTHLY_CALLS and the copy must follow
    # it.
    from src.config.settings import get_settings as _gs
    free_calls = _gs().free_tier_monthly_calls
    body = f"""
<!-- No nav link to the explanation. It is further down THIS page, and a nav
     item reads as a separate destination. -->
{nav}

<main class="lhome bold" id="main">
  <!-- Order: the hero (claim, the ask, a real response), the proof strip and
       ticker marquee, one real filing drawn beside a flagged one, built for,
       why pipelines break, how the check works, the live demo, pricing, trust,
       FAQ, and the ask again in the dark closing band. `#how < #demo <
       #pricing` (test_pricing_demo_recovery). -->
  {_climb()}
  {_bold_hero(free_calls, sample_ticker)}
  {_proof_bold(stats or {})}
  {_drawing_band(hero_bs, gallery)}
  {_built_for()}
  {_pipelines()}
  {_reasons()}
  {_cta_mid('Catch the filing that does not add up before your model does.')}
  {_how_bold(how_ok, flagged_view)}
  {_outcomes()}
  {_cta_mid(f'Free tier: {free_calls:,} calls a month. No card, no password.')}
  {_demo_band(_memo("card", lambda: _code_card(sample_ticker, sample)))}
  <div class="bsec"><div class="wrap">
  <div class="secno"><span>08</span> What it costs</div>
  {_pricing_summary(stats)}
  </div></div>
  {_trust()}
  <div class="bsec"><div class="wrap">
  <div class="secno"><span>10</span> Questions</div>
  {_plan_faq()}
{disclaimer}
  </div></div>
  {_get_started()}
  {_closing_band(free_calls, sample_ticker)}
  <div class="lfoot"><div class="wrap">
{footer}
  <p class="motion-pref"><button type="button" class="motion-toggle" id="motion-toggle"
    aria-pressed="false" hidden>Motion: off</button></p>
  </div></div>
</main>

<!-- In the body, not the shell: the shell is shared with /dashboard and the
     search pages, and none of those have a demo box to drive. home-motion.js
     only adds classes and CSS variables; it never writes a figure. -->
<script src="/static/nav.js?v={asset_version()}" defer></script>
<script src="/static/home.js?v={asset_version()}" defer></script>
<script src="/static/keyform.js?v={asset_version()}" defer></script>
<script src="/static/home-motion.js?v={asset_version()}" defer></script>
<!-- The scroll story. Vendored (no third-party request) and home only.
     home-story.js does nothing unless <html> has `motion`, and tears every
     trigger, tween and the smooth scroll down when the toggle turns it off. -->
<script src="/static/vendor/gsap.min.js?v={asset_version()}" defer></script>
<script src="/static/vendor/ScrollTrigger.min.js?v={asset_version()}" defer></script>
<script src="/static/vendor/lenis.min.js?v={asset_version()}" defer></script>
<script src="/static/home-story.js?v={asset_version()}" defer></script>
<script src="/static/home-bg.js?v={asset_version()}" defer></script>"""
    # The company count is read live rather than written down: this line once
    # said "1.7M data points" while /dataset, which counts the same table, said 1.8M.
    from src.report.schema import organization_ld, software_ld

    n_companies = companies_label()
    scale = (f" {n_companies} companies, free tier, no card." if n_companies
             else " Free tier, no card.")

    return _shell(
        "SEC EDGAR Balance Sheet API, Checked — BalanceProof",
        body,
        description=(
            "SEC EDGAR balance sheet API that checks filings "
            f"against A = L + E where it can and shows the reason when one fails.{scale}"
        ),
        canonical="/",
        ld=organization_ld() + software_ld(),
        film="none",
        theme="light",
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
  <a class="brand" href="/"><span class="dot"></span>BalanceProof</a>
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
        f"{query} — BalanceProof",
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
  <a class="brand" href="/"><span class="dot"></span>BalanceProof</a>
</div></nav>
<main class="wrap" id="main">
  <div class="empty">
    <h1>Search by ticker for now</h1>
    <p>Company names are not loaded on this instance, so “{escape(query)}”
      can only be read as a ticker symbol, and there is no company with that
      symbol.</p>
    {search_form()}
    <p class="tryline">These five are loaded:</p>
    <div class="sugg">{sugg}</div>
  </div>
</main>"""
    return _shell("Search by ticker — BalanceProof", body, noindex=True)


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
  <a class="brand" href="/"><span class="dot"></span>BalanceProof</a>
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
    return _shell("BalanceProof — search", body, noindex=True)
