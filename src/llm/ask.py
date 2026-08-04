"""The question box behind /company/{ticker}/ask.

Scope is deliberately tiny. The model sees ONE company's filed figures -- the
same numbers already drawn on the page -- and nothing else. No raw filings, no
other companies, no prices, no history. That bounds the context to a few
hundred tokens, and more importantly it bounds what the model can say: it
cannot compare, rank or forecast, because it has nothing to do it with.

The system prompt then forbids the same things in words. Both layers matter --
the context stops it having the material, the prompt stops it inventing some.

Everything about this feature is off the critical path. The page renders, and
is complete and correct, whether or not the model is reachable.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import structlog

from src.company.view1 import View1
from src.config.settings import get_settings
from src.llm.client import Answer, ModelInfo, ask_once

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You answer questions about one company's filed financial \
statements, using only the figures given to you below.

Rules, in order of precedence:
1. Answer only from the numbers provided. If the answer is not in them, say so \
plainly and stop.
2. Never predict, never recommend, never say whether the company is good or \
bad, healthy or unhealthy, strong or weak, cheap or expensive.
3. Never invent a figure. If asked about something not in the numbers below -- \
another company, a sector average, a share price, an earlier period -- say it \
is not in the filing data you were given.
4. Two or three sentences. No lists, no headings, no preamble.

The figures are as reported to the SEC for one period. "Other" lines are \
remainders: the total minus the components the filer broke out separately, not \
an estimate of anything."""

# What the reader is looking at, in the order they read it.
_SUGGESTED = (
    "Why is so much of this in 'other'?",
    "How does this compare to a typical company in this sector?",
    "What does negative equity mean?",
)


def suggested_questions() -> tuple[str, ...]:
    return _SUGGESTED


def _money(v: float | None) -> str:
    """Whole dollars, grouped. The model reasons about magnitude better from
    $4,424,900,000,000 than from "$4.42T", and the token cost is trivial."""
    return "not reported" if v is None else f"${v:,.0f}"


def build_context(view: View1) -> str:
    """The company's own filed figures, and nothing else.

    Exactly what the page draws: the same totals, the same components with the
    same percentages, the same named-but-missing lines. If a figure is not on
    the page it is not here, so the model and the reader are looking at one set
    of numbers.
    """
    d = view.as_dict()
    lines: list[str] = [
        f"Company: {d['company_name'] or d['ticker']} ({d['ticker']})",
    ]
    if d["sector"]:
        lines.append(f"Sector: {d['sector']}")
    lines += [
        f"Period end: {d['period_end']}",
        f"Filed with the SEC: {d['filing_date']}",
        "",
        f"Total assets: {_money(d['total_assets'])}",
        f"Total liabilities: {_money(d['total_liabilities'])}",
        f"Total equity: {_money(d['total_equity'])}",
    ]
    if d["liabilities_derived_from"]:
        lines.append(
            f"(Total liabilities is not stated separately in this filing; it is "
            f"computed from {d['liabilities_derived_from']}.)"
        )
    if d["negative_equity"]:
        lines.append("(Equity is negative: liabilities exceed total assets.)")

    lines.append("")
    lines.append("What it owns, as filed:")
    for b in d["assets"]:
        note = " (remainder: the total minus the lines above)" if b["is_remainder"] else ""
        lines.append(f"  {b['label']}: {_money(b['value'])} = {b['pct']:.1f}%{note}")

    lines.append("")
    lines.append("Claims on it, as filed:")
    for b in d["claims"]:
        note = " (remainder: the total minus the lines above)" if b["is_remainder"] else ""
        lines.append(f"  {b['label']}: {_money(b['value'])} = {b['pct']:.1f}%{note}")

    if d["missing_components"]:
        lines.append("")
        lines.append(
            "Lines this filer does not break out separately (they sit inside "
            "the totals and inside the 'other' remainders above): "
            + ", ".join(d["missing_components"])
        )
    return "\n".join(lines)


def build_prompt(view: View1, question: str) -> str:
    return f"{build_context(view)}\n\nQuestion: {question.strip()}"


