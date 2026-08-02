"""MOMENTUM-ONLY mode: a separate, labelled artifact -- not a degraded full run.

The whole point is that it never pretends to be the full composite. These tests
lock down the three things that make that true: it scores on the momentum
factors only, it bypasses the completeness gate BY DESIGN (while the full mode's
floor stays untouched), and every artifact it produces is labelled.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.config import factor_weights as fw


def test_label_names_the_exact_shortfall():
    assert fw.MODE_LABEL[fw.MODE_MOMENTUM_ONLY] == (
        "MOMENTUM ONLY — 3 of 19 factors — not the full composite."
    )
    # The full mode carries no label -- an empty label is what the templates key
    # off to render nothing.
    assert fw.MODE_LABEL[fw.MODE_FULL] == ""


def test_momentum_only_scores_on_momentum_factors_alone():
    w, f = fw.mode_config(fw.MODE_MOMENTUM_ONLY)
    assert set(w) == {"momentum"} and w["momentum"] == 1.0
    assert set(f) == {"momentum"}
    assert set(f["momentum"]) == {"mom_12_1", "mom_quality", "mom_residual"}


def test_full_mode_config_is_untouched():
    """Adding a mode must not have changed the real thing."""
    w, f = fw.mode_config(fw.MODE_FULL)
    assert w == fw.CATEGORY_WEIGHTS
    assert f == fw.CATEGORY_FACTORS
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert sum(len(x) for x in f.values()) == 19


def test_unknown_mode_is_rejected_not_defaulted():
    with pytest.raises(ValueError, match="unknown scoring mode"):
        fw.mode_config("cheeky")


def _raw_frame(n: int = 40) -> pd.DataFrame:
    """Stage-2 raw factors with momentum PRESENT and everything else EMPTY --
    exactly the state the funnel is in while fundamentals load."""
    rng = np.random.default_rng(0)
    idx = pd.Index([f"T{i:03d}" for i in range(n)], name="ticker")
    raw = pd.DataFrame(index=idx)
    for f in ("mom_12_1", "mom_quality", "mom_residual"):
        raw[f] = rng.normal(size=n)
    for f in fw.CATEGORY_FACTORS:
        for col in fw.CATEGORY_FACTORS[f]:
            if col not in raw.columns:
                raw[col] = np.nan
    raw["sector"] = [f"S{i % 4}" for i in range(n)]
    return raw


def test_momentum_only_ranks_when_the_full_composite_cannot():
    """The full composite on this frame is ~16% complete (3/19) and would be
    gated. Momentum-only produces a real, ordered ranking from the same data."""
    from src.factors.composite import score_composite

    raw = _raw_frame()
    w, f = fw.mode_config(fw.MODE_MOMENTUM_ONLY)
    res = score_composite(raw, category_weights=w, category_factors=f)
    scores = res.scores["factor_composite"].dropna()
    assert len(scores) == len(raw), "momentum-only left names unscored"
    # A real ranking: strictly ordered, not all-equal.
    assert scores.nunique() > 1
    assert list(scores) == sorted(scores, reverse=True)


def test_gate_still_trips_for_a_full_run_on_the_same_data():
    """The floor is untouched: the SAME thin data still aborts a full run."""
    from src.factors.composite import ALL_FACTORS, CompositeResult, score_composite
    from src.pipeline import DataQualityError, _completeness_gate

    raw = _raw_frame()
    res = score_composite(raw)  # full mode
    n = len(raw)
    res.factor_coverage = {
        f: round(float(raw[f].notna().sum()) / n, 3) if f in raw.columns else 0.0
        for f in ALL_FACTORS
    }
    assert isinstance(res, CompositeResult)
    with pytest.raises(DataQualityError, match="completeness"):
        _completeness_gate(res, 0.40)


def test_report_labels_header_and_every_block(session, tmp_path):
    """The label must reach the rendered page, and repeat across blocks -- a
    single card screenshotted alone still has to carry the caveat."""
    from src.catalysts.macro import MacroState
    from src.report.builder import build_report

    as_of = dt.date(2025, 8, 1)
    label = fw.MODE_LABEL[fw.MODE_MOMENTUM_ONLY]
    build_report(
        session, as_of=as_of, run_id="mom", dives=[], scores=pd.DataFrame(),
        trend_features=pd.DataFrame(), detail={},
        macro=MacroState(regime="NORMAL", score=0.0),
        funnel_counts={"Stage 0 universe": 5376, "Stage 1 trend": 900},
        funnel_rejects={}, stage_sectors={}, near_misses=[], api_calls={},
        cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "by_stage": {}},
        duration_s=12.0,
        warnings=[label],
        deterministic_names=[
            {"rank": 1, "ticker": "AAA", "sector": "Info Tech",
             "stage3_score": 1.2, "factor_composite": 1.2, "catalyst_score": 0.0,
             "categories": {"momentum": 1.2, "quality": None, "revisions": None,
                            "pead": None, "value": None},
             "data_completeness": 0.16},
        ],
        output_dir=str(tmp_path),
        mode=fw.MODE_MOMENTUM_ONLY,
    )
    html = (tmp_path / f"report_{as_of.isoformat()}.html").read_text()
    # Header banner + the ranking block + the warning box.
    assert html.count(label) >= 2, "label not repeated across report blocks"
    assert "modebanner" in html
    # And the weights table must show what ACTUALLY scored this run, not the
    # full five categories -- printing those would misrepresent the ranking on
    # the very page reporting it.
    wtable = html.split("Factor weights used")[1].split("</table>")[0]
    assert "mom_12_1" in wtable
    for absent in ("gross_profitability", "eps_rev_4w", "ev_ebit_inv", "sue"):
        assert absent not in wtable, f"{absent} shown as if it scored this run"


def test_full_mode_report_carries_no_label(session, tmp_path):
    """The label is exclusive to a partial mode -- a full run must stay clean."""
    from src.catalysts.macro import MacroState
    from src.report.builder import build_report

    as_of = dt.date(2025, 8, 1)
    build_report(
        session, as_of=as_of, run_id="full", dives=[], scores=pd.DataFrame(),
        trend_features=pd.DataFrame(), detail={},
        macro=MacroState(regime="NORMAL", score=0.0),
        funnel_counts={"Stage 0 universe": 5376}, funnel_rejects={},
        stage_sectors={}, near_misses=[], api_calls={},
        cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "by_stage": {}},
        duration_s=12.0, output_dir=str(tmp_path),
    )
    html = (tmp_path / f"report_{as_of.isoformat()}.html").read_text()
    assert "MOMENTUM ONLY" not in html
    # The CSS rule always ships; what must be absent is a rendered banner.
    assert 'class="modebanner' not in html


def test_stored_scores_carry_the_mode(tmp_path, monkeypatch):
    """IC must be able to separate the two. The column is stored, indexed, and
    defaults to 'full' so existing rows keep meaning what they meant."""
    from sqlalchemy import select

    from src.config.settings import get_settings
    from src.storage import repository
    from src.storage.db import init_db, reset_engine_cache, session_scope
    from src.storage.models import DailyScore

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        day = dt.date(2025, 8, 1)
        with session_scope() as s:
            repository.save_scores(s, [
                {"as_of_date": day, "ticker": "AAA", "stage_reached": 2,
                 "mode": fw.MODE_MOMENTUM_ONLY, "factor_composite": 1.0},
            ])
            repository.save_scores(s, [
                {"as_of_date": dt.date(2025, 8, 2), "ticker": "AAA",
                 "stage_reached": 2, "factor_composite": 2.0},  # default
            ])
        with session_scope() as s:
            got = dict(s.execute(
                select(DailyScore.as_of_date, DailyScore.mode)
            ).all())
        assert got[day] == fw.MODE_MOMENTUM_ONLY
        assert got[dt.date(2025, 8, 2)] == fw.MODE_FULL
    finally:
        get_settings.cache_clear()
        reset_engine_cache()


def test_ic_never_pools_the_two_modes(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage import repository
    from src.storage.db import init_db, reset_engine_cache, session_scope
    from src.validation.ic import compute_ic

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ic.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    try:
        rows = []
        for i in range(6):
            rows.append({
                "as_of_date": dt.date(2025, 8, 1), "ticker": f"M{i}",
                "stage_reached": 2, "mode": fw.MODE_MOMENTUM_ONLY,
                "factor_composite": float(i), "fwd_ret_21d": float(i) / 100,
            })
        with session_scope() as s:
            repository.save_scores(s, rows)
        with session_scope() as s:
            # A full-mode IC query must NOT see the momentum-only rows.
            full = compute_ic(s, score_col="factor_composite", horizon=21,
                              mode=fw.MODE_FULL)
            mom = compute_ic(s, score_col="factor_composite", horizon=21,
                             mode=fw.MODE_MOMENTUM_ONLY)
        assert full.n == 0, "full-mode IC pooled momentum-only scores"
        assert mom.n == 6
    finally:
        get_settings.cache_clear()
        reset_engine_cache()
