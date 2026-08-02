"""End-to-end proof of MOMENTUM-ONLY mode on the exact blocked state.

Seeds a market with bars and a universe but NO fundamentals, estimates or
earnings -- the state the funnel is actually in while the SEC backfill loads.
On that data:
  * a FULL run must still abort on the completeness floor (the floor is intact),
  * a MOMENTUM-ONLY run must produce a real, ordered, LABELLED top-10.
Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.config import factor_weights as fw

AS_OF = dt.date(2025, 6, 2)
N_NAMES = 150


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mom.db'}")
    monkeypatch.setenv("MIN_UNIVERSE_SIZE", "50")
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("FRED_API_KEY", "")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield tmp_path
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def _seed_prices_only(as_of: dt.date = AS_OF, n: int = N_NAMES) -> None:
    """Bars + universe ONLY. No Fundamental / EarningsEvent / EstimateSnapshot --
    every quality/value/revisions/pead factor is genuinely absent."""
    from src.storage import repository
    from src.storage.db import session_scope
    from src.universe.builder import build_universe_from_frames

    rng = np.random.default_rng(7)
    dates = [d.date() for d in pd.bdate_range(end=as_of, periods=420)]
    tickers = [f"T{i:03d}" for i in range(n)]
    sectors = ["Technology", "Healthcare", "Energy", "Financial Services", "Utilities"]
    drift = np.where(np.arange(n) < n * 2 // 3, 0.0016, -0.0016)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.009, (len(dates), n)) + drift, axis=0))

    with session_scope() as session:
        bars = [
            {"ticker": t, "date": d,
             "open": float(closes[i, j]) * 0.995, "high": float(closes[i, j]) * 1.01,
             "low": float(closes[i, j]) * 0.99, "close": float(closes[i, j]),
             "volume": 3_000_000.0}
            for j, t in enumerate(tickers)
            for i, d in enumerate(dates)
        ]
        repository.save_bars(session, bars)
        session.flush()
        ref = [{"ticker": t, "name": f"Co {t}", "security_type": "CS",
                "exchange": "XNAS", "cik": str(1000 + i), "active": True}
               for i, t in enumerate(tickers)]
        scr = [{"ticker": t, "market_cap": 1e9 + i * 1e8,
                "sector": sectors[i % 5], "industry": "X"}
               for i, t in enumerate(tickers)]
        build_universe_from_frames(
            as_of, session, [b for b in bars if b["date"] == as_of], ref, scr
        )


async def _empty_enrich(session, tickers, universe, as_of, **kw):
    return {t: {} for t in tickers}


def test_full_run_is_still_gated_on_prices_only_data(file_db, monkeypatch):
    """The floor is untouched: with fundamentals absent, a FULL run aborts."""
    from src import pipeline
    from src.catalysts import stage3 as s3

    _seed_prices_only()
    monkeypatch.setattr(s3, "enrich_tickers", _empty_enrich)
    with pytest.raises(pipeline.DataQualityError, match="completeness"):
        asyncio.run(
            pipeline.run_pipeline(
                AS_OF, run_id="FULL-GATED", resume_from=1, skip_llm=True,
                persist_universe=False,
            )
        )


def test_momentum_only_produces_a_labelled_top10(file_db, monkeypatch):
    """The deliverable: output while fundamentals load, labelled on its face."""
    from sqlalchemy import select

    from src import pipeline
    from src.catalysts import stage3 as s3
    from src.storage.db import session_scope
    from src.storage.models import DailyScore, RunLog, Thesis

    _seed_prices_only()
    monkeypatch.setattr(s3, "enrich_tickers", _empty_enrich)
    result = asyncio.run(
        pipeline.run_pipeline(
            AS_OF, run_id="MOM-E2E", resume_from=1, skip_llm=True,
            persist_universe=False, mode=fw.MODE_MOMENTUM_ONLY,
        )
    )

    label = fw.MODE_LABEL[fw.MODE_MOMENTUM_ONLY]
    # 1. It ran to completion and produced a ranked list.
    assert result.status == "ok"
    assert result.funnel_counts["Stage 2 factors"] > 0

    # 2. The run says what it is, in its warnings and on the RunLog.
    assert label in result.warnings
    with session_scope() as s:
        run = s.execute(
            select(RunLog).where(RunLog.run_id == "MOM-E2E")
        ).scalar_one()
        assert run.mode == fw.MODE_MOMENTUM_ONLY

    # 3. Every stored score is stamped, so IC can never pool it with a full run.
    with session_scope() as s:
        modes = set(s.execute(select(DailyScore.mode)).scalars())
        # In fast mode the published top-10 is the deterministic list stored as
        # theses (no DeepDives, so final_rank is unset by design).
        picks = s.execute(
            select(Thesis.ticker, Thesis.total_score, Thesis.thesis)
            .order_by(Thesis.total_score.desc())
        ).all()
    assert modes == {fw.MODE_MOMENTUM_ONLY}

    # 4. A real top-10: ten distinct names, ordered, with actual scores.
    assert len(picks) == 10, f"expected a top-10, got {len(picks)}"
    assert len({t for t, _s, _th in picks}) == 10
    scores_desc = [sc for _t, sc, _th in picks]
    assert scores_desc == sorted(scores_desc, reverse=True)
    assert len(set(scores_desc)) > 1, "every pick scored the same -- not a ranking"

    # 5. The stored thesis itself carries the label: /stock/<ticker> and the
    # site's picks list read this text, so it cannot be the only unlabelled path.
    for _t, _sc, text in picks:
        assert text.startswith(label), f"unlabelled thesis: {text[:60]}"

    # 6. The rendered report carries the label.

    html = (file_db / "reports" / f"report_{AS_OF.isoformat()}.html").read_text()
    assert html.count(label) >= 2
