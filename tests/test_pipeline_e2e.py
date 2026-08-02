"""End-to-end funnel test.

Every external API is stubbed. The point is to prove that Stages 0-3 produce a
defensible ranked list on their own, with no model involved -- exactly the
property the build order demands before the LLM layer is allowed to exist.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.catalysts.macro import MacroState
from src.catalysts.stage3 import combine_and_rank, score_catalysts
from src.factors.composite import run_stage2
from src.factors.trend import load_panels, run_trend_gate
from src.storage import repository
from src.storage.models import EarningsEvent, EstimateSnapshot, Fundamental
from src.storage.pit import get_universe
from src.universe.builder import build_universe_from_frames

AS_OF = dt.date(2025, 6, 2)
N_NAMES = 120


@pytest.fixture
def seeded(session):
    """A miniature but complete market: bars, universe, fundamentals, earnings."""
    rng = np.random.default_rng(42)
    dates = [d.date() for d in pd.bdate_range(end=AS_OF, periods=420)]
    tickers = [f"T{i:03d}" for i in range(N_NAMES)]
    sectors = ["Technology", "Healthcare", "Energy", "Financial Services"]

    # Half trend up, half trend down.
    drift = np.where(np.arange(N_NAMES) < N_NAMES // 2, 0.0018, -0.0018)
    rets = rng.normal(0, 0.008, (len(dates), N_NAMES)) + drift
    closes = 100 * np.exp(np.cumsum(rets, axis=0))

    bars = []
    for j, t in enumerate(tickers):
        for i, d in enumerate(dates):
            c = float(closes[i, j])
            bars.append(
                {
                    "ticker": t, "date": d, "open": c * 0.995, "high": c * 1.01,
                    "low": c * 0.99, "close": c, "volume": 2_000_000.0,
                }
            )
    repository.save_bars(session, bars)
    session.flush()

    ref = [
        {
            "ticker": t, "name": f"Company {t}", "security_type": "CS",
            "exchange": "XNAS", "cik": f"{1000 + i}", "active": True,
        }
        for i, t in enumerate(tickers)
    ]
    scr = [
        {
            "ticker": t, "market_cap": 1e9 + i * 1e8,
            "sector": sectors[i % len(sectors)], "industry": "X",
        }
        for i, t in enumerate(tickers)
    ]
    today_bars = [b for b in bars if b["date"] == AS_OF]
    universe, _ = build_universe_from_frames(AS_OF, session, today_bars, ref, scr)

    # PIT fundamentals: 8 quarters, each filed 40 days after period end.
    fund_rows = []
    for i, t in enumerate(tickers):
        quality = 1.0 + (i % 7) * 0.15
        for q in range(8):
            period_end = AS_OF - dt.timedelta(days=90 * (8 - q) + 5)
            filing = period_end + dt.timedelta(days=40)
            base = {
                "revenue": 1000 * quality * (1 + 0.03 * q),
                "cogs": 600 * (1 + 0.02 * q),
                "gross_profit": 400 * quality * (1 + 0.04 * q),
                "net_income": 100 * quality,
                "operating_income": 140 * quality,
                "ebit": 140 * quality,
                "ebitda": 180 * quality,
                "total_assets": 5000.0,
                "total_equity": 3000.0,
                "total_debt": 1000.0,
                "cash": 500.0,
                "operating_cash_flow": 150 * quality,
                "capex": -30.0,
                "current_assets": 2000.0,
                "current_liabilities": 900.0 - q * 5,
                "long_term_debt": 800.0 - q * 10,
                "shares_diluted": 100.0,
                "eps": quality,
                "income_tax": 30.0,
                "pretax_income": 130.0,
            }
            for metric, value in base.items():
                fund_rows.append(
                    Fundamental(
                        ticker=t, metric=metric, value=float(value),
                        period_end=period_end, fiscal_period=f"Q{q % 4 + 1}",
                        filing_date=filing, source="sec",
                    )
                )
    session.add_all(fund_rows)

    # Earnings and estimate snapshots for PEAD and revisions.
    for i, t in enumerate(tickers):
        for k in range(6):
            rd = AS_OF - dt.timedelta(days=20 + 90 * k)
            session.add(
                EarningsEvent(
                    ticker=t, report_date=rd,
                    actual_eps=1.0 + (i % 5) * 0.1 + k * 0.01,
                    consensus_eps=1.0, surprise_pct=(i % 5) * 0.1,
                    gap_pct=((i % 5) - 2) * 0.02,
                )
            )
        for weeks_ago in (6, 0):
            session.add(
                EstimateSnapshot(
                    ticker=t, captured_on=AS_OF - dt.timedelta(weeks=weeks_ago),
                    horizon="fq1",
                    eps_consensus=1.0 + (0.05 * (i % 4) if weeks_ago == 0 else 0),
                    revenue_consensus=1000.0 + (10 * (i % 4) if weeks_ago == 0 else 0),
                )
            )
    session.flush()
    return {"universe": universe, "tickers": tickers}


def test_stages_0_through_3_produce_a_ranked_list_without_any_llm(seeded, session):
    """After step 3 of the build order you have something usable. Prove it."""
    universe = seeded["universe"]
    assert len(universe) == N_NAMES

    tickers = universe["ticker"].tolist()
    sectors = universe.set_index("ticker")["sector"]

    # ---- Stage 1
    panels = load_panels(session, tickers, AS_OF)
    tr = run_trend_gate(
        panels["close"], panels["high"], panels["low"], panels["volume"],
        sectors=sectors,
    )
    assert 0 < len(tr.survivors) < len(tickers), "the trend gate cut nothing"
    # Downtrend names must be gone.
    assert not any(int(t[1:]) >= N_NAMES // 2 for t in tr.survivors.index)

    # ---- Stage 2
    comp = run_stage2(session, tr.survivors, universe, AS_OF, take=20)
    assert len(comp.selected) == min(20, len(tr.survivors))
    scores = comp.scores
    assert scores["factor_composite"].notna().all()
    assert scores["factor_composite"].is_monotonic_decreasing
    # Fundamentals were actually read (PIT), so quality is differentiated.
    assert scores["quality"].std() > 0
    assert scores["data_completeness"].mean() > 0.5

    # Sector neutrality: the top names are not all one sector.
    top_sectors = scores.head(10)["sector"]
    assert top_sectors.nunique() > 1, "top 10 collapsed into a single sector"

    # ---- Stage 3 scoring (no network: enrichment stubbed as empty)
    selected = comp.selected
    catalysts, detail = score_catalysts(session, selected, AS_OF, {t: {} for t in selected})
    assert len(catalysts) == len(selected)
    assert (catalysts["catalyst_score"] == 0).all()  # nothing to score, honestly zero

    macro = MacroState(regime="RISK_ON", score=2.0)
    ranked = combine_and_rank(
        comp.scores.loc[selected], catalysts, macro, take=10
    )
    assert len(ranked) == 10
    assert ranked["stage3_score"].is_monotonic_decreasing
    # The regime tilt was applied.
    assert (ranked["sector_tilt"] != 1.0).any()


def test_catalyst_scores_reorder_the_factor_ranking(seeded, session):
    """Stage 3 must be capable of changing the order, or it is decoration."""
    universe = seeded["universe"]
    tickers = universe["ticker"].tolist()
    sectors = universe.set_index("ticker")["sector"]

    panels = load_panels(session, tickers, AS_OF)
    tr = run_trend_gate(panels["close"], panels["high"], panels["low"],
                        panels["volume"], sectors=sectors)
    comp = run_stage2(session, tr.survivors, universe, AS_OF, take=20)
    selected = comp.selected

    # Give the WORST factor name a huge catalyst score.
    worst = selected[-1]
    catalysts = pd.DataFrame(
        {"catalyst_score": [0.0] * len(selected), "has_dilution_filing": False},
        index=pd.Index(selected, name="ticker"),
    )
    catalysts.loc[worst, "catalyst_score"] = 20.0

    macro = MacroState(regime="RISK_ON", score=2.0)
    ranked = combine_and_rank(comp.scores.loc[selected], catalysts, macro,
                              take=len(selected))
    assert ranked.index[0] == worst, "catalyst score had no effect on the ranking"


def test_pipeline_persists_a_universe_snapshot_for_backtests(seeded, session):
    session.flush()
    uni = get_universe(session, AS_OF)
    assert len(uni) == N_NAMES
    assert (uni["as_of_date"] == AS_OF).all()


def test_checkpoint_roundtrip_allows_resume(session):
    """If Stage 4 fails we resume from the Stage 3 output, not from zero."""
    repository.save_checkpoint(
        session, "run-1", AS_OF, 3, entry_count=400, exit_count=100,
        duration_s=12.5, api_calls=1200, payload={"selected": ["AAA", "BBB"]},
        rejected={"crowded short": 3},
    )
    session.flush()
    loaded = repository.load_checkpoint(session, "run-1", 3)
    assert loaded == {"selected": ["AAA", "BBB"]}
    assert repository.load_checkpoint(session, "run-1", 4) is None


def test_data_quality_gate_rejects_a_catastrophic_drop():
    from src.pipeline import DataQualityError, _check_drop

    warnings: list[str] = []
    _check_drop("2 factors", 1000, 400, warnings)  # normal
    assert not warnings

    _check_drop("2 factors", 1000, 95, warnings)  # 90.5% -- warn but proceed
    assert len(warnings) == 1

    with pytest.raises(DataQualityError):
        _check_drop("2 factors", 1000, 10, warnings)  # 99% -- abort


def test_last_trading_day_never_returns_a_weekend():
    from src.pipeline import _last_trading_day

    for offset in range(14):
        day = dt.date(2025, 6, 1) + dt.timedelta(days=offset)
        got = _last_trading_day(day)
        assert got.weekday() < 5, f"{got} is a weekend"
        assert got < day


def test_rate_limiter_paces_requests():
    """Nothing sprinkles time.sleep(); everything goes through the bucket."""
    from src.config.rate_limits import BUCKETS, Bucket
    from src.ingest.rate_limiter import RateLimiter

    BUCKETS["_test"] = Bucket("_test", rps=1000.0, burst=2, max_wait_s=1.0)
    limiter = RateLimiter(prefix="test-rl")

    async def run():
        for _ in range(5):
            await limiter.acquire("_test")

    asyncio.run(run())
    assert limiter.call_counts()["_test"] == 5
    BUCKETS.pop("_test")


def test_unknown_source_has_no_silent_default():
    from src.ingest.rate_limiter import RateLimiter

    with pytest.raises(KeyError):
        asyncio.run(RateLimiter().acquire("not_a_source"))


def test_stage3_checkpoint_roundtrips_with_dates_and_nans(session):
    """The Stage 3 payload carries dates, NaN and numpy scalars. All must
    survive a trip through the JSON column, or resume is a lie."""
    import numpy as np

    from src.catalysts.stage3 import (
        Stage3Result,
        checkpoint_payload,
        restore_from_checkpoint,
    )

    selected = pd.DataFrame(
        {"stage3_score": [2.0, 1.0], "catalyst_score": [np.nan, 3.0],
         "sector": ["Technology", "Energy"]},
        index=pd.Index(["AAA", "BBB"], name="ticker"),
    )
    result = Stage3Result(
        selected=selected,
        all_scored=selected,
        rejected={"CCC": ["earnings in 2bd"]},
        detail={
            "AAA": {
                "insiders": {"summary": "3 buys/30d", "cluster": True},
                "risk": {"next_earnings": dt.date(2025, 7, 1),
                         "short_interest_pct": np.float64(2.5)},
            }
        },
        api_calls=1600,
    )

    repository.save_checkpoint(
        session, "run-ckpt", AS_OF, 3, entry_count=400, exit_count=2,
        duration_s=90.0, api_calls=1600,
        payload=checkpoint_payload(result), rejected=result.rejected,
    )
    session.commit()

    restored = restore_from_checkpoint(
        repository.load_checkpoint(session, "run-ckpt", 3)
    )
    assert restored is not None
    assert list(restored.selected.index) == ["AAA", "BBB"]
    assert restored.api_calls == 1600
    assert restored.detail["AAA"]["insiders"]["cluster"] is True
    # Dates survive as ISO strings; NaN became a proper null.
    assert restored.detail["AAA"]["risk"]["next_earnings"] == "2025-07-01"
    assert restored.selected.at["AAA", "catalyst_score"] is None or pd.isna(
        restored.selected.at["AAA", "catalyst_score"]
    )


def test_restore_from_checkpoint_rejects_garbage():
    from src.catalysts.stage3 import restore_from_checkpoint

    assert restore_from_checkpoint(None) is None
    assert restore_from_checkpoint({}) is None
    assert restore_from_checkpoint({"selected": []}) is None
    assert restore_from_checkpoint({"selected": [{"no_ticker": 1}]}) is None


def test_fast_mode_persists_picks_so_the_site_shows_them(session):
    """skip_llm has no DeepDives; it must still persist minimal theses from the
    deterministic top-N, or the site's picks list comes up empty."""
    import datetime as dt
    from types import SimpleNamespace

    import pandas as pd

    from src.pipeline import _deterministic_score, _persist_scores
    from src.storage import repository

    as_of = dt.date(2025, 6, 2)
    comp = SimpleNamespace(scores=pd.DataFrame())  # no per-name scores to store
    det = [
        {"ticker": "AAA", "factor_composite": 2.0, "sector": "Tech"},
        {"ticker": "BBB", "factor_composite": 0.0, "sector": "Energy"},
        {"ticker": "CCC", "factor_composite": -1.0, "sector": "Health"},
    ]
    _persist_scores(session, as_of, None, comp, None, None, [], pd.Series(dtype=object), det)
    session.flush()

    theses = {t.ticker: t for t in repository.get_theses(session, as_of)}
    assert set(theses) == {"AAA", "BBB", "CCC"}
    assert theses["AAA"].total_score == _deterministic_score(2.0)  # ~74
    assert theses["BBB"].total_score == 50
    assert theses["AAA"].total_score > theses["CCC"].total_score  # ordering holds
    assert theses["AAA"].conviction is None  # honest: no model conviction


def test_history_depth_counts_distinct_trading_days(session):
    """The pipeline's insufficient-history guard keys off this: distinct trading
    days stored on/before as_of, the ceiling on any name's usable history."""
    import datetime as dt

    from src.pipeline import _history_depth
    from src.storage import repository

    as_of = dt.date(2025, 6, 2)
    dates = [d.date() for d in pd.bdate_range(end=as_of, periods=100)]
    # Two tickers sharing the same 100 dates -> 100 distinct days, not 200.
    repository.save_bars(
        session,
        [
            {"ticker": t, "date": d, "open": 10, "high": 11, "low": 9,
             "close": 10.0, "volume": 1_000_000}
            for t in ("AAA", "BBB")
            for d in dates
        ],
    )
    session.flush()
    assert _history_depth(session, as_of) == 100
    # A future as_of still only sees what's stored on/before it.
    assert _history_depth(session, dates[50]) == 51
