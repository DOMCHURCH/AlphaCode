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


def fmt_int(n: int) -> str:
    return f"{n:,}"


def plural(n: int, one: str, many: str) -> str:
    """These counts are read live, so "1 companies" is a real state to reach --
    on a fresh database, or the first load after a wipe."""
    return one if n == 1 else many


def _compact(n: int) -> str:
    """1,237,331 -> "1.2M". The summary line is read at a glance; exact digits
    belong on /about, where there is room to say what they count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    return f"{n:,}"


def _summary_line(stats: dict) -> str:
    """One line of scale, and a way to the page that explains it.

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
        return '<p class="summary"><a href="/about">How this works</a></p>'
    return (
        f'<p class="summary">{escape(" · ".join(parts))}'
        f' <a href="/about">How this works</a></p>'
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
  <a class="navlink" href="/about">About</a>
</div></nav>

<main class="wrap">
  <header class="hero">
    <h1 class="htitle">To Scale</h1>
    <p class="hlede">Every US public company's balance sheet, drawn at true
      proportion, from what they filed with the SEC.</p>
    {search_form()}
  </header>
  {_summary_line(stats or {})}
  {gallery}

  <footer>
    Source: <a href="https://www.sec.gov/dera/data/financial-statement-data-sets"
      rel="noopener">SEC Financial Statement Data Sets</a>. Built by Dominique
    Church — <a href="https://github.com/domchurch/alphacode"
      rel="noopener">source on GitHub</a>.
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
