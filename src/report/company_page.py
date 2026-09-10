"""Server-rendered HTML for /company/{ticker}.

Rendered on the server on purpose: the page is a drawing of numbers that are
already in the database, so it should arrive complete. Nothing to fetch, nothing
to hydrate, no half-loaded state.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from src.company.view1 import View1, describe_shape
from src.company.view2 import View2
from src.company.view3 import View3, describe_flow

_STATIC = Path(__file__).parent / "static"


def asset_version() -> str:
    """Cache-busting stamp, so a deployed CSS change actually reaches a phone."""
    try:
        return str(int(max(
            f.stat().st_mtime for f in _STATIC.iterdir() if f.is_file()
        )))
    except (OSError, ValueError):
        return "0"


_TONE_VARS = {
    "asset": ("--a0", "--a1", "--a2", "--a3", "--a4", "--a5", "--a6"),
    "liability": ("--l0", "--l1", "--l2", "--l3"),
}
# Tints this pale need ink labels, not white ones.
# Bands pale enough that a white label fails on them and an ink label is needed.
# --a3 and --l2 join the set for the dark palette, where every tint is lifted:
# white on #8299EF is 2.7:1, which is not a label, it is a rumour.
_PALE = {"--a3", "--a4", "--a5", "--a6", "--l2", "--l3"}


def _colour(block: dict[str, Any]) -> str:
    if block["kind"] == "equity":
        # Negative equity is drawn in ink with a yellow rule, not in the same
        # yellow as a healthy residual. It is a different fact and should not
        # read as a smaller version of the same thing.
        return "var(--band-neg)" if block["value"] < 0 else "var(--yellow)"
    fam = _TONE_VARS[block["kind"]]
    return f"var({fam[min(block['tone'], len(fam) - 1)]})"


def block_colour(block: dict[str, Any]) -> str:
    """The fill for one band. Shared with the home page, so a thumbnail and the
    full drawing of the same company are the same picture at two sizes."""
    return _colour(block)


def _is_pale(block: dict[str, Any]) -> bool:
    if block["kind"] == "equity":
        return block["value"] >= 0
    fam = _TONE_VARS[block["kind"]]
    return fam[min(block["tone"], len(fam) - 1)] in _PALE


def money(v: float | None) -> str:
    """Balance-sheet scale is unreadable as raw digits on a phone."""
    if v is None:
        return "—"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f}T"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.1f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.0f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.0f}K"
    return f"{sign}${a:,.0f}"


def _band(block: dict[str, Any], px_per_pct: float) -> str:
    height = max(block["pct"] * px_per_pct, 3.0)
    classes = ["band"]
    if block["is_remainder"]:
        classes.append("rem")
    if block["kind"] == "equity":
        classes.append("eq" if block["value"] >= 0 else "neg")
    if _is_pale(block):
        classes.append("pale")
    style = f"height:{height:.1f}px;background-color:{_colour(block)}"
    # A label must never be taller than its band. The band keeps its true
    # height -- scale fidelity is the whole point -- so the label adapts.
    if block["kind"] == "equity" and block["value"] < 0:
        # Negative equity is the most important thing on the page when it
        # exists, so it is labelled however thin the band. Short form: the value
        # must not be what gets ellipsised. The legend carries the full name.
        label = f'<span class="bl one">Equity <b>{money(block["value"])}</b></span>'
    elif block["label_style"] == "full":
        # The name gets its own line and is ellipsised there. Left to wrap, a
        # two-word name turns a two-line label into a three-line one and the
        # band clips it -- so the label the reader sees depends on how long the
        # word happens to be, which is not a thing the drawing should encode.
        label = (
            f'<span class="bl"><span class="bn">{escape(block["label"])}</span>'
            f'<span class="bv">{money(block["value"])}</span></span>'
        )
    elif block["label_style"] == "compact":
        label = (
            f'<span class="bl one">{escape(block["label"])} '
            f'<b>{money(block["value"])}</b></span>'
        )
    else:
        label = ""
    return f'<div class="{" ".join(classes)}" style="{style}">{label}</div>'


def _legend_rows(blocks: list[dict[str, Any]], total: float) -> str:
    out = []
    for b in blocks:
        sw = f'background-color:{_colour(b)}'
        cls = "sw rem" if b["is_remainder"] else "sw"
        share = abs(b["value"]) / total * 100.0 if total else 0.0
        note = f'<span class="ln">{escape(b["note"])}</span>' if b.get("note") else ""
        out.append(
            f'<tr class="lrow">'
            f'<td class="sw-cell" aria-hidden="true"><span class="{cls}" style="{sw}"></span></td>'
            f'<th scope="row" class="lk">{escape(b["label"])}{note}</th>'
            f'<td class="lv">{money(b["value"])}<small>{share:.0f}%</small></td>'
            f"</tr>"
        )
    return "".join(out)


def _flow_html(v3: View3 | None) -> str:
    """View 3: revenue in at the top, out through costs, profit at the bottom."""
    if v3 is None:
        return ""
    d = v3.as_dict()
    rev = d["revenue"]
    col_px = 260.0

    bands = []
    for i, st in enumerate(d["stages"]):
        h = max(st["pct"] / 100.0 * col_px, 3.0)
        tone = min(i, 3)
        lab = ""
        if h >= 26:
            lab = (f'<span class="bl">{escape(st["label"])}'
                   f'<span class="bv">{money(st["value"])} · {st["pct"]:.0f}%</span></span>')
        elif h >= 13:
            lab = (f'<span class="bl one">{escape(st["label"])} '
                   f'<b>{st["pct"]:.0f}%</b></span>')
        bands.append(
            f'<div class="band" style="height:{h:.1f}px;'
            f'background-color:var(--l{tone})">{lab}</div>'
        )

    keep = d["net_income"]
    keep_pct = abs(keep) / rev * 100.0
    keep_h = max(keep_pct / 100.0 * col_px, 14.0)
    keep_cls = "band keep" + (" loss" if d["loss_making"] else "")
    keep_lab = "Loss" if d["loss_making"] else "Profit kept"
    bands.append(
        f'<div class="{keep_cls}" style="height:{keep_h:.1f}px">'
        f'<span class="bl one">{keep_lab} <b>{money(keep)}</b> '
        f'· {d["margin_pct"]:.1f}%</span></div>'
    )

    rows = "".join(
        f'<tr class="lrow">'
        f'<td class="sw-cell" aria-hidden="true"><span class="sw" '
        f'style="background-color:var(--l{min(i, 3)})"></span></td>'
        f'<th scope="row" class="lk">{escape(st["label"])}'
        f'<span class="ln">{escape(st["caption"])}'
        + (" · computed as a remainder" if st["derived"] else "")
        + f'</span></th><td class="lv">{money(st["value"])}'
        f'<small>{st["pct"]:.0f}%</small></td></tr>'
        for i, st in enumerate(d["stages"])
    )

    basis = ("the last four quarters" if d["period_basis"] == "ttm"
             else "the year ended " + d["period_end"])
    notes = "".join(f'<div class="note">{escape(n)}</div>' for n in d["notes"])
    sentences = "".join(f"<p>{escape(s)}</p>" for s in describe_flow(v3))

    return f"""
  <section class="sec">
    <div class="sec-head"><h2>Where the money goes</h2></div>
    <p class="sec-sub">Every dollar of revenue over {escape(basis)}, and what
      is left after each cost comes out.</p>
    <div class="bs flow">
      <div class="bs-cap"><span>Revenue in</span><b>{money(rev)}</b></div>
      <div class="stack" role="img" aria-label="Where each dollar of revenue goes, drawn to scale — revenue {money(rev)}, {"loss" if d["loss_making"] else "profit kept"} {money(keep)}, margin {d["margin_pct"]:.1f}%. Line-by-line amounts in the table below.">{"".join(bands)}</div>
      <table class="legend"><caption class="vh">Revenue and costs, line by line</caption><tbody>{rows}</tbody></table>
    </div>
    {notes}
    <div class="shape">{sentences}</div>
  </section>"""


def _scale_html(v2: View2 | None) -> str:
    """View 2: the company against the economies nearest it in size."""
    if v2 is None:
        return ""
    d = v2.as_dict()
    rows = "".join(
        f'<div class="srow{" me" if r["kind"] == "company" else ""}">'
        f'<span class="sname">{escape(r["label"])}</span>'
        f'<span class="sbar"><i style="width:{max(r["pct_of_max"], 1.2):.2f}%"></i></span>'
        f'<span class="sval">{money(r["value"])}</span></div>'
        for r in d["rows"]
    )
    basis = ("last four quarters" if d["period_basis"] == "ttm"
             else "year ended " + d["period_end"])
    return f"""
  <section class="sec">
    <div class="sec-head"><h2>The size of it</h2></div>
    <p class="sec-sub">{escape(d["rank_note"])}</p>
    <div class="bs scale">
      <div class="scale-rows">{rows}</div>
    </div>
    <div class="caution">
      <strong>These measure different things.</strong> A country's GDP is
      everything it produced in a year. A company's revenue is what it sold.
      They share a unit, not a meaning — this shows how big the number is, and
      nothing more. A company is not "bigger than" a country.
    </div>
    <p class="prov">Company figure: {escape(basis)}. GDP:
      {escape(d["gdp_source"] or "World Bank")}, {d["gdp_year"]}.</p>
  </section>"""


def _ask_html(ticker: str, available: bool) -> str:
    """The question box. Absent entirely when the model is not configured.

    A disabled input with an apology is worse than nothing: it advertises a
    feature and then refuses, on a page whose whole claim is that what you see
    is what was filed.

    Progressive by construction -- everything above this point is already
    rendered and complete. If the script never arrives, or the model never
    answers, the reader has lost nothing but a convenience.
    """
    if not available:
        return ""
    from src.llm.ask import suggested_questions

    chips = "".join(
        f'<button type="button" class="qchip">{escape(q)}</button>'
        for q in suggested_questions()
    )
    return f"""
  <section class="sec ask" id="askbox" data-ticker="{escape(ticker)}">
    <div class="sec-head"><h2>Ask about these numbers</h2></div>
    <p class="sec-sub">Answered from the figures on this page and nothing else.
      Ask for something that is not here — another company, a share price, an
      earlier quarter — and it will tell you it is not in the filing data.</p>
    <form class="qform" id="qform">
      <label class="vh" for="qinput">Ask a question about these numbers</label>
      <div class="qfield">
        <input id="qinput" name="question" type="text" autocomplete="off"
          placeholder="What is the biggest thing it owns?" maxlength="300"
          enterkeyhint="send">
        <button type="submit" id="qsend">Ask</button>
      </div>
    </form>
    <div class="qchips">{chips}</div>
    <div class="qanswer" id="qanswer" role="status" aria-live="polite" hidden></div>
    <p class="qnote">This is a language model reading the same numbers you can
      see. It does not predict, rate or recommend.</p>
  </section>"""


def _company_meta(d: dict[str, Any]) -> str:
    """Description, canonical and social card for ONE company.

    These 6,000-odd pages are the site's entire long tail and they carried no
    description, no canonical and no card -- and a title that omitted the
    company name, which is the word somebody actually searches for. The
    description is built from the filing itself, so every page gets a
    different one rather than 6,000 copies of a template.
    """
    from src.report.home_page import SITE_ORIGIN
    from src.report.schema import breadcrumb_ld

    name = d["company_name"] or d["ticker"]
    total = d.get("total_assets")
    size = f" Total assets {money(total)}." if total else ""
    desc = (
        f"{name} ({d['ticker']}) balance sheet as filed with the SEC for the "
        f"period ended {d['period_end']}, drawn to scale.{size} As-reported "
        f"XBRL figures reconciled with the accounting identity."
    )
    url = f"{SITE_ORIGIN}/company/{d['ticker']}"
    crumbs = breadcrumb_ld(
        [("Home", "/"), (f"{name} ({d['ticker']})", f"/company/{d['ticker']}")]
    )
    tags = [
        f'<meta name="description" content="{escape(desc)}">',
        f'<link rel="canonical" href="{escape(url)}">',
        '<meta property="og:type" content="article">',
        '<meta property="og:site_name" content="To Scale">',
        f'<meta property="og:title" content="{escape(name)} ({escape(d["ticker"])}) balance sheet">',
        f'<meta property="og:description" content="{escape(desc)}">',
        f'<meta property="og:url" content="{escape(url)}">',
        f'<meta property="og:image" content="{SITE_ORIGIN}/static/media/backdrop.jpg">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{escape(name)} ({escape(d["ticker"])}) balance sheet">',
        f'<meta name="twitter:description" content="{escape(desc)}">',
        crumbs,
    ]
    return "\n".join(tags)


def render_company_page(
    view: View1,
    flow: View3 | None = None,
    scale: View2 | None = None,
    ask_available: bool = False,
) -> str:
    # The SAME backdrop every other page gets, from the one function that
    # owns it. This page renders its own <head> and <body> rather than
    # going through home_page._shell, and it used to carry a hand-copied
    # copy of the markup -- which is how it ended up as the only page on
    # the site still showing a still image after every other page moved
    # to the film: the copy had no id="backdrop" for the script to find,
    # and never loaded the script anyway.
    from src.report.backdrop import render_backdrop

    backdrop = render_backdrop()
    # Imported here, not at module level: nav imports legal, and legal
    # imports this module for asset_version -- a cycle at import time.
    from src.report.nav import render_footer

    d = view.as_dict()
    total = d["total_assets"]

    # One column is 100% of total assets. Fixed pixel height keeps both columns
    # on one scale and keeps the drawing above the fold on a phone.
    column_px = 300.0
    px_per_pct = column_px / 100.0

    assets_html = "".join(_band(b, px_per_pct) for b in d["assets"])

    # Claims: liabilities and positive equity stack above the baseline; negative
    # equity is drawn below it.
    above, below = [], []
    for b in d["claims"]:
        (below if (b["kind"] == "equity" and b["value"] < 0) else above).append(b)
    claims_html = "".join(_band(b, px_per_pct) for b in above)
    below_html = "".join(_band(b, px_per_pct) for b in below)

    liab_label = money(d["total_liabilities"]) if d["total_liabilities"] is not None else "—"
    eq_label = money(d["total_equity"]) if d["total_equity"] is not None else "—"

    missing_html = ""
    if d["missing_components"]:
        tags = "".join(f"<span>{escape(m)}</span>" for m in d["missing_components"])
        missing_html = (
            '<div class="missing"><h3>Not reported separately</h3>'
            "<p>This filer does not break these out. They are inside the totals, "
            "counted in the shaded remainder — not estimated, and not set to zero."
            f'</p><div class="tags">{tags}</div></div>'
        )

    # A filing that does not balance gets a WARNING, not a note. The site's
    # whole claim is that the two columns are the same money counted twice, so
    # when they are not, that has to read as an exception rather than as one
    # more grey line among the "this filer does not break X out" remarks.
    unbalanced = not d.get("balances", True)
    notes_html = "".join(
        f'<div class="note{" warn" if unbalanced and i == len(d["notes"]) - 1 else ""}">'
        f"{escape(n)}</div>"
        for i, n in enumerate(d["notes"])
    )

    shape_html = "".join(
        f"<p>{escape(s)}</p>" for s in describe_shape(view)
    )

    derived = ""
    if d["liabilities_derived_from"]:
        derived = (
            '<div class="derived">Total liabilities is not stated separately in '
            f'this filing. It is computed from {escape(d["liabilities_derived_from"])}'
            " — exact arithmetic from the filer's own figures, not an estimate.</div>"
        )

    simplified = ""
    if d["mode"] == "totals_only":
        simplified = (
            '<div class="note">This filer reports totals but no component '
            "breakdown, so the simplified view is shown. The proportions are "
            "still as filed.</div>"
        )

    # With a name, the ticker is an eyebrow above it. Without one, printing the
    # ticker twice -- once as the eyebrow, once as the heading -- reads as a
    # rendering bug rather than as missing data.
    has_name = bool((d["company_name"] or "").strip())
    name = escape(d["company_name"]) if has_name else escape(d["ticker"])
    eyebrow = (
        f'<p class="cticker">{escape(d["ticker"])}</p>' if has_name else ""
    )
    sector = (
        f'<span class="chip">{escape(d["sector"])}</span>' if d["sector"] else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(d["company_name"] or d["ticker"])} ({escape(d["ticker"])}) Balance Sheet — To Scale</title>
{_company_meta(d)}
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/company.css?v={asset_version()}">
<link rel="stylesheet" href="/static/dashboard.css?v={asset_version()}">
<link rel="stylesheet" href="/static/backdrop.css?v={asset_version()}">
<link rel="stylesheet" href="/static/dark.css?v={asset_version()}">
<link rel="stylesheet" href="/static/glass.css?v={asset_version()}">
<meta name="theme-color" content="#0a0a0a">
</head>
<body data-film="hero">
{backdrop}
<a class="skip" href="#main">Skip to content</a>
<nav><div class="wrap nav">
  <!-- The wordmark has always linked home, but nobody reads a wordmark as a
       control. The explicit back link is the difference between a way out and
       one you have to guess at. -->
  <a class="back" href="/"><span aria-hidden="true">←</span> Search</a>
  <span class="spacer"></span>
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
</div></nav>


<main class="wrap" id="main">
  <header class="chead">
    {eyebrow}
    <h1 class="cname">{name}</h1>
    <div class="cmeta">
      {sector}
      <span class="chip">Quarter ended {escape(d["period_end"])}</span>
      <span class="chip filed">Filed {escape(d["filing_date"])}</span>
    </div>
  </header>

  <section class="sec">
    <div class="sec-head"><h2>What it owns, and who has a claim on it</h2></div>
    <p class="sec-sub">Both columns are the same height because they are the same
      money, counted twice: once by what it is, once by who it belongs to.</p>

    <!-- The captions are their own grid row, shared by both columns, so the
         two stacks start at the same y no matter how either caption wraps.
         Nested inside the columns, "Owed & owned $4.06T + $362.4B" wraps where
         "Owns $4.42T" does not, and the right column silently drops half a
         line -- which breaks the one thing this drawing asserts. -->
    <div class="bs">
      <div class="bs-cols">
        <div class="bs-cap"><span>Owns</span><b>{money(total)}</b></div>
        <div class="bs-cap"><span>Owed &amp; owned</span><b>{liab_label} + {eq_label}</b></div>
        <div class="bs-col" role="img" aria-label="What it owns, drawn to scale — total assets {money(total)}. Line-by-line amounts in the assets table below.">
          <div class="stack">{assets_html}</div>
        </div>
        <div class="bs-col" role="img" aria-label="Who has a claim on it, drawn to scale — liabilities {liab_label}, equity {eq_label}. Line-by-line amounts in the claims table below.">
          <div class="stack">{claims_html}</div>
          {f'<div class="baseline"></div><div class="stack">{below_html}</div>' if below_html else ""}
        </div>
      </div>

      <table class="legend"><caption class="vh">Assets, line by line</caption>
        <tbody>{_legend_rows(d["assets"], total)}</tbody></table>
      <table class="legend"><caption class="vh">Liabilities and equity, line by line</caption>
        <tbody>{_legend_rows(d["claims"], total)}</tbody></table>
    </div>

    {derived}
    {simplified}
    {missing_html}
    {notes_html}
  </section>

  <section class="shape">{shape_html}</section>
  {_flow_html(flow)}
  {_scale_html(scale)}
  {_ask_html(d["ticker"], ask_available)}

{render_footer(
    f'Every figure is as reported to the SEC for the quarter ended '
    f'{escape(d["period_end"])}, filed {escape(d["filing_date"])}. Nothing is '
    f'estimated. This describes what a company reported; it is not advice and '
    f'makes no prediction.'
)}
</main>
{'<script src="/static/company.js?v=' + asset_version() + '" defer></script>'
 if ask_available else ''}
</body>
</html>"""



