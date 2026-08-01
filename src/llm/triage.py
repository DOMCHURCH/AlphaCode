"""Stage 4 -- LLM triage. 100 -> 25. Four batched calls of 25 tickers each.

On a schema violation we retry once with the parse error appended, then fall
back to the deterministic Stage 3 rank for that batch. The model is an
enhancement to the funnel, never a single point of failure for it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog
from pydantic import ValidationError

from src.config.settings import get_settings
from src.ingest.base import gather_bounded
from src.llm import client as llm_client
from src.llm.prompts import RETRY_SUFFIX, TRIAGE_SYSTEM
from src.llm.schemas import ModelUsage, TriageVerdict

log = structlog.get_logger(__name__)

BATCH_SIZE = 25


@dataclass
class TriageResult:
    verdicts: pd.DataFrame  # ticker-indexed: llm_score, keep, why
    selected: list[str]
    usage: ModelUsage = field(default_factory=ModelUsage)
    fallback_batches: int = 0
    llm_calls: int = 0


def _chunk(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _run_batch(
    client,
    packets: list[dict[str, Any]],
    model: str,
    model_info: dict[str, Any] | None,
) -> tuple[list[TriageVerdict], ModelUsage, bool]:
    """One batch. Returns (verdicts, usage, used_fallback)."""
    user = json.dumps(packets, separators=(",", ":"))
    total_usage = ModelUsage()
    last_error = ""

    for attempt in range(2):
        system = TRIAGE_SYSTEM
        if attempt == 1:
            system = TRIAGE_SYSTEM + RETRY_SUFFIX.format(error=last_error)
        try:
            parsed, usage, _ = await llm_client.complete_json(
                client,
                model=model,
                system=system,
                user=user,
                model_info=model_info,
                max_tokens=3000,
                # Only the first attempt's system block is cacheable -- the
                # retry appends the parse error, which changes the prefix.
                cache_system_block=(attempt == 0),
            )
            total_usage = total_usage + usage
            rows = llm_client.coerce_list(parsed)
            verdicts = [TriageVerdict.model_validate(r) for r in rows]
            if not verdicts:
                raise ValueError("no verdicts returned")
            return verdicts, total_usage, False
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:500]
            log.warning(
                "triage_batch_invalid", attempt=attempt, error=last_error[:200]
            )
        except Exception as exc:  # noqa: BLE001 - API failure, still fall back
            last_error = str(exc)[:500]
            log.warning("triage_batch_failed", attempt=attempt, error=last_error[:200])
            break

    return [], total_usage, True


async def run_triage(
    packets: list[dict[str, Any]],
    stage3_scores: pd.DataFrame,
    *,
    take: int | None = None,
    model: str | None = None,
    model_info: dict[str, Any] | None = None,
    batch_size: int = BATCH_SIZE,
) -> TriageResult:
    s = get_settings()
    take = take or s.stage4_take
    model = model or s.llm_triage_model

    if not packets:
        return TriageResult(pd.DataFrame(), [], ModelUsage())

    batches = _chunk(packets, batch_size)
    async with llm_client.make_client(concurrency=4) as c:
        results = await gather_bounded(
            [_run_batch(c, b, model, model_info) for b in batches], 4
        )
        llm_calls = c.calls_made

    rows: list[dict[str, Any]] = []
    usage = ModelUsage()
    fallbacks = 0

    for batch, res in zip(batches, results, strict=True):
        if isinstance(res, Exception):
            fallbacks += 1
            rows.extend(_fallback_rows(batch, stage3_scores))
            continue
        verdicts, batch_usage, used_fallback = res
        usage = usage + batch_usage
        if used_fallback or not verdicts:
            fallbacks += 1
            rows.extend(_fallback_rows(batch, stage3_scores))
            continue

        by_ticker = {v.t: v for v in verdicts}
        for p in batch:
            v = by_ticker.get(p["t"])
            if v is None:
                # The model dropped a ticker. Fall back for that one name only.
                rows.append(_fallback_row(p["t"], stage3_scores))
                continue
            rows.append(
                {
                    "ticker": v.t,
                    "llm_triage_score": float(v.score),
                    "keep": bool(v.keep),
                    "why": v.why,
                    "source": "llm",
                }
            )

    df = pd.DataFrame(rows).drop_duplicates("ticker").set_index("ticker")
    # `keep` is the model's opinion; the hard cut is still ours. Sort keepers
    # first, then by score, so a model that keeps everything cannot widen the
    # funnel.
    df = df.sort_values(["keep", "llm_triage_score"], ascending=[False, False])
    selected = list(df.head(take).index)

    if fallbacks:
        log.warning("triage_used_deterministic_fallback", batches=fallbacks)
    log.info(
        "stage4_complete",
        entry=len(packets),
        exit=len(selected),
        cost_usd=round(usage.cost_usd, 4),
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
    )
    return TriageResult(df, selected, usage, fallbacks, llm_calls)


def _fallback_rows(
    batch: list[dict[str, Any]], stage3_scores: pd.DataFrame
) -> list[dict[str, Any]]:
    return [_fallback_row(p["t"], stage3_scores) for p in batch]


def _fallback_row(ticker: str, stage3_scores: pd.DataFrame) -> dict[str, Any]:
    """Deterministic Stage 3 rank, mapped onto the 0-100 triage scale."""
    score = 50.0
    if ticker in stage3_scores.index and "stage3_score" in stage3_scores.columns:
        col = stage3_scores["stage3_score"]
        pct = (col <= col.get(ticker, col.median())).mean()
        score = float(pct * 100.0)
    return {
        "ticker": ticker,
        "llm_triage_score": score,
        "keep": score >= 50,
        "why": "deterministic stage-3 rank (LLM unavailable)",
        "source": "fallback",
    }
