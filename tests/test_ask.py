"""The question box: what it sends, what it refuses, and what it costs.

The risks worth testing here are not "does it call the API". They are:

  * a public endpoint with a paid key behind it, so the caps must hold across
    restarts and must be checked BEFORE the upstream call
  * a model string that does not resolve must fail at boot with a readable
    reason, not one 404 at a time to readers
  * a ":free" model must be refused -- they get delisted without notice
  * the context must contain that company's filed figures and NOTHING else
"""

from __future__ import annotations

import datetime as dt

import pytest

PE = dt.date(2025, 12, 31)
FD = dt.date(2026, 2, 13)


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ask.db'}")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed(ticker: str = "JPM") -> None:
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap

    metrics = {
        "total_assets": 4_424_900_000_000.0,
        "total_liabilities": 4_062_462_000_000.0,
        "total_equity": 362_438_000_000.0,
        "loans": 1_467_664_000_000.0,
        "cash": 469_000_000_000.0,
        "deposits": 2_560_000_000_000.0,
    }
    with session_scope() as s:
        s.add(SectorMap(ticker=ticker, cik="0", sector="Financials",
                        sector_source="sic"))
        for m, v in metrics.items():
            s.add(Fundamental(
                ticker=ticker, metric=m, value=v, period_end=PE,
                fiscal_period="FY", filing_date=FD, source="sec", restated=False,
            ))


def _view(ticker: str = "JPM"):
    from src.company.view1 import build_view1

    v = build_view1(ticker)
    assert v is not None
    return v


def _info(model: str = "deepseek/deepseek-chat"):
    from src.llm.client import ModelInfo

    return ModelInfo(
        id=model, name="Test", prompt_usd_per_token=1e-7,
        completion_usd_per_token=3e-7,
    )


# ------------------------------------------------------------------- resolving
class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _patch_get(monkeypatch, resp):
    import src.llm.client as client

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return resp

    monkeypatch.setattr(client.httpx, "AsyncClient", FakeClient)


