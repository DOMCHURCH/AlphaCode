"""A small OpenRouter client for the question box, and the model check.

Two jobs, both narrow:

  1. RESOLVE THE MODEL AT STARTUP. `GET /api/v1/models` is the list of what
     actually exists right now. A model string that does not appear in it is a
     configuration error, and the failure mode without this check is the worst
     kind: every question returns a 404 from upstream, at runtime, to a reader,
     with no signal to the operator. So it is resolved once at boot and the
     result -- available or the exact reason not -- is carried on the page and
     on /admin.

     The same call returns `pricing`, so the per-token price comes from the
     provider rather than a table in this repo that goes stale the first time
     the model is repriced.

  2. ASK ONE QUESTION. One turn, no history, no tools, no streaming.

`:free` model variants are refused outright. They are delisted without notice
and capped around 200 requests a day, which turns a working feature into a
broken one at an unpredictable moment for no saving worth having.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
import structlog

from src.config.settings import get_settings

log = structlog.get_logger(__name__)

# OpenRouter prices are dollars per token, as decimal strings.
_FREE_SUFFIX = ":free"


class ModelUnavailable(RuntimeError):
    """The configured model does not resolve. The feature must stay off."""


@dataclass(frozen=True)
class ModelInfo:
    """A model that exists, with the price the provider quoted for it."""

    id: str
    name: str
    prompt_usd_per_token: float
    completion_usd_per_token: float
    context_length: int | None = None

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.prompt_usd_per_token
            + completion_tokens * self.completion_usd_per_token
        )


@dataclass(frozen=True)
class Answer:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    model: str


def _price(raw: object) -> float:
    try:
        return max(0.0, float(str(raw)))
    except (TypeError, ValueError):
        return 0.0


async def resolve_model(model: str, *, timeout: float = 20.0) -> ModelInfo:
    """Confirm `model` is listed by OpenRouter and read back its price.

    Raises ModelUnavailable with a reason a human can act on -- the point of
    checking at startup is to turn a runtime 404 into a boot-time sentence.
    """
    if not model.strip():
        raise ModelUnavailable("LLM_ASK_MODEL is empty.")
    if model.endswith(_FREE_SUFFIX):
        raise ModelUnavailable(
            f"{model} is a ':free' variant. Those are delisted without notice "
            f"and rate-limited to roughly 200 requests a day. Set LLM_ASK_MODEL "
            f"to the paid id (drop the ':free' suffix)."
        )

    s = get_settings()
    if not s.openrouter_api_key:
        raise ModelUnavailable("OPENROUTER_API_KEY is not set.")

    url = f"{s.openrouter_base_url.rstrip('/')}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.get(
                url, headers={"Authorization": f"Bearer {s.openrouter_api_key}"}
            )
    except httpx.HTTPError as exc:
        raise ModelUnavailable(
            f"Could not reach {url}: {type(exc).__name__}: {str(exc)[:120]}"
        ) from exc

    if resp.status_code != 200:
        raise ModelUnavailable(f"{url} returned HTTP {resp.status_code}.")
    try:
        listed = resp.json().get("data") or []
    except ValueError as exc:
        raise ModelUnavailable(f"{url} did not return JSON.") from exc

    for m in listed:
        if m.get("id") != model:
            continue
        pricing = m.get("pricing") or {}
        info = ModelInfo(
            id=model,
            name=str(m.get("name") or model),
            prompt_usd_per_token=_price(pricing.get("prompt")),
            completion_usd_per_token=_price(pricing.get("completion")),
            context_length=m.get("context_length"),
        )
        if info.prompt_usd_per_token == 0.0 and info.completion_usd_per_token == 0.0:
            # A zero price is how a free tier presents itself even without the
            # suffix. Report it rather than quietly logging $0.00 per question.
            log.warning("llm_model_priced_at_zero", model=model)
        log.info(
            "llm_model_resolved", model=model,
            prompt_per_mtok=round(info.prompt_usd_per_token * 1e6, 4),
            completion_per_mtok=round(info.completion_usd_per_token * 1e6, 4),
        )
        return info

    near = [
        m.get("id") for m in listed
        if isinstance(m.get("id"), str) and model.split("/")[0] in m["id"]
    ][:5]
    raise ModelUnavailable(
        f"{model} is not in OpenRouter's model list ({len(listed)} models). "
        + (f"Same provider: {', '.join(near)}." if near else "Check the id.")
    )


async def ask_once(
    info: ModelInfo, system: str, question: str, *, timeout: float, max_tokens: int
) -> Answer:
    """One completion. No history, no tools, no streaming."""
    s = get_settings()
    url = f"{s.openrouter_base_url.rstrip('/')}/chat/completions"
    started = time.monotonic()
    payload = {
        "model": info.id,
        "temperature": 0.0,  # the same question should get the same answer
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
    }
    async with httpx.AsyncClient(timeout=timeout) as c:
        resp = await c.post(
            url,
            headers={
                "Authorization": f"Bearer {s.openrouter_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("OpenRouter returned no choices.")
    text = (choices[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    return Answer(
        text=text.strip(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=info.cost(prompt_tokens, completion_tokens),
        latency_ms=latency_ms,
        model=info.id,
    )
