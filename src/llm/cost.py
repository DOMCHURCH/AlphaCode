"""Token and cost accounting. Alerts if a run exceeds the configured budget."""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from src.config.settings import get_settings
from src.llm.schemas import ModelUsage

log = structlog.get_logger(__name__)


@dataclass
class CostTracker:
    by_stage: dict[str, ModelUsage] = field(default_factory=dict)

    def record(self, stage: str, usage: ModelUsage) -> None:
        self.by_stage[stage] = self.by_stage.get(stage, ModelUsage()) + usage

    @property
    def total(self) -> ModelUsage:
        out = ModelUsage()
        for u in self.by_stage.values():
            out = out + u
        return out

    def check_budget(self) -> bool:
        """True if within budget. Logs an alert if not."""
        limit = get_settings().max_run_cost_usd
        total = self.total
        if total.cost_usd > limit:
            log.error(
                "cost_budget_exceeded",
                cost_usd=round(total.cost_usd, 4),
                limit_usd=limit,
                tokens_in=total.prompt_tokens,
                tokens_out=total.completion_tokens,
            )
            return False
        return True

    def summary(self) -> dict:
        total = self.total
        return {
            "tokens_in": total.prompt_tokens,
            "tokens_out": total.completion_tokens,
            "cost_usd": round(total.cost_usd, 4),
            "by_stage": {
                k: {
                    "tokens_in": v.prompt_tokens,
                    "tokens_out": v.completion_tokens,
                    "cost_usd": round(v.cost_usd, 4),
                }
                for k, v in self.by_stage.items()
            },
        }
