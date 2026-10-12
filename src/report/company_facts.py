"""The prose and the arithmetic on a company page, rendered live.

WHY THIS IS NOT IN `company_page_extras`. The stored introduction is built once
per filing and read back by primary key. When it was built from one quarter and
the drawing above it was built from another, the page contradicted itself --
the paragraph and the chart disagreed about Apple's total assets. A stored
sentence can only ever be as current as the last time somebody rebuilt it.

So everything here is computed from the SAME `View1` the drawing is drawn
from, on every render of a page whose figures changed, and held in a small
in-process cache keyed by the figures themselves. A new filing changes the
key; nothing needs rebuilding and nothing can be stale.

THE RULE `page_extras` OBEYS APPLIES HERE TOO. Every number below is read from
`fundamentals` for this ticker. There is no sentence that would read the same
for two companies, and where a figure is absent the sentence that needed it is
omitted rather than softened.
"""

from __future__ import annotations

import datetime as dt
import time
from html import escape
from typing import Any

import structlog

from src.report.page_extras import build_intro, money

log = structlog.get_logger(__name__)

# Quarters shown in the "does it balance" history. Eight is two years: enough
# to see whether a failure is a one-off or a habit, short enough to read.
HISTORY_PERIODS = 8

# Identity bases, named the way the drawing's own note names them.
_BASIS_TERM = {
    "nci": "noncontrolling interest",
    "mezzanine": "mezzanine equity",
    "nci+mezzanine": "noncontrolling interest + mezzanine equity",
}


def dollars(v: float | None) -> str:
    """The whole figure, to the dollar. The arithmetic line has to show that
    the two sides are equal, and "$352.6B" cannot show that."""
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.0f}"


def _pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.1f}%"


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------
def arithmetic_line(view: Any) -> str:
    """Assets minus (liabilities plus equity), written out, or "" if a term is
    missing. Names whatever extra term the identity needed, so the line is the
    check that was actually run and not a prettier one."""
    a, liab, eq = view.total_assets, view.total_liabilities, view.total_equity
    if a is None or liab is None or eq is None:
        return ""
    basis = getattr(view, "identity_basis", "") or ""
    terms = [("liabilities", liab), ("equity", eq)]
    nci = getattr(view, "minority_interest", None)
    mezz = getattr(view, "mezzanine", None)
    if basis in ("nci", "nci+mezzanine") and nci is not None:
        terms.append(("noncontrolling interest", nci))
    if basis in ("mezzanine", "nci+mezzanine") and mezz is not None:
        terms.append(("mezzanine equity", mezz))
    rhs = sum(v for _, v in terms)
    gap = a - rhs
    words = " + ".join(f"{name} {dollars(v)}" for name, v in terms)
    return (
        f"Assets {dollars(a)} − ({words}) = {dollars(gap)}"
    )


def _stated_note(view: Any) -> str:
    """When the line above is not zero, the filer's own total referees it."""
    line = arithmetic_line(view)
    stated = getattr(view, "stated_rhs", None)
    if not line or not stated or line.rstrip().endswith("= $0"):
        return ""
    verdict = (
        "equals its total assets, so the filing balances against its own "
        "arithmetic and the difference is what this site did not read"
        if stated_agrees(view)
        else "does not equal its total assets, so the disagreement is in the "
        "filing's own totals"
    )
    return (
        "<p class=\"section-note\">The filer's own stated total for "
        f"liabilities and equity is {dollars(stated)}; that {verdict}.</p>"
    )


def _derivation_note(view: Any) -> str:
    src = getattr(view, "liabilities_derived_from", None)
    if not src:
        return ""
    return (
        "<p class=\"section-note\">Total liabilities is not stated in this "
        f"filing; it is derived as {escape(src)}. That makes the line above "
        "true by construction, so it is arithmetic on the filer's own figures "
        "and not an independent check.</p>"
    )


def ratio_rows(view: Any) -> list[tuple[str, str, str]]:
    """(label, working, result) for each ratio the figures support."""
    a, liab, eq = view.total_assets, view.total_liabilities, view.total_equity
    rows: list[tuple[str, str, str]] = []
    if not a or a <= 0:
        return rows
    if liab is not None:
        rows.append((
            "Liabilities to assets",
            f"{money(liab)} ÷ {money(a)}", _pct(liab, a),
        ))
    if eq is not None:
        rows.append((
            "Equity to assets",
            f"{money(eq)} ÷ {money(a)}", _pct(eq, a),
        ))
    if liab is not None and eq is not None and eq > 0:
        rows.append((
            "Liabilities to equity",
            f"{money(liab)} ÷ {money(eq)}", f"{liab / eq:.2f}×",
        ))
    # Largest NAMED line: a remainder is our own arithmetic gap, not a line the
    # filer reported, and calling it the largest would pass that off as fact.
    named_assets = [
        b for b in view.assets if not b.is_remainder and b.value > 0
    ]
    if named_assets:
        top = max(named_assets, key=lambda b: b.value)
        rows.append((
            f"Largest asset line: {top.label}",
            f"{money(top.value)} ÷ {money(a)}", _pct(top.value, a),
        ))
    named_liabs = [
        b for b in view.claims
        if b.kind == "liability" and not b.is_remainder and b.value > 0
    ]
    if named_liabs:
        top = max(named_liabs, key=lambda b: b.value)
        rows.append((
            f"Largest liability line: {top.label}",
            f"{money(top.value)} ÷ {money(a)}", _pct(top.value, a),
        ))
    return rows


