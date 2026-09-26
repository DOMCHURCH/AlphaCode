"""/restatements: what US companies revised in their SEC filings, updated daily.

Built from the exceptions feed (`src.company.exceptions`), so it says nothing
the API does not: the last 90 days of material restatements, and the filings
whose own totals disagree. A page that changes every time filings land is
also the kind search engines come back to, and it is the free window onto the
feed Pro and Business buy.
"""

from __future__ import annotations

import datetime as dt
from html import escape
from typing import Any

WINDOW_DAYS = 90
# Below this a "restatement" is mostly rounding in a re-presented comparative.
MATERIAL_PCT = 1.0
MAX_ROWS = 150

LABELS = {
    "total_assets": "Total assets", "total_liabilities": "Total liabilities",
    "total_equity": "Equity", "total_equity_incl_nci": "Equity incl. minority interest",
    "cash": "Cash", "current_assets": "Current assets",
    "current_liabilities": "Current liabilities", "long_term_debt": "Long-term debt",
    "revenue": "Revenue", "net_income": "Net income",
    "operating_income": "Operating income", "operating_cash_flow": "Operating cash flow",
    "eps_diluted": "Diluted EPS",
}


def _money(v: float | None, metric: str) -> str:
    if v is None:
        return "—"
    if metric == "eps_diluted":
        return f"${v:,.2f}"
    a, sign = abs(v), "-" if v < 0 else ""
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"{sign}${a / div:.3g}{unit}"
    return f"{sign}${a:,.0f}"


def _pct(v: float | None) -> str:
    return "" if v is None else f"{v:.2f}%"


def _names(tickers: set[str]) -> dict[str, str]:
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import UniverseSnapshot

    if not tickers:
        return {}
    try:
        with session_scope() as s:
            rows = s.execute(
                select(UniverseSnapshot.ticker, UniverseSnapshot.name)
                .where(UniverseSnapshot.ticker.in_(tickers))
                .order_by(UniverseSnapshot.as_of_date.asc())
            ).all()
    except Exception:  # noqa: BLE001 - names are a nicety
        return {}
    return {t: n for t, n in rows if n}


def select_events(events: list[dict[str, Any]], today: dt.date | None = None) -> tuple[list, list]:
    """(material restatements, filer-side failed checks) in the window, newest first."""
    since = ((today or dt.date.today()) - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    restated = [
        e for e in events
        if e["type"] == "restatement" and e["date"] >= since
        and e.get("change_pct") is not None and abs(e["change_pct"]) >= MATERIAL_PCT
    ]
    failed = [
        e for e in events
        if e["type"] == "failed_check" and e["date"] >= since
        and e.get("attribution") == "filer"
    ]
    return restated, failed


def render_restatements(*, nav: str = "") -> str:
    from src.company.exceptions import all_events
    from src.report.home_page import shell
    from src.report.nav import render_footer
    from src.report.schema import breadcrumb_ld

    try:
        events = all_events()
    except Exception:  # noqa: BLE001 - the page still explains itself
        events = []
    restated, failed = select_events(events)
    names = _names({e["ticker"] for e in restated[:MAX_ROWS] + failed})

    def company(t: str) -> str:
        n = names.get(t)
        label = f"{escape(n)} ({escape(t)})" if n else escape(t)
        return f'<a href="/company/{escape(t)}">{label}</a>'

    rows = "".join(
        f"<tr><td>{escape(e['date'])}</td><td>{company(e['ticker'])}</td>"
        f"<td>{escape(LABELS.get(e['metric'], e['metric']))}</td>"
        f"<td>{escape(e['period_end'])}</td>"
        f"<td>{_money(e['previous'], e['metric'])} &rarr; {_money(e['revised'], e['metric'])}</td>"
        f"<td>{e['change_pct']:+.1f}%</td></tr>"
        for e in restated[:MAX_ROWS]
    )
    fail_rows = "".join(
        f"<tr><td>{escape(e['date'])}</td><td>{company(e['ticker'])}</td>"
        f"<td>{escape(e['period_end'])}</td>"
        f"<td>{escape(e['meaning'])}</td>"
        f"<td>{_pct(e.get('drift_pct'))}</td></tr>"
        for e in failed
    )
    n_companies = len({e["ticker"] for e in restated})
    table = (
        f"""<div class="tablewrap"><table class="compare">
        <caption class="vh">Restatements in the last {WINDOW_DAYS} days</caption>
        <thead><tr><th scope="col">Filed</th><th scope="col">Company</th>
          <th scope="col">Figure</th><th scope="col">Period</th>
          <th scope="col">Was &rarr; now</th><th scope="col">Change</th></tr></thead>
        <tbody>{rows}</tbody></table></div>"""
        if rows else "<p class=\"plan-note\">None in the window yet.</p>"
    )
    fail_table = (
        f"""<div class="tablewrap"><table class="compare">
        <caption class="vh">Filings whose own totals disagree</caption>
        <thead><tr><th scope="col">Filed</th><th scope="col">Company</th>
          <th scope="col">Period</th><th scope="col">What</th>
          <th scope="col">Off by</th></tr></thead>
        <tbody>{fail_rows}</tbody></table></div>"""
        if fail_rows else "<p class=\"plan-note\">None in the window.</p>"
    )
    more = (f" Showing the newest {MAX_ROWS}." if len(restated) > MAX_ROWS else "")
    body = f"""{nav}
<main class="wrap" id="main">
  <header class="hero">
    <h1 class="htitle">Restatements in SEC Filings</h1>
    <p class="hlede">Figures US public companies revised in a later 10-K or
      10-Q, and filings whose own totals do not add up. Updated as filings are
      loaded, from SEC EDGAR.</p>
  </header>

  <section class="sec">
    <div class="sec-head"><h2>Revised in the last {WINDOW_DAYS} days</h2></div>
    <p class="sec-sub">{len(restated):,} figures at {n_companies:,} companies
      changed by {MATERIAL_PCT:.0f}% or more from what an earlier filing said
      for the same period.{more} "Was" is the figure as previously filed,
      "now" the revision; a backtest dated before the revision should use the
      first. <a href="/blog/point-in-time-fundamentals-sec-edgar">Why that
      matters</a>.</p>
    {table}
  </section>

  <section class="sec">
    <div class="sec-head"><h2>Filings that do not add up</h2></div>
    <p class="sec-sub">Balance sheets filed in the last {WINDOW_DAYS} days where
      the company's own totals disagree: total assets is not its stated total
      liabilities and equity. Gaps this dataset caused are not listed here.</p>
    {fail_table}
  </section>

  <section class="sec">
    <div class="sec-head"><h2>The whole feed</h2></div>
    <p class="sec-sub">Every restatement and failed check across every company,
      with the filing dates, is one API call or a CSV:
      <code>GET /api/exceptions</code> on the <a href="/pricing">Pro plan</a>
      (90 days) and Business (all of it). In Python,
      <code>pip install balanceproof</code> and
      <code>client.exceptions(type="restatement")</code>.
      <a href="/api">API documentation</a>.</p>
  </section>
  {render_footer("Source: SEC EDGAR XBRL filings, as filed.")}
</main>"""
    return shell(
        "Restatements in SEC Filings — BalanceProof",
        body,
        description=(
            f"{len(restated):,} figures at {n_companies:,} US companies restated in "
            f"SEC filings in the last {WINDOW_DAYS} days: what each first said, what "
            "it says now, and when it changed."
        ),
        canonical="/restatements",
        ld=breadcrumb_ld([("Home", "/"), ("Restatements", "/restatements")]),
    )
