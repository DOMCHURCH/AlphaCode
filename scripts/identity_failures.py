"""Why each filing that misses A = L + E misses it.

Run it against a populated database:

    python scripts/identity_failures.py            # table to stdout
    python scripts/identity_failures.py --json     # machine-readable

A = L + E is an identity, not a target. A correctly parsed, correctly tagged
filing satisfies it exactly, so every miss is one of two things: the filing is
not being read on the terms it was written on, or we failed to read it. "99.9%
accurate" is only an honest number if the remaining 0.1% can be named.

THE DISCRIMINATOR that makes this more than guesswork is
`liabilities_and_equity` -- the filer's OWN stated right-hand side. When a filer
publishes it, we get to compare three things rather than two:

    assets  vs  stated_rhs   -- does the FILING balance, on its own figures?
    stated_rhs  vs  L + E    -- did WE recover everything the filing put there?

If the filing balances against itself and our sum falls short, the missing
amount is a line we did not ingest and the fault is ours. If the filing does
not balance against its own stated total, that is the filer's arithmetic and no
parser can fix it. Without that tag the honest answer is "unexplained", and
this script says so rather than picking whichever bucket looks best.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any

# Gap below this share of assets is noise, not a finding: figures are reported
# in millions and a one-unit rounding difference on a $400bn balance sheet is
# eight decimal places of nothing.
ROUNDING_PCT = 1.0

# Same tolerance the drawing uses, so this script and the page agree about what
# "balances" means.
TOLERANCE_PCT = 0.5

# The outcome of every filing we can check, in report order.
#
# The first four are PASSES: `build_view1` draws all of them as balancing, and
# says on the drawing which term it had to add. They are broken out rather than
# summed because "balances outright" and "balances once you include the
# mezzanine" are different statements about a filing, and the whole point of
# this script is to be able to name the difference.
#
# The rest are failures.
PASS_CATEGORIES = (
    ("balanced", "Balances directly (A = L + E)", "—"),
    ("nci", "Reconciles once noncontrolling interests are included", "Filing structure"),
    ("mezzanine", "Reconciles once mezzanine equity is included", "Filing structure"),
    ("nci+mezzanine", "Reconciles once both are included", "Filing structure"),
)

FAIL_CATEGORIES = (
    ("rounding", "Rounding", "Neither"),
    ("missing_tag", "Missing XBRL tag in our ingest", "Ours"),
    ("broken", "Genuinely broken filing", "Company error"),
    ("unexplained", "Unexplained (no stated RHS to compare)", "Unknown"),
)

CATEGORIES = PASS_CATEGORIES + FAIL_CATEGORIES
_PASS_KEYS = frozenset(k for k, _, _ in PASS_CATEGORIES)

# Metrics that, if the filer published them, close a gap we cannot otherwise
# account for. When this script was written NONE of them were ingested, which
# was the finding rather than an oversight here: the bucket could never fill,
# so every mezzanine filer fell through to "rounding" or "unexplained".
#
# The first two are ingested now (src/ingest/xbrl.py), and the category is
# "Filing structure" rather than "Ours" to match: the drawing reads the block
# and the identity closes, so these stopped being failures at all.
#
# `redeemable_noncontrolling_interest` is still NOT ingested. It is left here
# deliberately -- a name in this tuple that no row can supply is exactly how
# the previous gap was found, and it will read as an empty contribution until
# somebody maps the tag.
MEZZANINE_METRICS = (
    "temporary_equity",
    "redeemable_preferred_stock",
    "redeemable_noncontrolling_interest",   # not ingested yet
)


def _metrics(session: Any, ticker: str, period_end: dt.date) -> dict[str, float]:
    from sqlalchemy import select

    from src.storage.models import Fundamental

    rows = session.execute(
        select(Fundamental.metric, Fundamental.value, Fundamental.filing_date)
        .where(Fundamental.ticker == ticker)
        .where(Fundamental.period_end == period_end)
    ).all()
    # Latest filing wins, matching every other read path in the codebase.
    best: dict[str, tuple[dt.date, float]] = {}
    for metric, value, filed in rows:
        if value is None:
            continue
        held = best.get(metric)
        if held is None or filed > held[0]:
            best[metric] = (filed, float(value))
    return {m: v for m, (_, v) in best.items()}


def classify(m: dict[str, float]) -> tuple[str, float]:
    """(category, drift_pct) for one period's metrics.

    The pass/fail half of this decision is delegated to
    `src.company.view1.resolve_identity` -- the same function `build_view1`
    uses to decide what the drawing says. It used to be reimplemented here, and
    the two copies disagreed: this script tried the NCI and the mezzanine
    separately and never together, and then counted a filing that closed on
    either as a FAILURE. The headline pass rate it printed was therefore the
    rate BEFORE the explanations, while the page quotes the rate after them.

    What stays here is the part the drawing has no opinion about: given that a
    filing does not balance on any basis, WHY not.
    """
    from src.company.view1 import resolve_identity

    assets = m.get("total_assets")
    liabilities = m.get("total_liabilities")
    equity = m.get("total_equity_incl_nci") or m.get("total_equity")
    if not assets or assets <= 0 or liabilities is None or equity is None:
        return "", 0.0

    # Both read exactly as `build_view1` reads them. The NCI is only a separate
    # line when the filer did NOT publish a combined equity total; with
    # `total_equity_incl_nci` in hand it is already inside `equity` and adding
    # it again would double-count. The mezzanine is read unconditionally,
    # because it sits outside permanent equity and no equity total absorbs it.
    nci = None if "total_equity_incl_nci" in m else m.get("minority_interest")
    mezzanine = _mezzanine(m)

    balances, drift, basis = resolve_identity(
        assets, liabilities, equity, nci, mezzanine
    )
    if balances:
        return (basis or "balanced"), drift

    # Past here the filing does not balance on any basis the filer published,
    # and the question is whose fault that is.

    # 1. Noise.
    if drift < ROUNDING_PCT:
        return "rounding", drift

    # 2/3. The filer's own stated right-hand side is the referee. If the filing
    #      balances against its OWN stated total and our sum falls short, the
    #      missing amount is a line we did not ingest. If it does not balance
    #      against its own total, that is the filer's arithmetic.
    stated = m.get("liabilities_and_equity")
    if stated:
        filing_itself = abs(assets - stated) / assets * 100.0
        if filing_itself > TOLERANCE_PCT:
            return "broken", drift
        return "missing_tag", drift

    return "unexplained", drift


def _mezzanine(m: dict[str, float]) -> float | None:
    """The mezzanine block, PREFERRED not summed -- as `view1._mezzanine` does.

    `temporary_equity` is the section total and the other two are components of
    it, so adding them would double-count a filer who tagged both.
    """
    for name in MEZZANINE_METRICS:
        value = m.get(name)
        if value:
            return value
    return None


def collect(as_of: dt.date | None = None) -> dict[str, Any]:
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    as_of = as_of or dt.date.today()
    out: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in CATEGORIES}
    checked = 0
    unclassifiable = 0

    with session_scope() as session:
        latest = session.execute(
            select(Fundamental.ticker, func.max(Fundamental.period_end))
            .where(Fundamental.period_end <= as_of)
            .group_by(Fundamental.ticker)
        ).all()

        for ticker, period_end in latest:
            m = _metrics(session, ticker, period_end)
            category, drift = classify(m)
            if not category:
                unclassifiable += 1
                continue
            checked += 1
            out[category].append({
                "ticker": ticker,
                "period_end": str(period_end),
                "drift_pct": round(drift, 2),
                "assets": m.get("total_assets"),
            })

    passes = sum(len(out[k]) for k, _, _ in PASS_CATEGORIES)
    failures = sum(len(out[k]) for k, _, _ in FAIL_CATEGORIES)
    return {
        "as_of": str(as_of),
        "checked": checked,
        # `checked` counts every ticker we could form an opinion about;
        # `unclassifiable` is the rest -- a missing assets, liabilities or
        # equity figure means there is no identity to test, and calling that a
        # pass or a failure would both be lies.
        "unclassifiable": unclassifiable,
        "passes": passes,
        "failures": failures,
        "pass_rate_pct": round(passes / checked * 100.0, 2) if checked else 0.0,
        "by_category": out,
        "counts": {k: len(out[k]) for k, _, _ in CATEGORIES},
    }


def _examples(rows: list[dict[str, Any]], n: int = 5) -> str:
    """The biggest few by assets -- big names are checkable by a reader."""
    top = sorted(rows, key=lambda r: -(r["assets"] or 0))[:n]
    return ", ".join(r["ticker"] for r in top) or "—"


def table(result: dict[str, Any]) -> str:
    checked = result["checked"] or 1
    failures = result["failures"] or 1
    lines = [
        "RECONCILES (what `build_view1` draws as balancing)",
        "",
        "| Category | Count | % of checked | Examples | Whose fault |",
        "|----------|-------|--------------|----------|-------------|",
    ]
    for key, label, fault in PASS_CATEGORIES:
        rows = result["by_category"][key]
        lines.append(
            f"| {label} | {len(rows):,} | {len(rows) / checked * 100:.2f}% "
            f"| {_examples(rows)} | {fault} |"
        )
    lines += [
        "",
        "DOES NOT RECONCILE",
        "",
        "| Category | Count | % of failures | Examples | Whose fault |",
        "|----------|-------|---------------|----------|-------------|",
    ]
    for key, label, fault in FAIL_CATEGORIES:
        rows = result["by_category"][key]
        lines.append(
            f"| {label} | {len(rows):,} | {len(rows) / failures * 100:.1f}% "
            f"| {_examples(rows)} | {fault} |"
        )
    lines += [
        "",
        f"Checked {result['checked']:,} companies "
        f"({result['unclassifiable']:,} had no testable identity and are excluded). "
        f"{result['passes']:,} reconcile, {result['failures']:,} do not "
        f"— a {result['pass_rate_pct']:.2f}% pass rate.",
        "",
        "This is the pass rate AFTER the explanations, which is what the page "
        "quotes: a filing that balances once its noncontrolling interest or its "
        "mezzanine block is included is a filing that balances, and the drawing "
        "says which term it added.",
    ]
    return "\n".join(lines)


def main() -> int:
    result = collect()
    if "--json" in sys.argv:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(table(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