def stated_agrees(view: Any) -> bool:
    """Does the filer's own stated liabilities-plus-equity equal its assets?

    The same test `_check_identity` uses to decide the gap is on our side of
    the wire, at the same tolerance."""
    from src.company.view1 import IDENTITY_TOLERANCE

    stated, a = getattr(view, "stated_rhs", None), view.total_assets
    if not stated or not a:
        return False
    return abs(a - stated) / a <= IDENTITY_TOLERANCE


def balance_verdict(row: dict[str, Any]) -> str:
    """One period's identity result, in words that do not blame the filer."""
    if row["balances"]:
        basis = row.get("basis") or ""
        if not basis or basis == "as_filed":
            return "Balanced"
        return f"Balanced, with {_BASIS_TERM.get(basis, basis)}"
    pct = row.get("imbalance_pct")
    if row.get("stated_agrees") and pct is not None:
        # The filer's own total for liabilities plus equity equals its total
        # assets, so the filing balances against its own arithmetic. What falls
        # short is what this site read, and the row must not say otherwise.
        return f"Filer's own totals agree; we read {pct:.1f}% less than its stated total"
    if pct is None:
        return "Does not balance"
    return f"Does not balance (off by {pct:.1f}%)"


def history_html(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    body = "".join(
        "<tr>"
        f"<th scope=\"row\">{escape(r['period_end'])}</th>"
        f"<td>{escape(r['filing_date'])}</td>"  # latest filing carrying it
        f"<td>{money(r['assets'])}</td>"
        f"<td>{money(r['liabilities'])}</td>"
        f"<td>{money(r['equity'])}</td>"
        f"<td>{escape(balance_verdict(r))}</td>"
        "</tr>"
        for r in rows
    )
    balanced = sum(1 for r in rows if r["balances"])
    return (
        "<h3>Does it balance, quarter by quarter</h3>"
        f"<p class=\"section-note\">{balanced} of the last {len(rows)} "
        "balance sheets on file reconcile as this site reads them. Each row "
        "is judged by the same check as the drawing above, and “latest "
        "filing” is the newest filing that carried that period's "
        "figures, since later filings repeat earlier periods as comparatives."
        "</p>"
        "<div class=\"tscroll\"><table class=\"facts-history\"><caption class=\"vh\">Balance sheet "
        "totals and identity result by period</caption><thead><tr>"
        "<th scope=\"col\">Period end</th><th scope=\"col\">Latest filing</th>"
        "<th scope=\"col\">Assets</th><th scope=\"col\">Liabilities</th>"
        "<th scope=\"col\">Equity</th><th scope=\"col\">A = L + E</th>"
        f"</tr></thead><tbody>{body}</tbody></table></div>"
    )


def hubs_html(view: Any, sector_slug_value: str | None) -> str:
    links = []
    if view.sector and sector_slug_value:
        links.append(
            f'<li><a href="/sector/{escape(sector_slug_value)}">All '
            f"{escape(view.sector)} companies</a> — largest by total "
            "assets first, as most recently filed.</li>"
        )
    links.append(
        '<li><a href="/companies">Every company covered</a> — the '
        "directory this page belongs to.</li>"
    )
    links.append(
        '<li><a href="/methodology">How the check works</a> — what '
        "“balanced” means and what the exceptions are.</li>"
    )
    return f'<h3>Where to go from here</h3><ul class="notelist">{"".join(links)}</ul>'


def build_facts_html(
    view: Any,
    history: list[dict[str, Any]],
    sector_slug_value: str | None,
) -> str:
    """The "numbers behind the drawing" section."""
    parts = []
    line = arithmetic_line(view)
    if line:
        parts.append(
            "<h3>The check, written out</h3>"
            f'<p class="facts-arith"><code>{escape(line)}</code></p>'
            + _stated_note(view)
            + _derivation_note(view)
        )
    rows = ratio_rows(view)
    if rows:
        body = "".join(
            f"<tr><th scope=\"row\">{escape(label)}</th>"
            f"<td>{escape(working)}</td><td>{escape(result)}</td></tr>"
            for label, working, result in rows
        )
        parts.append(
            "<h3>Ratios</h3><div class=\"tscroll\"><table class=\"facts-ratios\"><caption "
            "class=\"vh\">Ratios computed from this balance sheet</caption>"
            f"<tbody>{body}</tbody></table></div>"
        )
    parts.append(history_html(history))
    parts.append(hubs_html(view, sector_slug_value))
    name = view.company_name or view.ticker
    return (
        '<section class="sec" id="facts">'
        f'<div class="sec-head"><h2>The numbers behind {escape(str(name))}'
        "’s balance sheet</h2></div>"
        + "".join(p for p in parts if p)
        + "</section>"
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _history_rows(view: Any, ticker: str) -> list[dict[str, Any]]:
    """The newest `HISTORY_PERIODS` balance sheets, each judged by build_view1.

    The newest period IS `view` -- reused, not rebuilt -- so the first row of
    the table and the drawing cannot disagree. Earlier periods go through the
    same function with `period_end` set.
    """
    from src.company.history import _periods
    from src.company.view1 import build_view1

    def row(v: Any) -> dict[str, Any]:
        return {
            "period_end": v.period_end.isoformat(),
            "filing_date": v.filing_date.isoformat(),
            "assets": v.total_assets,
            "liabilities": v.total_liabilities,
            "equity": v.total_equity,
            "balances": bool(v.balances),
            "basis": v.identity_basis or "",
            "imbalance_pct": v.imbalance_pct,
            "stated_agrees": stated_agrees(v),
        }

    rows = [row(view)]
    periods, _ = _periods(ticker, None)
    for p in periods:
        if len(rows) >= HISTORY_PERIODS:
            break
        if p >= view.period_end:
            continue
        # A near-duplicate of the newest (SEC rounds to month end) is skipped
        # by `_periods`; one that slips through is skipped here.
        if (view.period_end - p).days <= 10:
            continue
        older = build_view1(ticker, period_end=p)
        if older is not None:
            rows.append(row(older))
    return rows


def _form_for(ticker: str, filing_date: dt.date) -> str | None:
    from sqlalchemy import select

    from src.report.page_extras import PERIODIC_FORMS
    from src.storage.db import session_scope
    from src.storage.models import FilingEvent

    with session_scope() as session:
        return session.execute(
            select(FilingEvent.form)
            .where(FilingEvent.ticker == ticker)
            .where(FilingEvent.form.in_(PERIODIC_FORMS))
            .where(FilingEvent.filing_date == filing_date)
            .limit(1)
        ).scalar_one_or_none()


_CACHE: dict[tuple, tuple[float, tuple[str, str]]] = {}
_TTL_SECONDS = 3600.0
_MAX_ENTRIES = 4000


def clear_cache() -> None:
    _CACHE.clear()


def render_facts(view: Any) -> tuple[str, str] | None:
    """(intro_html, facts_html) for this view, or None if it could not be built.

    None means "keep whatever the page already had", never "show nothing": a
    failed read here must not take the paragraph off a page that has one.
    """
    key = (
        view.ticker, view.period_end, view.filing_date,
        round(view.total_assets or 0.0), round(view.total_liabilities or 0.0),
        round(view.total_equity or 0.0),
    )
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    try:
        from src.report.sector_page import sector_slug

        history = _history_rows(view, view.ticker)
        prior = history[1] if len(history) > 1 else None
        d = view.as_dict()
        intro = build_intro(
            ticker=view.ticker,
            company_name=view.company_name,
            sector=view.sector,
            asset_lines=d.get("assets") or [],
            claim_lines=d.get("claims") or [],
            negative_equity=bool(d.get("negative_equity")),
            missing_components=bool(d.get("missing_components")),
            assets=view.total_assets,
            liabilities=view.total_liabilities,
            equity=view.total_equity,
            prior_assets=prior["assets"] if prior else None,
            prior_period_end=(
                dt.date.fromisoformat(prior["period_end"]) if prior else None
            ),
            period_end=view.period_end,
            filing_date=view.filing_date,
            form=_form_for(view.ticker, view.filing_date),
            balances=view.balances,
            identity_basis=view.identity_basis,
            imbalance_pct=view.imbalance_pct,
            stated_rhs=view.stated_rhs,
        )
        facts = build_facts_html(
            view, history,
            sector_slug(view.sector) if view.sector else None,
        )
    except Exception as exc:  # noqa: BLE001 - decoration must never take the page
        log.warning("company_facts_failed", ticker=view.ticker, error=str(exc)[:200])
        return None
    if len(_CACHE) >= _MAX_ENTRIES:
        _CACHE.clear()
    _CACHE[key] = (now, (intro, facts))
    return intro, facts
