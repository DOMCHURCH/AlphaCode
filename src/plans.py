"""What each paid tier includes -- the one table every gate reads.

Written 2026-09-23 when Starter and Business joined Pro. Every feature check,
the /api docs, the pricing cards and the tests read from `MATRIX`, so the
numbers here are the product: change a cell and every surface moves with it.
Monthly call quotas are NOT here -- they are settings (`*_TIER_MONTHLY_CALLS`),
because operators tune those per deployment.

Enterprise is not a fifth tier string. `subscription_tier` is VARCHAR(8) and
"enterprise" does not fit; an Enterprise account is a Business account with
per-account overrides (`ApiUser.custom_monthly_calls` and friends) set by the
operator under a contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

TIER_ORDER: tuple[str, ...] = ("free", "starter", "pro", "business", "enterprise")
TIER_NAMES: dict[str, str] = {
    "free": "Free", "starter": "Starter", "pro": "Pro", "business": "Business",
    "enterprise": "Enterprise",
}

# None means "no limit beyond what the database holds".
MATRIX: dict[str, dict[str, Any]] = {
    # How far back /history reaches, in years.
    "history_years": {"free": 1, "starter": 5, "pro": 10, "business": None, "enterprise": None},
    # "What changed": period-over-period deltas and restatements.
    "changes": {"free": False, "starter": True, "pro": True, "business": True, "enterprise": True},
    # Companies an account can watch for new-filing alerts.
    "watchlist": {"free": 0, "starter": 3, "pro": 50, "business": 500, "enterprise": None},
    # Tickers per POST /api/verify call (each ticker is one metered call).
    "bulk_verify": {"free": 0, "starter": 0, "pro": 50, "business": 500, "enterprise": 5000},
    # Filing links (accession, form, SEC URL) on every figure returned.
    "provenance": {"free": False, "starter": False, "pro": False, "business": True, "enterprise": True},
    # HTTPS endpoints that receive signed alert POSTs.
    "webhooks": {"free": 0, "starter": 0, "pro": 0, "business": 5, "enterprise": 25},
    # `as_of=`: the figures exactly as they were public on a past date.
    "point_in_time": {"free": False, "starter": False, "pro": True, "business": True, "enterprise": True},
    # Days of the exceptions feed (failed checks + restatements, every company).
    "exceptions_feed": {"free": 0, "starter": 0, "pro": 90, "business": None, "enterprise": None},
}

FEATURE_LABELS: dict[str, str] = {
    "history_years": "balance sheet history",
    "changes": "what changed since the last filing",
    "watchlist": "watchlist and email alerts",
    "bulk_verify": "bulk verification",
    "provenance": "filing links on every figure",
    "webhooks": "webhooks",
    "point_in_time": "point-in-time as_of queries",
    "exceptions_feed": "the exceptions and restatements feed",
}


def allowance(tier: str, feature: str) -> Any:
    """The cell for this tier; an unknown tier reads as Free."""
    row = MATRIX[feature]
    return row.get(tier, row["free"])


def lowest_tier_with(feature: str) -> str:
    """The cheapest tier where the feature is on (truthy, or unlimited)."""
    row = MATRIX[feature]
    for tier in TIER_ORDER:
        value = row[tier]
        if value is None or value:
            return tier
    return "business"


def require(tier: str, feature: str) -> Any:
    """The allowance, or a 403 that names the plan which unlocks it.

    403 rather than 402: the caller is identified and simply not entitled,
    and every client library treats 402 as a curiosity.
    """
    value = allowance(tier, feature)
    if value is None or value:
        return value
    need = lowest_tier_with(feature)
    raise HTTPException(
        status_code=403,
        detail={
            "error": "plan_required",
            "feature": FEATURE_LABELS.get(feature, feature),
            "your_plan": TIER_NAMES.get(tier, "Free"),
            "required_plan": TIER_NAMES[need],
            "upgrade": "https://balanceproof.dev/pricing",
        },
    )


def public_matrix() -> list[dict[str, Any]]:
    """The matrix as rows, for /api.json and the pricing page."""
    return [
        {"feature": key, "label": FEATURE_LABELS[key],
         **{t: MATRIX[key][t] for t in TIER_ORDER}}
        for key in MATRIX
    ]
