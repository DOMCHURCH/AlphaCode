"""What a company page says beyond its three drawings.

An introduction written from the filing, four peers, the last few filings, and
a JSON-LD node describing the company. All of it is BUILT AHEAD and stored in
`company_page_extras`; the page reads one row by primary key.

THE RULE THIS FILE OBEYS. Every sentence below is assembled from a figure that
is already in the database for that ticker. There is no template with a
company name dropped into it, and there is nothing that would read the same
for two different companies -- a page that says nothing specific is worse than
a page that says nothing, because it costs a reader the time to find that out.
Where a figure is missing the sentence that needed it is omitted, never
softened into a claim that survives without it.

Nothing here is authoritative. Every number is recoverable from `fundamentals`
and every filing from `filing_events`, so a stale or absent row costs a section
of the page and never a wrong figure.
"""

from __future__ import annotations

import datetime as dt
import json
from html import escape
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# The filings worth linking. An 8-K is an event, not a balance sheet, and this
# section sits under a drawing of one.
PERIODIC_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F")

PEER_COUNT = 4
FILING_COUNT = 3

SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data"


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def money(value: float | None) -> str:
    """A figure at the scale a reader holds in their head."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1e12:
        return f"{sign}${v / 1e12:.2f}T"
    if v >= 1e9:
        return f"{sign}${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{sign}${v / 1e6:.0f}M"
    return f"{sign}${v:,.0f}"


def _pct_change(now: float | None, before: float | None) -> tuple[float, str] | None:
    """(percent, direction) or None where there is nothing honest to say."""
    if now is None or not before:
        return None
    change = (now - before) / abs(before) * 100.0
    if abs(change) < 0.05:
        return 0.0, "held flat"
    return change, "grew" if change > 0 else "shrank"


def _article(word: str) -> str:
    """"a" or "an". Sector labels are a closed set of ordinary English words --
    no acronyms, no "a hour" cases -- so the vowel test is sufficient here and
    a word-list would be pretending to a precision this does not need."""
    return "an" if word[:1].lower() in "aeiou" else "a"


def _rank_phrase(n: int) -> str:
    """1 -> "the largest", 2 -> "the 2nd largest", 11 -> "the 11th largest"."""
    if n == 1:
        return "the largest"
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"the {n}{suffix} largest"


def _named_lines(components: list[dict[str, Any]], n: int = 2) -> list[dict[str, Any]]:
    """The biggest real lines, largest first.

    `is_remainder` entries are excluded: "Other assets" is the arithmetic left
    over once the named lines are taken out, so calling it the largest line
    would be reporting our own gap in coverage as a fact about the company.
    """
    real = [
        c for c in components
        if not c.get("is_remainder") and (c.get("value") or 0) > 0
    ]
    return sorted(real, key=lambda c: -(c.get("value") or 0))[:n]


# ---------------------------------------------------------------------------
# The introduction
# ---------------------------------------------------------------------------
def build_intro(
    *,
    ticker: str,
    company_name: str | None,
    sector: str | None,
    assets: float | None,
    liabilities: float | None,
    equity: float | None,
    prior_assets: float | None = None,
    prior_period_end: dt.date | None = None,
    period_end: dt.date | None = None,
    filing_date: dt.date | None = None,
    form: str | None = None,
    balances: bool = True,
    identity_basis: str = "",
    imbalance_pct: float | None = None,
    asset_lines: list[dict[str, Any]] | None = None,
    claim_lines: list[dict[str, Any]] | None = None,
    negative_equity: bool = False,
    missing_components: bool = False,
    sector_rank: int | None = None,
    sector_total: int | None = None,
) -> str:
    """The opening paragraph, assembled from this filing and the one before it.

    Built as a list of sentences, each of which is dropped whole when the
    figure behind it is absent. That is why there is no "N/A" anywhere in the
    output: a sentence either has its number or it does not exist.
    """
    name = company_name or ticker
    sentences: list[str] = []

    opening = f"{escape(name)} ({escape(ticker)}) files with the SEC"
    if sector:
        label = sector.lower()
        opening += f" as {_article(label)} {escape(label)} company"
    sentences.append(opening + ".")

    if assets is not None:
        line = f"Its most recent balance sheet puts total assets at {money(assets)}"
        if liabilities is not None and equity is not None:
            line += (
                f", against {money(liabilities)} of liabilities and "
                f"{money(equity)} of equity"
            )
        if period_end:
            line += f", as at {period_end.isoformat()}"
        sentences.append(line + ".")

    change = _pct_change(assets, prior_assets)
    if change is not None and prior_period_end:
        pct, direction = change
        if direction == "held flat":
            sentences.append(
                f"Total assets held flat against {prior_period_end.isoformat()}, "
                f"when they stood at {money(prior_assets)}."
            )
        else:
            sentences.append(
                f"That balance sheet {direction} {abs(pct):.1f}% against "
                f"{prior_period_end.isoformat()}, when total assets were "
                f"{money(prior_assets)}."
            )

    # How the balance sheet is funded. Two figures a reader can hold, and the
    # single number that most distinguishes a bank from a software company.
    if assets and liabilities is not None and equity is not None and assets > 0:
        liab_share = liabilities / assets * 100.0
        eq_share = equity / assets * 100.0
        line = (
            f"Liabilities account for {liab_share:.1f}% of the balance sheet "
            f"and equity {eq_share:.1f}%"
        )
        if equity > 0:
            line += f", about ${liabilities / equity:,.2f} of liabilities for every dollar of equity"
        sentences.append(line + ".")

    # What the company actually holds, in its own filed line items. This is the
    # sentence that differs most between two companies of the same size, which
    # is exactly why it earns its place.
    top_assets = _named_lines(asset_lines or [])
    if top_assets and assets:
        first = top_assets[0]
        line = (
            f"The largest single line on the asset side is "
            f"{escape(str(first['label']))} at {money(first['value'])}, "
            f"{first['value'] / assets * 100:.1f}% of total assets"
        )
        if len(top_assets) > 1:
            second = top_assets[1]
            line += (
                f", followed by {escape(str(second['label']))} at "
                f"{money(second['value'])}"
            )
        sentences.append(line + ".")

    # Liabilities only. Equity is a claim on the company in the accounting
    # sense and the drawing shows it in that column, but calling the owners'
    # stake "the largest claim against it" reads as a debt, which it is not.
    debts = _named_lines(
        [c for c in (claim_lines or []) if c.get("kind") == "liability"], n=1
    )
    if debts and assets:
        c = debts[0]
        sentences.append(
            f"Its largest single liability is {escape(str(c['label']))} at "
            f"{money(c['value'])}, {c['value'] / assets * 100:.1f}% of the "
            f"balance sheet."
        )

    # Where it sits among the companies we cover in its own sector. Derived
    # from the same read the peer list uses, so it costs nothing extra.
    if sector and sector_rank and sector_total and sector_total > 1:
        sentences.append(
            f"By total assets it is {_rank_phrase(sector_rank)} of the "
            f"{sector_total:,} {escape(sector.lower())} companies covered here."
        )

    if negative_equity:
        sentences.append(
            "Equity is negative, so the claims against this company exceed "
            "what it owns on the filed figures. That is a real shape, not an "
            "error, and it is drawn below the baseline rather than hidden."
        )

    if filing_date:
        line = (
            f"The figures come from {_article(str(form))} {escape(str(form))}"
            if form
            else "The figures come from a filing"
        )
        line += f" filed on {filing_date.isoformat()}"
        sentences.append(line + ".")

    if missing_components:
        sentences.append(
            "Some line items this filer reports are not broken out separately "
            "here; the totals are complete and the difference is shown as a "
            "remainder rather than distributed across the lines that are named."
        )

    sentences.append(_identity_sentence(balances, identity_basis, imbalance_pct))
    sentences.append(
        "Every figure above is as filed. Nothing is estimated, smoothed or "
        "restated, and where the filing and the identity disagree the "
        "disagreement is shown rather than closed."
    )

    body = " ".join(s for s in sentences if s)
    return f'<p class="company-intro">{body}</p>'


def _identity_sentence(
    balances: bool, basis: str, imbalance_pct: float | None
) -> str:
    """What the identity check found, in one sentence, naming the term used."""
    if balances and not basis:
        return (
            "Assets equal liabilities plus equity on the filed figures, so this "
            "balance sheet reconciles directly."
        )
    if balances and basis == "nci":
        return (
            "The filing reports the parent's equity and the noncontrolling "
            "interest as separate lines, so it reconciles once the "
            "noncontrolling interest is included — both figures as filed."
        )
    if balances and basis == "mezzanine":
        return (
            "The filing presents redeemable instruments between liabilities and "
            "equity, so it reconciles once that mezzanine block is included — "
            "every figure as filed."
        )
    if balances and basis == "nci+mezzanine":
        return (
            "The filing reports equity, a mezzanine block and a noncontrolling "
            "interest as three separate lines, so it reconciles once all three "
            "are included — every figure as filed."
        )
    drift = f" by {imbalance_pct:.1f}%" if imbalance_pct is not None else ""
    return (
        f"This filing does not reconcile{drift}: liabilities plus equity differ "
        "from total assets, and it is flagged rather than adjusted."
    )


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------
def build_peers_html(peers: list[dict[str, Any]]) -> str:
    """Four companies in the same sector, nearest in size.

    Nearest by assets rather than the four biggest, because the four biggest
    are the same four on every page in the sector and a list that never changes
    is a navigation element pretending to be a comparison.
    """
    if not peers:
        return ""
    items = "".join(
        f'<li><a href="/company/{escape(p["ticker"])}">'
        f'<span class="peer-ticker">{escape(p["ticker"])}</span>'
        f'<span class="peer-name">{escape(p.get("name") or p["ticker"])}</span>'
        f'<span class="peer-assets">{money(p.get("assets"))}</span>'
        f"</a></li>"
        for p in peers
    )
    return (
        '<section class="company-peers"><h2>Related companies</h2>'
        '<p class="section-note">The nearest in size that file in the same '
        "sector, each drawn to the same scale.</p>"
        f'<ul class="peer-list">{items}</ul></section>'
    )


def build_filings_html(ticker: str, filings: list[dict[str, Any]]) -> str:
    """The last few periodic filings, linked to the filing itself on EDGAR."""
    if not filings:
        return ""
    rows = []
    for f in filings:
        label = f"{f['form']} — {f['filing_date']}"
        url = f.get("url")
        cell = (
            f'<a href="{escape(url)}" rel="noopener nofollow">{escape(label)}</a>'
            if url
            else escape(label)
        )
        rows.append(f"<li>{cell}</li>")
    return (
        '<section class="company-filings"><h2>Recent filings</h2>'
        '<p class="section-note">Straight to the document on EDGAR — this site '
        "reads these, it does not host them.</p>"
        f'<ul class="filing-list">{"".join(rows)}</ul></section>'
    )


def filing_url(cik: str | None, accession: str | None, primary_doc: str | None) -> str | None:
    """The EDGAR URL for one filing, or None rather than a guess.

    Every part is required: without the CIK there is no directory, without the
    accession there is no filing, and without the primary document the link
    lands on an index rather than the filing. A link that 404s is worse than a
    line of plain text.
    """
    if not (cik and accession and primary_doc):
        return None
    return f"{SEC_ARCHIVE}/{cik.lstrip('0')}/{accession.replace('-', '')}/{primary_doc}"


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------
def build_company_ld(
    *,
    ticker: str,
    company_name: str | None,
    sector: str | None,
    origin: str,
    assets: float | None = None,
    period_end: dt.date | None = None,
) -> str:
    """An `Organization` node describing THIS company.

    Distinct from `schema.organization_ld()`, which describes the publisher of
    this site. This one says who the page is about, which is new information on
    every page rather than the same declaration six thousand times.

    `tickerSymbol` is the field that lets an engine tie the page to the entity
    it already knows about, and it is the reason this node is worth emitting at
    all. Fields with no value are omitted rather than sent empty.
    """
    node: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": company_name or ticker,
        "tickerSymbol": ticker,
        "url": f"{origin}/company/{ticker}",
        "identifier": {"@type": "PropertyValue", "propertyID": "ticker", "value": ticker},
    }
    if sector:
        node["industry"] = sector
    if assets is not None and period_end is not None:
        # A dated, sourced figure. Undated it would be a claim with no shelf
        # life, which is the kind of thing that ages into a wrong answer.
        node["subjectOf"] = {
            "@type": "Dataset",
            "name": f"{company_name or ticker} balance sheet, {period_end.isoformat()}",
            "variableMeasured": {
                "@type": "PropertyValue",
                "name": "Total assets",
                "value": assets,
                "unitCode": "USD",
                "observationDate": period_end.isoformat(),
            },
        }
    text = json.dumps(node, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>'
