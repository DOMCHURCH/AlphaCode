"""Full-pipeline resume test.

Unlike test_pipeline_e2e (which drives the stages individually), this runs the
real `run_pipeline` orchestrator against a shared on-disk database, then resumes
it and proves the Stage 3 checkpoint is actually reused -- the expensive
enrichment is NOT re-run. That is the whole point of writing the checkpoint.

Every external API is stubbed to no-ops or empty; nothing here reaches the
network. The deterministic stages run for real on seeded data.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import numpy as np
import pandas as pd
import pytest

AS_OF = dt.date(2025, 6, 2)
N_NAMES = 150


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A file-backed sqlite DB that run_pipeline and the test both see."""
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    url = f"sqlite:///{tmp_path / 'resume.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("MIN_UNIVERSE_SIZE", "50")
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("FRED_API_KEY", "")  # macro degrades to neutral, no network

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        yield url
    finally:
        # Leave the caches clean so later tests get their own settings/engine.
        get_settings.cache_clear()
        reset_engine_cache()


def _seed(as_of: dt.date = AS_OF, n: int = N_NAMES) -> None:
    """Seed a miniature but complete market into the configured DB."""
    from src.storage import repository
    from src.storage.db import session_scope
    from src.storage.models import EarningsEvent, EstimateSnapshot, Fundamental
    from src.universe.builder import build_universe_from_frames

    rng = np.random.default_rng(42)
    dates = [d.date() for d in pd.bdate_range(end=as_of, periods=420)]
    tickers = [f"T{i:03d}" for i in range(n)]
    sectors = ["Technology", "Healthcare", "Energy", "Financial Services", "Utilities"]

    drift = np.where(np.arange(n) < n * 2 // 3, 0.0016, -0.0016)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.009, (len(dates), n)) + drift, axis=0))

    with session_scope() as session:
        bars = [
            {
                "ticker": t, "date": d,
                "open": float(closes[i, j]) * 0.995, "high": float(closes[i, j]) * 1.01,
                "low": float(closes[i, j]) * 0.99, "close": float(closes[i, j]),
                "volume": 3_000_000.0,
            }
            for j, t in enumerate(tickers)
            for i, d in enumerate(dates)
        ]
        repository.save_bars(session, bars)
        session.flush()

        ref = [
            {"ticker": t, "name": f"Co {t}", "security_type": "CS",
             "exchange": "XNAS", "cik": str(1000 + i), "active": True}
            for i, t in enumerate(tickers)
        ]
        scr = [
            {"ticker": t, "market_cap": 1e9 + i * 1e8,
             "sector": sectors[i % 5], "industry": "X"}
            for i, t in enumerate(tickers)
        ]
        build_universe_from_frames(
            as_of, session, [b for b in bars if b["date"] == as_of], ref, scr
        )

        fund = []
        for i, t in enumerate(tickers):
            q = 1.0 + (i % 7) * 0.15
            for k in range(8):
                pe = as_of - dt.timedelta(days=90 * (8 - k) + 5)
                fd = pe + dt.timedelta(days=40)
                base = {
                    "revenue": 1000 * q, "cogs": 600.0, "gross_profit": 400 * q,
                    "net_income": 100 * q, "operating_income": 140 * q,
                    "ebit": 140 * q, "ebitda": 180 * q, "total_assets": 5000.0,
                    "total_equity": 3000.0, "total_debt": 1000.0, "cash": 500.0,
                    "operating_cash_flow": 150 * q, "capex": -30.0,
                    "current_assets": 2000.0, "current_liabilities": 900.0 - k * 5,
                    "long_term_debt": 800.0 - k * 10, "shares_diluted": 100.0,
                    "eps": q, "income_tax": 30.0, "pretax_income": 130.0,
                }
                for m, v in base.items():
                    fund.append(
                        Fundamental(ticker=t, metric=m, value=float(v), period_end=pe,
                                    fiscal_period=f"Q{k % 4 + 1}", filing_date=fd,
                                    source="sec")
                    )
        session.add_all(fund)
        for i, t in enumerate(tickers):
            for k in range(6):
                session.add(EarningsEvent(
                    ticker=t, report_date=as_of - dt.timedelta(days=20 + 90 * k),
                    actual_eps=1.0 + (i % 5) * 0.1, consensus_eps=1.0,
                    gap_pct=((i % 5) - 2) * 0.02))
            for w in (6, 0):
                session.add(EstimateSnapshot(
                    ticker=t, captured_on=as_of - dt.timedelta(weeks=w), horizon="fq1",
                    eps_consensus=1.0 + (0.05 * (i % 4) if w == 0 else 0),
                    revenue_consensus=1000.0 + (10 * (i % 4) if w == 0 else 0)))


async def _empty_enrich(session, tickers, universe, as_of, **kw):
    return {t: {} for t in tickers}


def test_resume_reuses_stage3_checkpoint_without_re_enriching(file_db, monkeypatch):
    """Run the funnel, then resume it: Stage 3 enrichment must not run again."""
    from src import pipeline
    from src.catalysts import stage3 as s3
    from src.storage import repository
    from src.storage.db import session_scope

    _seed()

    # First run: no network. resume_from=1 skips the Polygon universe build and
    # uses the seeded snapshot; enrichment is stubbed empty.
    monkeypatch.setattr(s3, "enrich_tickers", _empty_enrich)
    first = asyncio.run(
        pipeline.run_pipeline(
            AS_OF, run_id="RESUME-TEST", resume_from=1, skip_llm=True,
            persist_universe=False,
        )
    )
    assert first.funnel_counts["Stage 3 catalysts"] > 0

    # The Stage 3 checkpoint must be on disk for the resume to find.
    with session_scope() as session:
        assert repository.max_checkpoint_stage(session, "RESUME-TEST") >= 3
        assert repository.latest_run_id(session, AS_OF) == "RESUME-TEST"

    # Now make enrichment explode. A correct resume never calls it.
    async def _boom(*a, **k):
        raise AssertionError("Stage 3 enrichment was re-run on resume")

    monkeypatch.setattr(s3, "enrich_tickers", _boom)
    second = asyncio.run(pipeline.resume_last(AS_OF, skip_llm=True))

    # Same run, restored -- funnel counts for the restored stages match.
    assert second.run_id == "RESUME-TEST"
    assert second.funnel_counts["Stage 3 catalysts"] == \
        first.funnel_counts["Stage 3 catalysts"]
    assert second.report_paths.get("html")


def test_resume_without_a_prior_run_raises(file_db):
    from src import pipeline

    with pytest.raises(RuntimeError, match="No prior run"):
        asyncio.run(pipeline.resume_last(AS_OF))


def test_start_run_is_idempotent_for_resume(file_db):
    """Reusing a run_id must not violate the unique constraint."""
    from src.storage import repository
    from src.storage.db import session_scope

    with session_scope() as session:
        repository.start_run(session, "DUP", AS_OF)
    with session_scope() as session:
        repository.finish_run(session, "DUP", status="failed", error="boom")
    # A resume calls start_run again with the same id -- must reset, not insert.
    with session_scope() as session:
        repository.start_run(session, "DUP", AS_OF)
    with session_scope() as session:
        run = repository.get_run(session, AS_OF)
        assert run.run_id == "DUP"
        assert run.status == "running"
        assert run.error is None
        assert run.finished_at is None
