"""OpenRouter client with model verification, prompt caching and cost tracking.

Model strings are never treated as stable. At startup we hit
`GET /api/v1/models`, verify the configured string resolves, and fail loudly
with the available DeepSeek options if it does not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import structlog

from src.config.rate_limits import CACHE_TTL
from src.config.settings import get_settings
from src.ingest.base import APIClient, PermanentAPIError
from src.llm.schemas import ModelUsage

log = structlog.get_logger(__name__)


class ModelNotAvailable(RuntimeError):
    """The configured model string did not resolve against OpenRouter."""


def make_client(concurrency: int = 5) -> APIClient:
    s = get_settings()
    if not s.openrouter_api_key:
        raise PermanentAPIError("OPENROUTER_API_KEY is not set")
    return APIClient(
        "openrouter",
        s.openrouter_base_url,
        headers={
            "Authorization": f"Bearer {s.openrouter_api_key}",
            "Content-Type": "application/json",
            "X-Title": "AlphaFunnel",
        },
        concurrency=concurrency,
        timeout=180.0,
        cache_ttl=0,  # completions are never cached
    )


async def list_models(client: APIClient) -> list[dict[str, Any]]:
    data = await client.get_json(
        "/models", use_cache=True, cache_ttl=CACHE_TTL["openrouter_models"]
    )
    return (data or {}).get("data") or []


async def verify_model(client: APIClient, model: str) -> dict[str, Any]:
    """Resolve a model string. Raises with the DeepSeek alternatives listed."""
    models = await list_models(client)
    by_id = {m.get("id"): m for m in models if m.get("id")}
    if model in by_id:
        log.info("llm_model_verified", model=model)
        return by_id[model]

    alternatives = sorted(m for m in by_id if "deepseek" in m.lower())
    raise ModelNotAvailable(
        f"Model {model!r} did not resolve against OpenRouter. "
        f"Available DeepSeek models: {alternatives or 'none found'}. "
        f"Set LLM_TRIAGE_MODEL / LLM_DEEP_MODEL to one of these."
    )


async def verify_configured_models() -> dict[str, dict[str, Any]]:
    """Startup check. Called by the pipeline before Stage 4 runs."""
    s = get_settings()
    if not s.llm_verify_models_on_startup:
        return {}
    out = {}
    async with make_client() as c:
        for name in {s.llm_triage_model, s.llm_deep_model}:
            out[name] = await verify_model(c, name)
    return out


def _price(model_info: dict[str, Any], key: str) -> float:
    try:
        return float((model_info.get("pricing") or {}).get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def compute_cost(usage: dict[str, Any], model_info: dict[str, Any] | None) -> float:
    """USD for one completion, from OpenRouter's per-token pricing."""
    if not model_info:
        return 0.0
    pin = _price(model_info, "prompt")
    pout = _price(model_info, "completion")
    return (
        float(usage.get("prompt_tokens") or 0) * pin
        + float(usage.get("completion_tokens") or 0) * pout
    )


async def complete_json(
    client: APIClient,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float | None = None,
    max_tokens: int = 4000,
    model_info: dict[str, Any] | None = None,
    cache_system_block: bool = True,
) -> tuple[Any, ModelUsage, str]:
    """One chat completion, parsed as JSON.

    Returns (parsed, usage, raw_text). Raises ValueError if the response is not
    parseable JSON -- callers handle the retry.

    The system block is marked with cache_control so OpenRouter caches it. We
    pay for the field definitions once, not once per batch.
    """
    s = get_settings()
    system_content: Any = system
    if cache_system_block:
        system_content = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user},
        ],
        "temperature": s.llm_temperature if temperature is None else temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    data = await client.post_json("/chat/completions", payload)
    choices = (data or {}).get("choices") or []
    if not choices:
        raise ValueError(f"OpenRouter returned no choices: {str(data)[:300]}")
    text = (choices[0].get("message") or {}).get("content") or ""

    raw_usage = (data or {}).get("usage") or {}
    usage = ModelUsage(
        prompt_tokens=int(raw_usage.get("prompt_tokens") or 0),
        completion_tokens=int(raw_usage.get("completion_tokens") or 0),
        cost_usd=float(raw_usage.get("cost") or 0.0)
        or compute_cost(raw_usage, model_info),
    )
    return parse_json_response(text), usage, text


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_response(text: str) -> Any:
    """Parse a model response as JSON, tolerating code fences and preamble."""
    if not text or not text.strip():
        raise ValueError("empty response")
    candidate = text.strip()

    m = _FENCE.search(candidate)
    if m:
        candidate = m.group(1).strip()

    try:
        return json.loads(candidate)
    except ValueError:
        pass

    # Fall back to the outermost JSON object/array in the text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        end = candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except ValueError:
                continue
    raise ValueError(f"response was not valid JSON: {candidate[:300]}")


def coerce_list(parsed: Any, keys: Sequence[str] = ("results", "verdicts", "data")) -> list:
    """Models wrap arrays in an object roughly half the time. Unwrap either."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for k in keys:
            v = parsed.get(k)
            if isinstance(v, list):
                return v
        # single-key object wrapping a list
        vals = [v for v in parsed.values() if isinstance(v, list)]
        if len(vals) == 1:
            return vals[0]
    return []
