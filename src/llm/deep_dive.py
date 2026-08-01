"""Stage 5 -- LLM deep dive. 25 -> 10. One call per ticker.

Enforces the rubric: the model outputs each subscore separately and we compute
the total ourselves. `invalidation` must be concrete and checkable; a vague one
is a validation error and gets retried.

Final selection caps at 3 names per sector so the top-10 is not one sector bet
wearing ten tickers.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog
from pydantic import ValidationError

from src.config.settings import get_settings
from src.ingest.base import gather_bounded
from src.llm import client as llm_client
from src.llm.prompts import DEEP_DIVE_SYSTEM, RETRY_SUFFIX
from src.llm.schemas import DeepDive, ModelUsage

log = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class DeepDiveResult:
    dives: list[DeepDive]
    final: list[DeepDive]
    usage: ModelUsage = field(default_factory=ModelUsage)
    failures: dict[str, str] = field(default_factory=dict)
    llm_calls: int = 0


async def _one_dive(
    client,
    packet: dict[str, Any],
    model: str,
    model_info: dict[str, Any] | None,
) -> tuple[DeepDive | None, ModelUsage, str]:
    ticker = packet.get("ticker", "?")
    user = json.dumps(packet, separators=(",", ":"))
    usage = ModelUsage()
    last_error = ""

    for attempt in range(MAX_ATTEMPTS):
        system = DEEP_DIVE_SYSTEM
        if attempt:
            system = DEEP_DIVE_SYSTEM + RETRY_SUFFIX.format(error=last_error)
        try:
            parsed, u, _ = await llm_client.complete_json(
                client,
                model=model,
                system=system,
                user=user,
                model_info=model_info,
                max_tokens=2500,
                cache_system_block=(attempt == 0),
            )
            usage = usage + u
            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
            parsed.setdefault("ticker", ticker)
            return DeepDive.model_validate(parsed), usage, ""
        except ValidationError as exc:
            # The most common cause is a vague `invalidation`. The retry
            # message names it explicitly, which fixes it most of the time.
            last_error = _summarise_validation(exc)
            log.warning(
                "deep_dive_invalid", ticker=ticker, attempt=attempt,
                error=last_error[:200],
            )
        except ValueError as exc:
            last_error = str(exc)[:400]
            log.warning("deep_dive_unparseable", ticker=ticker, attempt=attempt)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)[:400]
            log.warning("deep_dive_failed", ticker=ticker, error=last_error[:200])
            break

    return None, usage, last_error


def _summarise_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}")
    return "; ".join(parts)


async def run_deep_dive(
    packets: list[dict[str, Any]],
    sectors: dict[str, str],
    *,
    take: int | None = None,
    max_per_sector: int | None = None,
    model: str | None = None,
    model_info: dict[str, Any] | None = None,
    concurrency: int = 5,
) -> DeepDiveResult:
    s = get_settings()
    take = take or s.stage5_take
    max_per_sector = max_per_sector or s.max_per_sector
    model = model or s.llm_deep_model

    if not packets:
        return DeepDiveResult([], [], ModelUsage())

    async with llm_client.make_client(concurrency=concurrency) as c:
        results = await gather_bounded(
            [_one_dive(c, p, model, model_info) for p in packets], concurrency
        )
        llm_calls = c.calls_made

    dives: list[DeepDive] = []
    usage = ModelUsage()
    failures: dict[str, str] = {}

    for packet, res in zip(packets, results, strict=True):
        ticker = packet.get("ticker", "?")
        if isinstance(res, Exception):
            failures[ticker] = str(res)[:300]
            continue
        dive, u, err = res
        usage = usage + u
        if dive is None:
            failures[ticker] = err or "unknown"
            continue
        dives.append(dive)

    final = select_final(dives, sectors, take=take, max_per_sector=max_per_sector)

    log.info(
        "stage5_complete",
        entry=len(packets),
        scored=len(dives),
        final=len(final),
        failed=len(failures),
        cost_usd=round(usage.cost_usd, 4),
    )
    return DeepDiveResult(dives, final, usage, failures, llm_calls)


def select_final(
    dives: list[DeepDive],
    sectors: dict[str, str],
    *,
    take: int,
    max_per_sector: int,
) -> list[DeepDive]:
    """Rank by total_score, take `take`, at most `max_per_sector` per sector."""
    ranked = sorted(dives, key=lambda d: d.total_score, reverse=True)
    counts: dict[str, int] = defaultdict(int)
    out: list[DeepDive] = []
    overflow: list[DeepDive] = []

    for d in ranked:
        sec = sectors.get(d.ticker) or "Unknown"
        if counts[sec] >= max_per_sector:
            overflow.append(d)
            continue
        out.append(d)
        counts[sec] += 1
        if len(out) == take:
            return out

    # If the sector cap left us short of `take`, we ship fewer names rather
    # than backfilling from the overflow -- the cap exists precisely to stop a
    # concentrated list, and relaxing it under pressure defeats it.
    if overflow and len(out) < take:
        log.info(
            "final_list_short_of_target_due_to_sector_cap",
            selected=len(out),
            target=take,
            held_back=len(overflow),
        )
    return out


def dives_to_frame(dives: list[DeepDive]) -> pd.DataFrame:
    if not dives:
        return pd.DataFrame()
    rows = []
    for d in dives:
        rows.append(
            {
                "ticker": d.ticker,
                "total_score": d.total_score,
                **{f"sub_{k}": v for k, v in d.subscores.model_dump().items()},
                "conviction": d.conviction,
                "time_horizon_days": d.time_horizon_days,
                "thesis": d.thesis,
                "bull_case": d.bull_case,
                "bear_case": d.bear_case,
                "invalidation": d.invalidation,
            }
        )
    return pd.DataFrame(rows).set_index("ticker")
