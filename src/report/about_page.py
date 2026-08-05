"""/about — the essay that used to be on the front page.

The landing page's job is to get someone to a company in one tap. Everything
that explains the project competes with that, so it lives here and the home
page links to it with a single line of numbers.

Anyone who follows that link has already decided they are interested, which is
the right audience for four screens of detail.
"""

from __future__ import annotations

from html import escape

from src.report.home_page import fmt_int, plural, shell


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


def render_about(stats: dict | None = None) -> str:
    body = f"""
<nav><div class="wrap nav">
  <a class="brand" href="/"><span class="dot"></span>To&nbsp;Scale</a>
  <span class="spacer"></span>
  <a class="navlink" href="/">Search</a>
</div></nav>

<main class="wrap">
  <header class="abouthead">
    <h1 class="htitle">How this works</h1>
    <p class="hlede">Every US public company's balance sheet, drawn at true
      proportion, from what they filed with the SEC.</p>
  </header>
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
    return shell("How To Scale works", body)
