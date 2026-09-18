"""One page per sector: every company in it, ranked, with its own identity split.

These exist because of a link problem, not a content problem. The sitemap lists
6,189 company pages and almost nothing on the site points AT them: the home page
links five, `/methodology` links none, and each company page links four peers.
A crawler that reaches a page only through the sitemap, with no internal path to
it, is a crawler being told the page does not matter -- which is the textbook
cause of "Crawled -- currently not indexed".

The sector chip on every company page becomes a link to one of these, so the
6,189 pages acquire a parent and each other. That is the whole point; the
content is what makes the parent worth indexing on its own.

And the content is real rather than a directory listing, because the identity
split is already computed per sector by `stats.identity_breakdown`. A hub that
said only "here are 988 companies" would be the thin page this is meant to fix.
"""

from __future__ import annotations

import datetime as dt
import re
from html import escape
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Companies whose sector the SIC map could not place. They are the ones MOST in
# need of an internal link -- an unclassified company is orphaned twice -- so
# they get a hub rather than being dropped. Named honestly: it is not a sector.
UNCLASSIFIED = "Unclassified"
UNCLASSIFIED_SLUG = "unclassified"

# Below this a hub is a list of two companies and a lot of chrome, which is the
# thin page the whole exercise is trying not to create.
MIN_COMPANIES = 5


def sector_slug(sector: str | None) -> str:
    """"Information Technology" -> "information-technology"."""
    if not sector or not sector.strip():
        return UNCLASSIFIED_SLUG
    return re.sub(r"[^a-z0-9]+", "-", sector.strip().lower()).strip("-")


def _fmt_money(v: float) -> str:
    """$1.2T / $5.0B / $12.3M, matching the company pages."""
    a = abs(v)
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= cut:
            return f"${v / cut:,.1f}{suffix}"
    return f"${v:,.0f}"


def load_sectors() -> list[dict[str, Any]]:
    """Every sector with enough companies to be worth a page, largest first.

    Returns `{sector, slug, companies, newest}` per entry. `newest` is the most
    recent filing date in that sector and becomes the sitemap `lastmod` -- a
    real date, like every other lastmod on this site, rather than today.
    """
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap

    try:
        with session_scope() as session:
            sector_of = dict(
                session.execute(select(SectorMap.ticker, SectorMap.sector)).all()
            )
            rows = session.execute(
                select(Fundamental.ticker, func.max(Fundamental.filing_date))
                .where(Fundamental.metric == "total_assets", Fundamental.value > 0)
                .group_by(Fundamental.ticker)
            ).all()
    except Exception as exc:  # noqa: BLE001 - a missing hub must not break a page
        log.warning("sector_load_failed", error=str(exc)[:200])
        return []

    grouped: dict[str, list[dt.date]] = {}
    for ticker, filed in rows:
        name = (sector_of.get(ticker) or "").strip() or UNCLASSIFIED
        if filed is not None:
            grouped.setdefault(name, []).append(filed)

    out = [
        {
            "sector": name,
            "slug": sector_slug(None if name == UNCLASSIFIED else name),
            "companies": len(dates),
            "newest": max(dates),
        }
        for name, dates in grouped.items()
        if len(dates) >= MIN_COMPANIES
    ]
    out.sort(key=lambda s: -s["companies"])
    return out


