"""LLM layer: schema enforcement, packet discipline, batching, fallback."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from pydantic import ValidationError

from src.catalysts.macro import MacroState
from src.llm.client import coerce_list, compute_cost, parse_json_response
from src.llm.cost import CostTracker
from src.llm.deep_dive import select_final
from src.llm.packets import build_deep_packet, build_triage_packet, estimate_tokens
from src.llm.schemas import DeepDive, ModelUsage, Subscores, TriageVerdict
from src.llm.triage import _chunk, _fallback_row


def _dive(**over):
    base = dict(
        ticker="NVDA",
        total_score=0,
        subscores=dict(trend=20, fundamental=15, catalyst=12, news=10, macro=7, risk=8),
        thesis="A" * 120,
        bull_case="Datacenter demand keeps compounding.",
        bear_case="Multiple compression on any growth wobble.",
        invalidation="A daily close below $142, the SMA200, invalidates the setup.",
        time_horizon_days=60,
        conviction="high",
        key_risks=["customer concentration"],
        catalysts_ahead=[{"event": "Q3 earnings", "date": "2026-08-20"}],
    )
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Invalidation: the mandatory, checkable field
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "vague",
    [
        "if the thesis breaks",
        "If fundamentals deteriorate",
        "if sentiment turns against the name",
        "general market weakness",
        "the story changes",
        "momentum stalls out",
    ],
)
def test_vague_invalidation_is_rejected(vague):
    """A thesis without a falsification condition is a story, not an analysis."""
    with pytest.raises(ValidationError):
        DeepDive.model_validate(_dive(invalidation=vague))


@pytest.mark.parametrize(
    "concrete",
    [
        "A weekly close below $142.50",
        "Gross margin falls below 60% in the next print",
        "No signed contract announced by 2026-09-30",
        "Price breaks under the SMA200",
        "Revenue growth decelerates more than 15% quarter on quarter",
    ],
)
def test_concrete_invalidation_is_accepted(concrete):
    d = DeepDive.model_validate(_dive(invalidation=concrete))
    assert d.invalidation


def test_total_score_is_recomputed_from_subscores():
    """The model must not produce a holistic number; the sum wins."""
    d = DeepDive.model_validate(_dive(total_score=99))
    assert d.total_score == 72 == d.subscores.total()


def test_subscore_caps_are_enforced():
    with pytest.raises(ValidationError):
        Subscores(trend=26, fundamental=0, catalyst=0, news=0, macro=0, risk=0)
    with pytest.raises(ValidationError):
        Subscores(trend=0, fundamental=0, catalyst=0, news=16, macro=0, risk=0)


def test_thesis_must_be_substantive():
    with pytest.raises(ValidationError):
        DeepDive.model_validate(_dive(thesis="Looks good."))


def test_ticker_is_normalised():
    assert DeepDive.model_validate(_dive(ticker=" nvda ")).ticker == "NVDA"


def test_triage_why_capped_at_20_words():
    v = TriageVerdict(t="msft", score=80, keep=True, why=" ".join(["word"] * 50))
    assert len(v.why.split()) == 20
    assert v.t == "MSFT"


def test_bad_catalyst_date_is_blanked_not_fabricated():
    d = DeepDive.model_validate(
        _dive(catalysts_ahead=[{"event": "earnings", "date": "sometime in Q3"}])
    )
    assert d.catalysts_ahead[0].date == ""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def test_parse_json_tolerates_code_fences_and_preamble():
    assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_response('Here you go:\n{"a": 1}') == {"a": 1}
    assert parse_json_response("```\n[1, 2]\n```") == [1, 2]
    with pytest.raises(ValueError):
        parse_json_response("not json at all")
    with pytest.raises(ValueError):
        parse_json_response("")


def test_coerce_list_unwraps_object_wrapped_arrays():
    assert coerce_list([1, 2]) == [1, 2]
    assert coerce_list({"results": [1]}) == [1]
    assert coerce_list({"verdicts": [2]}) == [2]
    assert coerce_list({"anything": [3]}) == [3]
    assert coerce_list({"a": 1}) == []


# ---------------------------------------------------------------------------
# Token discipline
# ---------------------------------------------------------------------------
def _fixture_series():
    scores = pd.Series(
        {
            "sector": "Technology", "momentum": 1.8, "quality": 1.2,
            "revisions": 2.1, "value": -0.7, "pead": 0.9,
            "factor_composite": 1.4, "data_completeness": 0.9, "regime_fit": 0.8,
            "insider_points": 3.0, "filing_points": 1.0, "news_points": 2.0,
            "options_points": 0.5,
        }
    )
    trend = pd.Series(
        {
            "close": 187.4, "rs_percentile": 94, "atr_pct": 3.4,
            "pct_of_52w_high": 0.97, "ema20": 180.0, "sma50": 172.0,
            "sma200": 150.0, "rsi14": 61, "ret_3m": 0.22, "ret_12m": 0.85,
            "high_52w": 193.0, "low_52w": 98.0, "vol_expansion": 1.3,
            "donchian_breakout": True, "realised_vol_20d": 0.31,
        }
    )
    raw = pd.Series(
        {
            "days_since_earnings": 12, "sue": 2.4, "pead_window": 0.8,
            "earnings_gap": 0.06, "eps_rev_4w": 0.031, "rev_rev_4w": 0.018,
        }
    )
    detail = {
        "insiders": {"summary": "3 buys/30d, 2director+1cfo [CLUSTER]",
                     "distinct_buyers": 3, "cluster": True, "sells": 0,
                     "buy_value_usd": 1_200_000},
        "filings": {"forms": ["8-K"], "flags": ["8-K 1.01 material agreement"]},
        "news": {"volume_z": 2.2, "tone_avg": 3.1, "tone_slope_7d": 0.12,
                 "source_diversity": 0.71, "article_count": 42,
                 "themes": ["MERGER"],
                 "headlines": [{"title": "Big deal announced", "tone": 4.2,
                                "domain": "reuters.com"}]},
        "options": {"iv_rank": 41, "put_call_oi_ratio": 0.6, "skew_25d": -0.02,
                    "upside_oi_share": 0.62},
        "risk": {"short_interest_pct": 2.1, "days_to_cover": 1.2, "spread_bps": 4},
        "reco": {"reco_mean": 4.4, "reco_trend_delta": 0.15},
    }
    return scores, trend, raw, detail


def test_triage_packet_stays_under_the_token_budget():
    """Under 200 tokens per ticker is the entire reason the funnel exists."""
    scores, trend, raw, detail = _fixture_series()
    macro = MacroState(regime="RISK_ON", score=2.0)
    packet = build_triage_packet("NVDA", scores, trend, raw, detail, macro)
    assert estimate_tokens(packet) < 200, f"packet is {estimate_tokens(packet)} tokens"
    assert packet["t"] == "NVDA"
    assert packet["rs"] == 94


def test_triage_packet_drops_empty_fields():
    macro = MacroState(regime="RISK_ON", score=1.0)
    packet = build_triage_packet(
        "X", pd.Series({"sector": "Tech"}), pd.Series(dtype=float),
        pd.Series(dtype=float), {}, macro,
    )
    assert "px" not in packet  # None values are omitted, not sent as null
    assert packet["t"] == "X"


def test_packets_never_contain_raw_prices_or_article_bodies():
    """No raw price arrays, no raw news text -- computed statistics only."""
    scores, trend, raw, detail = _fixture_series()
    macro = MacroState(regime="RISK_ON", score=2.0)
    deep = build_deep_packet("NVDA", scores, trend, raw, detail, macro)

    blob = json.dumps(deep)
    # Headlines are capped and carry no body text.
    for h in deep["news"]["top_headlines"]:
        assert set(h) <= {"h", "tone"}
        assert len(h["h"]) <= 180
    # No key holds a long array of numbers (a price series would).
    def _no_long_numeric_arrays(o):
        if isinstance(o, list):
            nums = [v for v in o if isinstance(v, (int, float))]
            assert len(nums) < 20, "a raw numeric series leaked into the packet"
            for v in o:
                _no_long_numeric_arrays(v)
        elif isinstance(o, dict):
            for v in o.values():
                _no_long_numeric_arrays(v)

    _no_long_numeric_arrays(deep)
    assert len(blob) < 12000


def test_deep_packet_caps_fundamentals_at_8_quarters():
    scores, trend, raw, detail = _fixture_series()
    macro = MacroState(regime="RISK_ON", score=2.0)
    quarterly = pd.DataFrame(
        {"revenue": range(1, 21), "eps": [1.0] * 20},
        index=pd.date_range("2020-03-31", periods=20, freq="QE").date,
    ).astype(float)
    deep = build_deep_packet(
        "X", scores, trend, raw, detail, macro, fundamentals=quarterly
    )
    assert len(deep["fundamentals_8q"]) == 8


def test_deep_packet_truncates_filing_excerpts():
    scores, trend, raw, detail = _fixture_series()
    macro = MacroState(regime="RISK_ON", score=1.0)
    excerpts = [{"form": "8-K", "text": "x" * 5000}] * 6
    deep = build_deep_packet(
        "X", scores, trend, raw, detail, macro, filing_text=excerpts
    )
    assert len(deep["filings"]["excerpts"]) == 3


# ---------------------------------------------------------------------------
# Batching, fallback, sector cap
# ---------------------------------------------------------------------------
def test_chunking_produces_four_batches_of_25():
    packets = [{"t": f"T{i}"} for i in range(100)]
    batches = _chunk(packets, 25)
    assert len(batches) == 4
    assert all(len(b) == 25 for b in batches)


def test_fallback_uses_the_deterministic_stage3_rank():
    """When the model fails, the funnel still produces a defensible order."""
    scores = pd.DataFrame(
        {"stage3_score": [3.0, 2.0, 1.0]}, index=["BEST", "MID", "WORST"]
    )
    best = _fallback_row("BEST", scores)
    worst = _fallback_row("WORST", scores)
    assert best["llm_triage_score"] > worst["llm_triage_score"]
    assert best["source"] == "fallback"
    assert "deterministic" in best["why"]


def test_sector_cap_prevents_a_single_sector_top_ten():
    dives = [
        DeepDive.model_validate(
            _dive(
                ticker=f"T{i}",
                subscores=dict(trend=25 - i, fundamental=20, catalyst=20,
                               news=15, macro=10, risk=10),
            )
        )
        for i in range(10)
    ]
    all_tech = {d.ticker: "Technology" for d in dives}
    picked = select_final(dives, all_tech, take=10, max_per_sector=3)
    assert len(picked) == 3, "sector cap was not enforced"

    mixed = {
        d.ticker: ["Technology", "Energy", "Healthcare", "Financials"][i % 4]
        for i, d in enumerate(dives)
    }
    picked_mixed = select_final(dives, mixed, take=10, max_per_sector=3)
    assert len(picked_mixed) > 3
    counts: dict[str, int] = {}
    for d in picked_mixed:
        counts[mixed[d.ticker]] = counts.get(mixed[d.ticker], 0) + 1
    assert max(counts.values()) <= 3


def test_select_final_ranks_by_total_score():
    dives = [
        DeepDive.model_validate(
            _dive(ticker="LOW",
                  subscores=dict(trend=5, fundamental=5, catalyst=5, news=5,
                                 macro=5, risk=5)),
        ),
        DeepDive.model_validate(
            _dive(ticker="HIGH",
                  subscores=dict(trend=25, fundamental=20, catalyst=20, news=15,
                                 macro=10, risk=10)),
        ),
    ]
    picked = select_final(dives, {"LOW": "A", "HIGH": "B"}, take=1, max_per_sector=3)
    assert picked[0].ticker == "HIGH"


# ---------------------------------------------------------------------------
# Cost tracking
# ---------------------------------------------------------------------------
def test_cost_tracker_sums_and_flags_budget():
    t = CostTracker()
    t.record("triage", ModelUsage(prompt_tokens=1000, completion_tokens=200, cost_usd=0.3))
    t.record("deep_dive", ModelUsage(prompt_tokens=5000, completion_tokens=2000, cost_usd=0.4))
    assert t.total.prompt_tokens == 6000
    assert t.total.cost_usd == pytest.approx(0.7)
    assert t.check_budget() is True

    t.record("deep_dive", ModelUsage(cost_usd=5.0))
    assert t.check_budget() is False
    assert t.summary()["cost_usd"] == pytest.approx(5.7)


def test_compute_cost_from_openrouter_pricing():
    info = {"pricing": {"prompt": "0.0000002", "completion": "0.0000008"}}
    cost = compute_cost({"prompt_tokens": 1_000_000, "completion_tokens": 100_000}, info)
    assert cost == pytest.approx(0.2 + 0.08)
    assert compute_cost({"prompt_tokens": 100}, None) == 0.0
