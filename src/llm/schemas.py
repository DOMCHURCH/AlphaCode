"""Pydantic v2 schemas for every LLM output. Nothing unvalidated gets through."""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from src.config.factor_weights import RUBRIC_MAX


class TriageVerdict(BaseModel):
    """Stage 4 output, one per ticker."""

    t: str = Field(description="ticker")
    score: int = Field(ge=0, le=100)
    keep: bool
    why: str

    @field_validator("why", mode="before")
    @classmethod
    def _cap_words(cls, v: str) -> str:
        """Truncate rather than reject. A model that ignores the 20-word cap
        has still produced a usable verdict; failing the batch over it would
        cost far more than the extra words."""
        if not isinstance(v, str):
            return v
        return " ".join(v.split()[:20])[:200]

    @field_validator("t")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()


class TriageBatch(BaseModel):
    verdicts: list[TriageVerdict]


class Subscores(BaseModel):
    trend: int = Field(ge=0, le=RUBRIC_MAX["trend"])
    fundamental: int = Field(ge=0, le=RUBRIC_MAX["fundamental"])
    catalyst: int = Field(ge=0, le=RUBRIC_MAX["catalyst"])
    news: int = Field(ge=0, le=RUBRIC_MAX["news"])
    macro: int = Field(ge=0, le=RUBRIC_MAX["macro"])
    risk: int = Field(ge=0, le=RUBRIC_MAX["risk"])

    def total(self) -> int:
        return (
            self.trend
            + self.fundamental
            + self.catalyst
            + self.news
            + self.macro
            + self.risk
        )


class CatalystAhead(BaseModel):
    event: str
    date: str = ""

    @field_validator("date")
    @classmethod
    def _iso_or_blank(cls, v: str) -> str:
        if not v:
            return ""
        try:
            dt.date.fromisoformat(v[:10])
        except ValueError:
            return ""
        return v[:10]


# An invalidation is only useful if it can actually be checked tomorrow. These
# are the shapes that qualify: a price level, a named metric with a direction,
# or a dated event.
_INVALIDATION_SIGNALS = (
    r"\$\s?\d",  # a dollar price level
    r"\d+(\.\d+)?\s*%",  # a percentage threshold
    r"\b(below|above|under|over|breaks?|falls?|drops?|closes?|exceeds?)\b",
    r"\b(sma|ema|ma)\s?\d+",  # a moving-average level
    r"\d{4}-\d{2}-\d{2}",  # a date
)

_VAGUE_PHRASES = (
    "if the thesis breaks",
    "if fundamentals deteriorate",
    "if sentiment turns",
    "if the story changes",
    "if things go wrong",
    "general market weakness",
    "if momentum fades",
)


class DeepDive(BaseModel):
    """Stage 5 output. The `invalidation` field is mandatory and must be
    concrete -- a thesis without a falsification condition is a story, not an
    analysis."""

    ticker: str
    total_score: int = Field(ge=0, le=100)
    subscores: Subscores
    thesis: str = Field(min_length=80)
    bull_case: str = Field(min_length=20)
    bear_case: str = Field(min_length=20)
    invalidation: str = Field(min_length=15)
    time_horizon_days: int = Field(ge=1, le=730)
    conviction: Literal["high", "medium", "low"]
    key_risks: list[str] = Field(default_factory=list)
    catalysts_ahead: list[CatalystAhead] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("invalidation")
    @classmethod
    def _must_be_checkable(cls, v: str) -> str:
        low = v.lower().strip()
        if any(p in low for p in _VAGUE_PHRASES):
            raise ValueError(
                "invalidation is vague; state a specific price level, a named "
                "metric with a threshold, or a dated event"
            )
        if not any(re.search(p, low) for p in _INVALIDATION_SIGNALS):
            raise ValueError(
                "invalidation must contain a checkable condition: a price "
                "level, a percentage threshold, a named MA, or a date"
            )
        return v.strip()

    @model_validator(mode="after")
    def _total_matches_subscores(self) -> DeepDive:
        """The model outputs subscores separately, then sums. We do not let it
        produce a holistic number -- if the sum disagrees, the sum wins."""
        computed = self.subscores.total()
        if self.total_score != computed:
            object.__setattr__(self, "total_score", computed)
        return self


class ModelUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: ModelUsage) -> ModelUsage:
        return ModelUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )
