"""Score SEC filing activity and insider transactions.

The directional reads encoded here:

  Form 4 purchases (code P)    strong positive; cluster buys strongest of all
  Form 4 sales   (code S)      weak negative -- mostly 10b5-1, discount heavily
  8-K 1.01                     material agreement, positive with context
  8-K 2.02                     results, neutral -- PEAD already scores that
  8-K 5.02                     officer departure, negative if CFO
  S-1 / S-3 / 424B5            strong negative, dilution incoming
  SC 13D                       activist stake, strong positive
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

import structlog

from src.config.factor_weights import CATALYST_POINTS
from src.ingest.sec_edgar import DILUTION_FORMS, POSITIVE_FORMS

log = structlog.get_logger(__name__)

CLUSTER_WINDOW_DAYS = 30
CLUSTER_MIN_INSIDERS = 3

ROLE_POINTS = {
    "ceo": "insider_buy_ceo_cfo",
    "cfo": "insider_buy_ceo_cfo",
    "director": "insider_buy_director",
    "officer": "insider_buy_director",
    "other": "insider_buy_other",
}


def score_insiders(
    transactions: Iterable[dict[str, Any]], as_of: dt.date
) -> tuple[float, dict[str, Any]]:
    """Score insider activity for one ticker. Returns (points, detail)."""
    txns = list(transactions)
    if not txns:
        return 0.0, {"summary": "none", "buys": 0, "sells": 0}

    window_start = as_of - dt.timedelta(days=CLUSTER_WINDOW_DAYS)
    buys = [t for t in txns if (t.get("code") or "").upper() == "P"]
    sells = [t for t in txns if (t.get("code") or "").upper() == "S"]
    recent_buys = [t for t in buys if t.get("date") and t["date"] >= window_start]

    points = 0.0
    distinct = {t.get("person") for t in recent_buys if t.get("person")}
    is_cluster = len(distinct) >= CLUSTER_MIN_INSIDERS
    if is_cluster:
        # The single strongest insider signal in the literature.
        points += CATALYST_POINTS["insider_cluster_buy"]

    roles_seen: dict[str, int] = {}
    for t in recent_buys:
        role = (t.get("role") or "other").lower()
        roles_seen[role] = roles_seen.get(role, 0) + 1
        points += CATALYST_POINTS[ROLE_POINTS.get(role, "insider_buy_other")]

    # Sales are discounted hard -- scheduled 10b5-1 plans dominate the sample.
    points += CATALYST_POINTS["insider_sale"] * min(len(sells), 4)

    buy_value = sum(float(t.get("value_usd") or 0) for t in recent_buys)
    role_str = "+".join(
        f"{n}{r}" for r, n in sorted(roles_seen.items(), key=lambda kv: -kv[1])
    )
    summary = (
        f"{len(recent_buys)} buys/{CLUSTER_WINDOW_DAYS}d"
        + (f", {role_str}" if role_str else "")
        + (" [CLUSTER]" if is_cluster else "")
    ) if recent_buys else f"{len(sells)} sells, no buys"

    return points, {
        "summary": summary,
        "buys": len(recent_buys),
        "sells": len(sells),
        "distinct_buyers": len(distinct),
        "cluster": is_cluster,
        "buy_value_usd": buy_value,
        "roles": roles_seen,
    }


def _items(filing: dict[str, Any]) -> set[str]:
    raw = filing.get("items") or ""
    return {i.strip() for i in str(raw).split(",") if i.strip()}


def score_filings(
    filings: Iterable[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    """Score a ticker's recent filings. Returns (points, detail)."""
    fs = list(filings)
    if not fs:
        return 0.0, {"forms": [], "flags": []}

    points = 0.0
    flags: list[str] = []
    forms: list[str] = []

    for f in fs:
        form = (f.get("form") or "").upper()
        forms.append(form)
        items = _items(f)

        if form in DILUTION_FORMS:
            points += CATALYST_POINTS["shelf_or_secondary"]
            flags.append(f"{form} dilution risk")
            continue
        if form in POSITIVE_FORMS:
            points += CATALYST_POINTS["sc_13d"]
            flags.append("SC 13D activist stake")
            continue
        if form.startswith("8-K"):
            if any(i.startswith("1.01") for i in items):
                points += CATALYST_POINTS["form_8k_101"]
                flags.append("8-K 1.01 material agreement")
            if any(i.startswith("5.02") for i in items):
                # Only a CFO/CEO exit is reliably negative; other departures are
                # ambiguous and we score them as near-noise.
                desc = str(f.get("detail") or "").lower()
                if "financial" in desc or "cfo" in desc:
                    points += CATALYST_POINTS["form_8k_502_cfo"]
                    flags.append("8-K 5.02 CFO departure")
                else:
                    points += CATALYST_POINTS["form_8k_502_other"]
                    flags.append("8-K 5.02 officer change")
            # 8-K 2.02 (results) is deliberately worth zero -- PEAD scores it.

    unique_forms = sorted(set(forms))
    return points, {
        "forms": unique_forms,
        "flags": flags,
        "count": len(fs),
        "has_dilution_filing": any(f in DILUTION_FORMS for f in unique_forms),
    }


def score_institutional(delta_holders: float | None) -> tuple[float, dict[str, Any]]:
    """13F change in institutional ownership.

    Positive only when accumulating. Index funds move with the index and tell
    you nothing, so the caller is expected to pass a hedge-fund-only delta.
    """
    if delta_holders is None:
        return 0.0, {}
    pts = CATALYST_POINTS["institutional_accumulation"] if delta_holders > 0 else 0.0
    return pts, {"institutional_delta": delta_holders}
