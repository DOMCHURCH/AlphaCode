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


def _headline_fact(view: View1) -> str:
    """The largest thing the company actually reports owning, as a share.

    Read off the drawing rather than composed, and named blocks only: the
    remainder is a leftover, not a line anyone filed, so "Loans 33%" is the
    honest headline for a bank even when the unbroken-out remainder is larger.
    A filer that breaks nothing out gets told so plainly instead.
    """
    d = view.as_dict()
    named = [b for b in d["assets"] if not b["is_remainder"]]
    if not named:
        rem = max(d["assets"], key=lambda b: b["pct"], default=None)
        return f"{rem['pct']:.0f}% not broken out" if rem else ""
    biggest = max(named, key=lambda b: b["pct"])
    return f"{escape(biggest['label'])} {biggest['pct']:.0f}%"


def _card(s: Suggestion, view: View1 | None) -> str:
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
        f'<span class="cfact">{_headline_fact(view)}</span>'
        f'<span class="csize">{money(d["total_assets"])} of assets</span>'
        "</a>"
    )


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
</head>
<body>
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
    <label class="slabel" for="q">Ticker</label>
    <div class="sfield">
      <input id="q" name="q" type="text" value="{escape(value)}"
        placeholder="JPM" autocomplete="off" autocapitalize="characters"
        spellcheck="false" maxlength="16" enterkeyhint="go"{af}>
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


def _fmt(n: int) -> str:
    return f"{n:,}"


def _plural(n: int, one: str, many: str) -> str:
    """These counts are read live, so "1 quarters" is a real state to reach --
    on a fresh database, or the first load after a wipe."""
    return one if n == 1 else many


def _numbers(stats: dict) -> str:
    """The scale of the data, counted live so the page cannot go stale.

    Any figure that cannot be counted right now is omitted rather than
    estimated -- which is the same rule the drawings follow.
    """
    if not stats or not stats.get("facts"):
        return ""

    rows = [
        (_fmt(stats["facts"]),
         _plural(stats["facts"], "as-reported fact", "as-reported facts"),
         "from SEC quarterly Financial Statement Data Sets"),
        (_fmt(stats["companies"]),
         _plural(stats["companies"], "company", "companies"),
         f"{_fmt(stats['drawable'])} with enough detail to draw"),
    ]
    if stats.get("quarters"):
        rows.append((
            _fmt(stats["quarters"]),
            _plural(stats["quarters"], "quarter", "quarters"),
            "every figure carrying its own filing date",
        ))

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
      <span>of the {_fmt(ident['checkable'])}
        {_plural(ident['checkable'], 'company', 'companies')} with a complete
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
    pairs: list[tuple[Suggestion, View1 | None]], stats: dict | None = None
) -> str:
    """`pairs` is (suggestion, its view or None), in the order to show them."""
    cards = "".join(_card(s, v) for s, v in pairs)

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

    body = f"""
<nav><div class="wrap nav">
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
  <span class="spacer"></span>
  <a class="navlink" href="/admin">Admin</a>
</div></nav>

<main class="wrap">
  <header class="hero">
    <h1 class="htitle">To Scale</h1>
    <p class="hlede">Every US public company's balance sheet, drawn at true
      proportion, from what they filed with the SEC.</p>
    {search_form()}
  </header>
  {gallery}
  {_example_figure()}
  {_numbers(stats or {})}
  {_hard()}
  {_limits()}

  <footer>
    Source: <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"
      rel="noopener">SEC Financial Statement Data Sets</a>. Built by Dominique
    Church — <a href="https://github.com/domchurch/alphacode"
      rel="noopener">source on GitHub</a>. This describes what a company
    reported; it is not advice and makes no prediction.
  </footer>
</main>"""
    return _shell("To Scale — filed financial statements, drawn to scale", body)


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
<main class="wrap">
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