def render_not_found(ticker: str, reason: str) -> str:
    """Says what is missing, then offers tickers that are actually there.

    The alternatives are read out of the database, not hardcoded: sending a
    reader from one empty page to another is the one thing an empty state must
    not do.
    """
    # The SAME backdrop every other page gets, from the one function that
    # owns it. This page renders its own <head> and <body> rather than
    # going through home_page._shell, and it used to carry a hand-copied
    # copy of the markup -- which is how it ended up as the only page on
    # the site still showing a still image after every other page moved
    # to the film: the copy had no id="backdrop" for the script to find,
    # and never loaded the script anyway.
    from src.report.backdrop import render_backdrop

    backdrop = render_backdrop()
    from src.company.suggest import suggestions
    from src.report.home_page import search_form

    picks = suggestions()
    sugg = "".join(
        f'<a href="/company/{escape(s.ticker)}">{escape(s.ticker)}'
        + (f" <small>{escape(s.kind)}</small>" if s.kind else "")
        + "</a>"
        for s in picks
    )
    alternatives = (
        f'<p class="tryline">These are filed and complete, and each one looks '
        f"completely different from the others:</p>"
        f'<div class="sugg">{sugg}</div>'
        if picks
        else '<p class="tryline">No filed statements are loaded yet.</p>'
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(ticker)} — nothing to draw</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/company.css?v={asset_version()}">
<link rel="stylesheet" href="/static/dashboard.css?v={asset_version()}">
<meta name="robots" content="noindex,follow">
<link rel="stylesheet" href="/static/backdrop.css?v={asset_version()}">
<link rel="stylesheet" href="/static/dark.css?v={asset_version()}">
<link rel="stylesheet" href="/static/glass.css?v={asset_version()}">
<meta name="theme-color" content="#0a0a0a">
</head>
<body data-film="hero">
{backdrop}
<a class="skip" href="#main">Skip to content</a>
<nav><div class="wrap nav">
  <!-- The wordmark has always linked home, but nobody reads a wordmark as a
       control. The explicit back link is the difference between a way out and
       one you have to guess at. -->
  <a class="back" href="/"><span aria-hidden="true">←</span> Search</a>
  <span class="spacer"></span>
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
</div></nav>

<main class="wrap" id="main">
  <div class="empty">
    <h1>Nothing to draw for {escape(ticker)}</h1>
    <p>{escape(reason)}</p>
    {search_form()}
    {alternatives}
  </div>
</main>
</body>
</html>"""