def hash_ip(ip: str) -> str:
    """A salted digest of the caller's address.

    Enough to rate-limit one caller; not a record of who read what. Salted with
    the API key so the digests are not reversible from a public rainbow table
    of the IPv4 space.
    """
    salt = get_settings().openrouter_api_key or "to-scale"
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# caps
# --------------------------------------------------------------------------
class RateLimited(RuntimeError):
    """A cap was hit. Carries what to tell the reader and when to come back."""

    def __init__(self, message: str, retry_after_s: int = 3600) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def _day_start(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def usage_today(now: dt.datetime | None = None) -> dict[str, float]:
    """Requests and spend since 00:00 UTC, read from the table."""
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import LlmUsage

    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    since = _day_start(now)
    with session_scope() as s:
        row = s.execute(
            select(
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.cost_usd), 0.0),
                func.coalesce(func.sum(LlmUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LlmUsage.completion_tokens), 0),
            ).where(LlmUsage.created_at >= since)
        ).one()
    return {
        "requests": int(row[0] or 0),
        "cost_usd": float(row[1] or 0.0),
        "prompt_tokens": int(row[2] or 0),
        "completion_tokens": int(row[3] or 0),
    }


def check_caps(ip_hash: str, now: dt.datetime | None = None) -> None:
    """Raise RateLimited if this request must not be made.

    Checked BEFORE the upstream call, against persisted rows, so a restart
    cannot hand out a fresh daily budget. Failed calls count: an attempt costs
    an attempt, and a limiter that only counted successes could be spun by
    making requests that fail.
    """
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import LlmUsage

    s = get_settings()
    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)

    with session_scope() as session:
        per_ip = session.execute(
            select(func.count(LlmUsage.id)).where(
                LlmUsage.ip_hash == ip_hash,
                LlmUsage.created_at >= now - dt.timedelta(hours=1),
            )
        ).scalar_one()
    if s.llm_ask_per_ip_per_hour > 0 and per_ip >= s.llm_ask_per_ip_per_hour:
        raise RateLimited(
            f"That's {per_ip} questions in the last hour from here, which is "
            f"the limit. Try again later.",
        )

    today = usage_today(now)
    if s.llm_ask_per_day > 0 and today["requests"] >= s.llm_ask_per_day:
        raise RateLimited(
            "The question box has hit its daily limit. It resets at midnight UTC."
        )
    if s.llm_ask_daily_cost_usd > 0 and today["cost_usd"] >= s.llm_ask_daily_cost_usd:
        raise RateLimited(
            "The question box has hit its daily spending cap. It resets at "
            "midnight UTC."
        )


def record(
    ticker: str,
    ip_hash: str,
    model: str,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    ok: bool = True,
    error: str | None = None,
) -> None:
    """Write one usage row. Never raises -- accounting must not fail a reply."""
    from src.storage.db import session_scope
    from src.storage.models import LlmUsage

    try:
        with session_scope() as s:
            s.add(LlmUsage(
                ticker=ticker[:16], ip_hash=ip_hash, model=model[:96],
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cost_usd=cost_usd, latency_ms=latency_ms, ok=ok,
                error=error[:200] if error else None,
            ))
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_usage_record_failed", error=str(exc)[:200])


async def answer_question(
    info: ModelInfo, view: View1, question: str, ip_hash: str
) -> Answer:
    """Ask, then record -- whichever way it goes."""
    s = get_settings()
    check_caps(ip_hash)
    prompt = build_prompt(view, question)
    try:
        out = await ask_once(
            info, SYSTEM_PROMPT, prompt,
            timeout=s.llm_ask_timeout_s, max_tokens=s.llm_ask_max_tokens,
        )
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
        record(view.ticker, ip_hash, info.id, ok=False, error=str(exc))
        raise
    record(
        view.ticker, ip_hash, info.id,
        prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens,
        cost_usd=out.cost_usd, latency_ms=out.latency_ms, ok=True,
    )
    log.info(
        "llm_ask", ticker=view.ticker, model=info.id,
        prompt_tokens=out.prompt_tokens, completion_tokens=out.completion_tokens,
        cost_usd=round(out.cost_usd, 6), latency_ms=out.latency_ms,
    )
    return out