LIST = {"data": [
    {"id": "deepseek/deepseek-chat", "name": "DeepSeek V3",
     "pricing": {"prompt": "0.00000027", "completion": "0.0000011"},
     "context_length": 64000},
    {"id": "google/gemini-2.0-flash-001", "name": "Gemini Flash",
     "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
]}


def test_a_free_variant_is_refused_without_even_asking(db, monkeypatch):
    """':free' models are delisted without notice and capped near 200/day, so
    the box would break silently at an unpredictable moment."""
    import src.llm.client as client

    def boom(**kw):
        raise AssertionError("must not reach the network to reject a :free id")

    monkeypatch.setattr(client.httpx, "AsyncClient", boom)

    import asyncio

    with pytest.raises(client.ModelUnavailable, match="delisted"):
        asyncio.run(client.resolve_model("deepseek/deepseek-chat:free"))


def test_resolving_reads_the_price_from_the_provider(db, monkeypatch):
    """Cost comes from /api/v1/models, so a repricing cannot leave a stale
    number in this repo quietly under-reporting spend."""
    import asyncio

    import src.llm.client as client

    _patch_get(monkeypatch, _Resp(200, LIST))
    info = asyncio.run(client.resolve_model("deepseek/deepseek-chat"))

    assert info.prompt_usd_per_token == pytest.approx(2.7e-7)
    assert info.completion_usd_per_token == pytest.approx(1.1e-6)
    # 500 in, 100 out.
    assert info.cost(500, 100) == pytest.approx(500 * 2.7e-7 + 100 * 1.1e-6)


def test_an_unknown_model_names_itself_and_suggests_neighbours(db, monkeypatch):
    import asyncio

    import src.llm.client as client

    _patch_get(monkeypatch, _Resp(200, LIST))
    with pytest.raises(client.ModelUnavailable) as exc:
        asyncio.run(client.resolve_model("deepseek/deepseek-v9"))

    msg = str(exc.value)
    assert "deepseek/deepseek-v9" in msg
    assert "deepseek/deepseek-chat" in msg, "point at what does exist"


def test_a_missing_key_is_reported_as_such(db, monkeypatch):
    import asyncio

    from src.config.settings import get_settings
    import src.llm.client as client

    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(client.ModelUnavailable, match="OPENROUTER_API_KEY"):
        asyncio.run(client.resolve_model("deepseek/deepseek-chat"))


# --------------------------------------------------------------------- context
def test_the_context_is_this_company_and_nothing_else(db):
    """The first of the two guardrails: the model cannot compare or forecast
    because it has no other company, no price and no history to do it with."""
    from src.llm.ask import build_context

    _seed()
    ctx = build_context(_view())

    assert "JPM" in ctx
    assert "2025-12-31" in ctx and "2026-02-13" in ctx
    assert "$4,424,900,000,000" in ctx
    assert "Loans" in ctx and "Customer deposits" in ctx
    # Nothing beyond this company's own filed figures.
    for forbidden in ("MSFT", "WMT", "price", "sector average", "consensus",
                      "forecast", "peer"):
        assert forbidden.lower() not in ctx.lower()


def test_the_context_stays_small(db):
    """Roughly 500 tokens. A context that quietly grows is a bill that quietly
    grows, on an endpoint anyone can call."""
    from src.llm.ask import SYSTEM_PROMPT, build_prompt

    _seed()
    total = len(SYSTEM_PROMPT) + len(build_prompt(_view(), "Why?"))

    # ~4 chars/token is the usual rule of thumb; 4000 chars ~= 1000 tokens.
    assert total < 4000, f"context is {total} chars, which is too much to send"


def test_the_context_names_what_the_filer_did_not_break_out(db):
    """"Why is so much of this in 'other'?" is answerable only if the missing
    lines are in the context. They are on the page, so they are here."""
    from src.llm.ask import build_context

    _seed()
    ctx = build_context(_view())

    assert "does not break out separately" in ctx
    assert "remainder" in ctx


def test_the_system_prompt_forbids_the_four_things(db):
    from src.llm.ask import SYSTEM_PROMPT

    low = SYSTEM_PROMPT.lower()
    assert "only from the numbers provided" in low
    assert "never predict" in low and "never recommend" in low
    assert "never invent a figure" in low
    assert "two or three sentences" in low


# ------------------------------------------------------------------------ caps
def _usage_row(ip_hash: str, when: dt.datetime, cost: float = 0.0) -> None:
    from src.storage.db import session_scope
    from src.storage.models import LlmUsage

    with session_scope() as s:
        s.add(LlmUsage(
            created_at=when, ticker="JPM", ip_hash=ip_hash,
            model="deepseek/deepseek-chat", prompt_tokens=500,
            completion_tokens=60, cost_usd=cost, ok=True,
        ))


def test_the_per_ip_cap_holds(db, monkeypatch):
    from src.config.settings import get_settings
    from src.llm.ask import RateLimited, check_caps

    monkeypatch.setenv("LLM_ASK_PER_IP_PER_HOUR", "3")
    get_settings.cache_clear()
    now = dt.datetime(2026, 8, 4, 12, 0)
    for _ in range(3):
        _usage_row("aaa", now - dt.timedelta(minutes=5))

    with pytest.raises(RateLimited, match="limit"):
        check_caps("aaa", now)
    check_caps("bbb", now)  # a different caller is unaffected


def test_the_per_ip_window_rolls(db, monkeypatch):
    from src.config.settings import get_settings
    from src.llm.ask import check_caps

    monkeypatch.setenv("LLM_ASK_PER_IP_PER_HOUR", "2")
    get_settings.cache_clear()
    now = dt.datetime(2026, 8, 4, 12, 0)
    _usage_row("aaa", now - dt.timedelta(hours=2))
    _usage_row("aaa", now - dt.timedelta(hours=3))

    # Two hours ago is outside the window, so this must not raise.
    check_caps("aaa", now)


def test_the_daily_request_ceiling_is_a_hard_stop(db, monkeypatch):
    from src.config.settings import get_settings
    from src.llm.ask import RateLimited, check_caps

    monkeypatch.setenv("LLM_ASK_PER_DAY", "5")
    get_settings.cache_clear()
    now = dt.datetime(2026, 8, 4, 23, 0)
    for i in range(5):
        _usage_row(f"ip{i}", now - dt.timedelta(hours=1))

    with pytest.raises(RateLimited, match="daily limit"):
        check_caps("someone-new", now)


def test_the_daily_spend_cap_is_a_hard_stop(db, monkeypatch):
    """The cap that actually matters: requests are a proxy, dollars are not."""
    from src.config.settings import get_settings
    from src.llm.ask import RateLimited, check_caps

    monkeypatch.setenv("LLM_ASK_PER_DAY", "100000")
    monkeypatch.setenv("LLM_ASK_DAILY_COST_USD", "0.50")
    get_settings.cache_clear()
    now = dt.datetime(2026, 8, 4, 9, 0)
    _usage_row("aaa", now - dt.timedelta(minutes=1), cost=0.49)
    check_caps("bbb", now)
    _usage_row("aaa", now - dt.timedelta(minutes=1), cost=0.02)

    with pytest.raises(RateLimited, match="spending cap"):
        check_caps("bbb", now)


def test_the_daily_count_survives_a_restart(db, monkeypatch):
    """The whole reason usage is a table and not a counter. An in-process tally
    resets on every container restart, so on a platform that restarts freely
    the 'hard stop' would be a hard stop per restart."""
    from src.config.settings import get_settings
    from src.llm.ask import RateLimited, check_caps, usage_today
    from src.storage.db import reset_engine_cache

    monkeypatch.setenv("LLM_ASK_PER_DAY", "2")
    get_settings.cache_clear()
    now = dt.datetime(2026, 8, 4, 10, 0)
    _usage_row("aaa", now - dt.timedelta(minutes=10))
    _usage_row("bbb", now - dt.timedelta(minutes=9))

    # Everything in memory goes away.
    reset_engine_cache()
    get_settings.cache_clear()

    assert usage_today(now)["requests"] == 2
    with pytest.raises(RateLimited):
        check_caps("ccc", now)


def test_a_failed_call_still_counts(db, monkeypatch):
    """Otherwise the limiter can be spun by making requests that fail."""
    from src.config.settings import get_settings
    from src.llm.ask import RateLimited, check_caps, record

    monkeypatch.setenv("LLM_ASK_PER_IP_PER_HOUR", "2")
    get_settings.cache_clear()
    record("JPM", "aaa", "m", ok=False, error="upstream 500")
    record("JPM", "aaa", "m", ok=False, error="upstream 500")

    with pytest.raises(RateLimited):
        check_caps("aaa")


def test_caps_are_checked_before_the_upstream_call(db, monkeypatch):
    """A cap enforced after the request has already been paid for is not a cap."""
    import asyncio

    import src.llm.ask as ask
    from src.config.settings import get_settings

    monkeypatch.setenv("LLM_ASK_PER_DAY", "1")
    get_settings.cache_clear()
    _seed()
    _usage_row("aaa", dt.datetime.now(dt.UTC).replace(tzinfo=None))

    async def must_not_run(*a, **kw):
        raise AssertionError("the model was called after the cap was hit")

    monkeypatch.setattr(ask, "ask_once", must_not_run)
    with pytest.raises(ask.RateLimited):
        asyncio.run(ask.answer_question(_info(), _view(), "why?", "aaa"))


def test_the_ip_is_hashed_not_stored(db):
    from src.llm.ask import hash_ip

    h = hash_ip("203.0.113.9")
    assert "203.0.113.9" not in h
    assert len(h) == 32
    assert h == hash_ip("203.0.113.9"), "stable, or the limit does not bind"
    assert h != hash_ip("203.0.113.10")


# -------------------------------------------------------------------- endpoint
@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from src import api

    with TestClient(api.app) as c:
        yield c


def test_the_box_is_absent_when_the_model_did_not_resolve(client, monkeypatch):
    """A disabled input with an apology advertises a feature and then refuses.
    On a page whose claim is 'this is what was filed', absent is better."""
    import src.api as api

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", None)

    text = client.get("/company/JPM").text

    assert "askbox" not in text
    assert "company.js" not in text, "no script for a feature that is off"


def test_the_endpoint_says_which_half_is_broken(client, monkeypatch):
    import src.api as api

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", None)
    monkeypatch.setattr(api, "_ASK_MODEL_ERROR",
                        "deepseek/nope is not in OpenRouter's model list")

    r = client.post("/company/JPM/ask", json={"question": "why?"})

    assert r.status_code == 503
    assert "not in OpenRouter's model list" in r.json()["detail"]


def test_the_box_and_its_suggestions_render(client, monkeypatch):
    import src.api as api

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", _info())

    text = client.get("/company/JPM").text

    assert 'id="askbox"' in text
    assert "company.js" in text
    for q in ("Why is so much of this in &#x27;other&#x27;?",
              "What does negative equity mean?"):
        assert q in text


def test_a_question_returns_the_answer_and_what_it_cost(client, monkeypatch):
    import src.api as api
    import src.llm.ask as ask
    from src.llm.client import Answer

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", _info())
    sent = {}

    async def fake(info, system, prompt, *, timeout, max_tokens):
        sent["system"] = system
        sent["prompt"] = prompt
        return Answer("Loans are 33% of what it owns.", 520, 40,
                      info.cost(520, 40), 700, info.id)

    monkeypatch.setattr(ask, "ask_once", fake)
    r = client.post("/company/JPM/ask", json={"question": "biggest asset?"})

    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "Loans are 33% of what it owns."
    assert body["prompt_tokens"] == 520
    assert body["cost_usd"] == pytest.approx(520 * 1e-7 + 40 * 3e-7)
    # The company's own figures went up, and the question with them.
    assert "$4,424,900,000,000" in sent["prompt"]
    assert "biggest asset?" in sent["prompt"]
    assert "Never predict" in sent["system"]


def test_the_answer_is_recorded_for_admin(client, monkeypatch):
    import src.api as api
    import src.llm.ask as ask
    from src.llm.client import Answer

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", _info())

    async def fake(info, system, prompt, *, timeout, max_tokens):
        return Answer("ok.", 500, 50, info.cost(500, 50), 400, info.id)

    monkeypatch.setattr(ask, "ask_once", fake)
    client.post("/company/JPM/ask", json={"question": "why?"})

    today = ask.usage_today()
    assert today["requests"] == 1
    assert today["prompt_tokens"] == 500
    assert today["cost_usd"] > 0

    panel = client.get("/admin.json").json()["ask"]
    assert panel["today"]["requests"] == 1
    assert panel["caps"]["per_day"] > 0


def test_an_empty_or_oversized_question_is_refused_locally(client, monkeypatch):
    import src.api as api
    import src.llm.ask as ask

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", _info())

    async def must_not_run(*a, **kw):
        raise AssertionError("a malformed question must not reach the model")

    monkeypatch.setattr(ask, "ask_once", must_not_run)

    assert client.post("/company/JPM/ask", json={"question": "  "}).status_code == 400
    assert client.post(
        "/company/JPM/ask", json={"question": "x" * 500}
    ).status_code == 400


def test_a_ticker_with_no_figures_is_refused_before_the_model(client, monkeypatch):
    import src.api as api
    import src.llm.ask as ask

    monkeypatch.setattr(api, "_ASK_MODEL", _info())

    async def must_not_run(*a, **kw):
        raise AssertionError("nothing to answer from; must not spend a call")

    monkeypatch.setattr(ask, "ask_once", must_not_run)
    r = client.post("/company/NOSUCH/ask", json={"question": "why?"})

    assert r.status_code == 404


def test_an_upstream_failure_is_not_leaked_to_the_reader(client, monkeypatch):
    """The reader gets a sentence; the operator gets the row and the log."""
    import src.api as api
    import src.llm.ask as ask

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", _info())

    async def boom(*a, **kw):
        raise RuntimeError("OpenRouter HTTP 500: Bearer sk-or-v1-SECRET leaked")

    monkeypatch.setattr(ask, "ask_once", boom)
    r = client.post("/company/JPM/ask", json={"question": "why?"})

    assert r.status_code == 502
    assert "SECRET" not in r.text and "sk-or" not in r.text
    assert ask.usage_today()["requests"] == 1, "a failed attempt still counts"


def test_a_rate_limited_caller_gets_a_429_and_a_retry_after(client, monkeypatch):
    import src.api as api
    import src.llm.ask as ask
    from src.config.settings import get_settings

    _seed()
    monkeypatch.setattr(api, "_ASK_MODEL", _info())
    monkeypatch.setenv("LLM_ASK_PER_DAY", "1")
    get_settings.cache_clear()
    _usage_row("x", dt.datetime.now(dt.UTC).replace(tzinfo=None))

    r = client.post("/company/JPM/ask", json={"question": "why?"})

    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "midnight UTC" in r.json()["detail"]
