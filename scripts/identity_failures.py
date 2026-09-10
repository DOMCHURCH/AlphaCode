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

CATEGORIES = (
    ("nci", "Noncontrolling interests", "Filing structure"),
    ("mezzanine", "Mezzanine / redeemable preferred", "Filing structure"),
    ("rounding", "Rounding", "Neither"),
    ("missing_tag", "Missing XBRL tag in our ingest", "Ours"),
    ("broken", "Genuinely broken filing", "Company error"),
    ("unexplained", "Unexplained (no stated RHS to compare)", "Unknown"),
)

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
    """(category, drift_pct) for one period's metrics."""
    assets = m.get("total_assets")
    liabilities = m.get("total_liabilities")
    equity = m.get("total_equity_incl_nci") or m.get("total_equity")
    if not assets or assets <= 0 or liabilities is None or equity is None:
        return "", 0.0

    rhs = liabilities + equity
    drift = abs(assets - rhs) / assets * 100.0
    if drift <= TOLERANCE_PCT:
        return "", drift

    # 1. Does the noncontrolling interest close it? Only meaningful when the
    #    filer did not already publish a combined equity total.
    nci = m.get("minority_interest")
    if nci and "total_equity_incl_nci" not in m:
        with_nci = abs(assets - (rhs + nci)) / assets * 100.0
        if with_nci <= TOLERANCE_PCT:
            return "nci", drift

    # 2. Mezzanine, where the filer published one of the tags. Checked before
    #    rounding so a genuine structural cause is not written off as noise.
    for name in MEZZANINE_METRICS:
        value = m.get(name)
        if value:
            closed = abs(assets - (rhs + value)) / assets * 100.0
            if closed <= TOLERANCE_PCT:
                return "mezzanine", drift

    # 3. Noise.
    if drift < ROUNDING_PCT:
        return "rounding", drift

    # 4/5. The filer's own stated right-hand side is the referee.
    stated = m.get("liabilities_and_equity")
    if stated:
        filing_itself = abs(assets - stated) / assets * 100.0
        if filing_itself > TOLERANCE_PCT:
            return "broken", drift
        return "missing_tag", drift

    return "unexplained", drift


def collect(as_of: dt.date | None = None) -> dict[str, Any]:
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    as_of = as_of or dt.date.today()
    out: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in CATEGORIES}
    checked = 0

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
                checked += 1
                continue
            checked += 1
            out[category].append({
                "ticker": ticker,
                "period_end": str(period_end),
                "drift_pct": round(drift, 2),
                "assets": m.get("total_assets"),
            })

    failures = sum(len(v) for v in out.values())
    return {"checked": checked, "failures": failures, "by_category": out}


def table(result: dict[str, Any]) -> str:
    failures = result["failures"] or 1
    lines = [
        "| Category | Count | % of failures | Examples | Whose fault |",
        "|----------|-------|---------------|----------|-------------|",
    ]
    for key, label, fault in CATEGORIES:
        rows = result["by_category"][key]
        pct = len(rows) / failures * 100.0
        examples = ", ".join(
            r["ticker"] for r in sorted(rows, key=lambda r: -(r["assets"] or 0))[:3]
        ) or "—"
        lines.append(
            f"| {label} | {len(rows)} | {pct:.1f}% | {examples} | {fault} |"
        )
    checked = result["checked"]
    rate = (checked - result["failures"]) / checked * 100.0 if checked else 0.0
    lines.append("")
    lines.append(
        f"Checked {checked:,} companies. {result['failures']:,} miss the "
        f"identity — a {rate:.2f}% pass rate."
    )
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