def build_sector(slug: str) -> dict[str, Any] | None:
    """Everything one hub page needs, or None when the slug names no sector."""
    from sqlalchemy import func, select

    from src.company.stats import identity_breakdown
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    wanted = slug.strip().lower()
    match = next((s for s in load_sectors() if s["slug"] == wanted), None)
    if match is None:
        return None
    sector = match["sector"]
    unclassified = sector == UNCLASSIFIED

    try:
        with session_scope() as session:
            sector_of = dict(
                session.execute(select(SectorMap.ticker, SectorMap.sector)).all()
            )
            latest = dict(
                session.execute(
                    select(Fundamental.ticker, func.max(Fundamental.period_end))
                    .where(
                        Fundamental.metric == "total_assets", Fundamental.value > 0
                    )
                    .group_by(Fundamental.ticker)
                ).all()
            )
            rows = session.execute(
                select(
                    Fundamental.ticker, Fundamental.period_end, Fundamental.value
                ).where(
                    Fundamental.metric == "total_assets", Fundamental.value > 0
                )
            ).all()
            # One row per (date, ticker), so the dict keeps the last name
            # seen per ticker -- which is what every other read path does.
            names = dict(
                session.execute(
                    select(UniverseSnapshot.ticker, UniverseSnapshot.name)
                ).all()
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("sector_build_failed", slug=slug, error=str(exc)[:200])
        return None

    companies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ticker, period_end, value in rows:
        if ticker in seen or latest.get(ticker) != period_end:
            continue
        mine = (sector_of.get(ticker) or "").strip()
        if (mine or UNCLASSIFIED) != sector:
            continue
        seen.add(ticker)
        companies.append({
            "ticker": ticker,
            "name": names.get(ticker) or ticker,
            "assets": float(value),
        })
    companies.sort(key=lambda c: -c["assets"])

    # The identity split for THIS sector, off the same walk that produces
    # /methodology. Not recomputed here: a second walk would be a second
    # definition of "reconciles".
    split: dict[str, int] = {}
    breakdown = identity_breakdown()
    if breakdown:
        key = "" if unclassified else sector
        split = (breakdown.get("by_sector") or {}).get(key, {})

    return {
        "sector": sector,
        "slug": match["slug"],
        "unclassified": unclassified,
        "companies": companies,
        "newest": match["newest"],
        "split": split,
    }


def _split_line(split: dict[str, int], sector: str) -> str:
    """One sentence of identity result, or nothing if it is not computed yet."""
    if not split:
        return ""
    reconciled = sum(
        split.get(k, 0) for k in ("balanced", "nci", "mezzanine", "nci+mezzanine")
    )
    flagged = sum(
        split.get(k, 0) for k in ("rounding", "missing_tag", "broken", "unexplained")
    )
    if not (reconciled or flagged):
        return ""
    ours = split.get("missing_tag", 0)
    theirs = split.get("broken", 0)
    tail = ""
    if ours:
        tail = (
            f" Of the {flagged:,} flagged, {ours:,} "
            f"{'is' if ours == 1 else 'are'} a component this site could not "
            "read &mdash; ours, not the filer's."
        )
    if theirs:
        tail += (
            f" {theirs:,} {'is a filing' if theirs == 1 else 'are filings'} whose "
            "own stated totals disagree with each other."
        )
    return (
        f"<p class=\"sec-sub\">Of the {reconciled + flagged:,} "
        f"{escape(sector.lower())} balance sheets with a testable identity, "
        f"<b>{reconciled:,}</b> reconcile against A = L + E and "
        f"<b>{flagged:,}</b> are flagged with a named reason. None are adjusted "
        f"to fit.{tail}</p>"
    )


def _table(companies: list[dict[str, Any]]) -> str:
    rows = "".join(
        f'<tr><td class="num">{i}</td>'
        f'<td><a href="/company/{escape(c["ticker"])}">{escape(c["ticker"])}</a></td>'
        f'<td>{escape(str(c["name"]))}</td>'
        f'<td class="num">{_fmt_money(c["assets"])}</td></tr>'
        for i, c in enumerate(companies, 1)
    )
    return f"""
    <div class="tablewrap">
      <table class="mtable sectortable">
        <thead><tr>
          <th scope="col">#</th><th scope="col">Ticker</th>
          <th scope="col">Company</th><th scope="col">Total assets</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def seo_title(sector: str) -> str:
    """Kept under the 60-character result-list budget, like every other page."""
    return f"{sector} Balance Sheets, Ranked by Assets"


def render_sector(data: dict[str, Any], *, nav: str = "", others: list[dict[str, Any]] | None = None) -> str:
    from src.report.home_page import shell
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld

    sector = data["sector"]
    companies = data["companies"]
    unclassified = data["unclassified"]

    lede = (
        "Every company on this site whose sector the SEC's own SIC mapping "
        "could not place. They are grouped here rather than hidden, because an "
        "unplaced company is still a filed balance sheet."
        if unclassified else
        f"Every {sector.lower()} company covered here, ranked by total assets. "
        "Each one is drawn to scale and checked against the accounting "
        "identity, and the link goes to the filing behind it."
    )

    nav_others = ""
    if others:
        items = "".join(
            f'<li><a href="/sector/{escape(o["slug"])}">{escape(o["sector"])}</a>'
            f' <span class="muted">{o["companies"]:,}</span></li>'
            for o in others if o["slug"] != data["slug"]
        )
        nav_others = f"""
  <section class="sec" id="other-sectors">
    <div class="sec-head"><h2>Every sector</h2></div>
    <ul class="notelist sectorlist">{items}</ul>
  </section>"""

    body = f"""{nav}
<main class="wrap post" id="main">
  <article>
    <header class="hero">
      <h1 class="htitle">{escape(sector)} balance sheets</h1>
      <p class="hlede">{escape(lede)}</p>
    </header>

    <section class="sec" id="what-we-found">
      <div class="sec-head"><h2>What the check found here</h2></div>
      {_split_line(data["split"], sector) or
       '<p class="sec-sub">The identity split for this sector is still being '
       'computed. It appears here once the walk completes.</p>'}
      <p class="sec-sub">The method is the same one used everywhere on this
        site and is written out on <a href="/methodology">how we verify</a>.
        Nothing on this page is estimated or restated.</p>
    </section>

    <section class="sec" id="companies">
      <div class="sec-head"><h2>{len(companies):,} companies</h2></div>
      <p class="sec-sub">Ranked by total assets as most recently filed. Figures
        are as reported to the SEC.</p>
      {_table(companies)}
    </section>
  </article>
{nav_others}

  <aside class="sec cta">
    <div class="sec-head"><h2>Query this sector</h2></div>
    <p class="sec-sub">Every figure on this page is available from the API,
      reconciled and flagged the same way. Free tier, no card.</p>
    <div class="api-cta">
      <a class="btn" href="/dashboard">Get a free API key</a>
      <a class="btn ghost" href="/api">API docs</a>
      <a class="btn ghost" href="/dataset">Full dataset</a>
    </div>
  </aside>

{render_footer()}
</main>"""

    return shell(
        seo_title(sector),
        body,
        description=(
            f"Every {sector.lower()} company's balance sheet, ranked by total "
            "assets and reconciled against Assets = Liabilities + Equity. "
            "As filed, with every exception named."
        ),
        canonical=f"/sector/{data['slug']}",
        ld=breadcrumb_ld([
            ("Home", "/"),
            (f"{sector} balance sheets", f"/sector/{data['slug']}"),
        ]) if breadcrumb_ld else "",
    )
