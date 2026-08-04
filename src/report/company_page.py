"""Server-rendered HTML for /company/{ticker}.

Rendered on the server on purpose: the page is a drawing of numbers that are
already in the database, so it should arrive complete. Nothing to fetch, nothing
to hydrate, no half-loaded state.
"""

from __future__ import annotations

from html import escape
from typing import Any

from src.company.view1 import View1, describe_shape

SUGGESTED = (
    ("JPM", "a bank"),
    ("AAL", "an airline"),
    ("MSFT", "a software company"),
    ("WMT", "a retailer"),
    ("FCX", "a miner"),
)

_TONE_VARS = {
    "asset": ("--a0", "--a1", "--a2", "--a3", "--a4", "--a5", "--a6"),
    "liability": ("--l0", "--l1", "--l2", "--l3"),
}
# Tints this pale need ink labels, not white ones.
_PALE = {"--a4", "--a5", "--a6", "--l3"}


def _colour(block: dict[str, Any]) -> str:
    if block["kind"] == "equity":
        # Negative equity is drawn in ink with a yellow rule, not in the same
        # yellow as a healthy residual. It is a different fact and should not
        # read as a smaller version of the same thing.
        return "var(--ink)" if block["value"] < 0 else "var(--yellow)"
    fam = _TONE_VARS[block["kind"]]
    return f"var({fam[min(block['tone'], len(fam) - 1)]})"


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
        label = (
            f'<span class="bl">{escape(block["label"])}'
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
            f'<div class="lrow"><span class="{cls}" style="{sw}"></span>'
            f'<span class="lk">{escape(b["label"])}{note}</span>'
            f'<span class="lv">{money(b["value"])}<small>{share:.0f}%</small></span></div>'
        )
    return "".join(out)


def render_company_page(view: View1) -> str:
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

    notes_html = "".join(
        f'<div class="note">{escape(n)}</div>' for n in d["notes"]
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

    name = escape(d["company_name"] or d["ticker"])
    sector = (
        f'<span class="chip">{escape(d["sector"])}</span>' if d["sector"] else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(d["ticker"])} — what it owns</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Jost:wght@400;600;700;800&family=Space+Mono:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/company.css">
</head>
<body>
<nav><div class="wrap nav">
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
  <span class="spacer"></span>
</div></nav>

<main class="wrap">
  <header class="chead">
    <p class="cticker">{escape(d["ticker"])}</p>
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

    <div class="bs">
      <div class="bs-cols">
        <div class="bs-col">
          <div class="bs-cap"><span>Owns</span><b>{money(total)}</b></div>
          <div class="stack">{assets_html}</div>
        </div>
        <div class="bs-col">
          <div class="bs-cap"><span>Owed &amp; owned</span><b>{liab_label} + {eq_label}</b></div>
          <div class="stack">{claims_html}</div>
          {f'<div class="baseline"></div><div class="stack">{below_html}</div>' if below_html else ""}
        </div>
      </div>

      <div class="legend">{_legend_rows(d["assets"], total)}</div>
      <div class="legend">{_legend_rows(d["claims"], total)}</div>
    </div>

    {derived}
    {simplified}
    {missing_html}
    {notes_html}
  </section>

  <section class="shape">{shape_html}</section>

  <footer>
    Every figure is as reported to the SEC for the quarter ended
    {escape(d["period_end"])}, filed {escape(d["filing_date"])}. Nothing is
    estimated. This describes what a company reported; it is not advice and
    makes no prediction.
  </footer>
</main>
</body>
</html>"""


def render_not_found(ticker: str, reason: str) -> str:
    sugg = "".join(
        f'<a href="/company/{t}">{t} <small>{d}</small></a>' for t, d in SUGGESTED
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
<link rel="stylesheet" href="/static/company.css">
</head>
<body>
<nav><div class="wrap nav">
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
</div></nav>
<main class="wrap">
  <div class="empty">
    <h1>Nothing to draw for {escape(ticker)}</h1>
    <p>{escape(reason)}</p>
    <p>These five are filed and complete, and each one looks completely
      different from the others:</p>
    <div class="sugg">{sugg}</div>
  </div>
</main>
</body>
</html>"""
