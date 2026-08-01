"""Stage 6: charts render and the report is a single portable HTML file."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from src.catalysts.macro import MacroState
from src.llm.schemas import DeepDive
from src.report import charts as ch
from src.report.builder import build_report

AS_OF = dt.date(2025, 6, 2)


def _dive(ticker="NVDA", trend=22):
    return DeepDive.model_validate(
        {
            "ticker": ticker,
            "total_score": 0,
            "subscores": {"trend": trend, "fundamental": 16, "catalyst": 14,
                          "news": 11, "macro": 8, "risk": 8},
            "thesis": (
                "The name sits at 97% of its 52-week high with RS 94 and a "
                "12-1 momentum z of 1.8. Gross profitability of 0.31 places it "
                "in the top decile of its sector. Three insiders bought in the "
                "last 30 days including the CFO. Estimate revisions are running "
                "positive at +3.1% over four weeks."
            ),
            "bull_case": "Revisions momentum is self-reinforcing here.",
            "bear_case": "A high multiple leaves no room for a growth wobble.",
            "invalidation": "A daily close below $142, which is the SMA200.",
            "time_horizon_days": 60,
            "conviction": "high",
            "key_risks": ["customer concentration", "export controls"],
            "catalysts_ahead": [{"event": "Q3 earnings", "date": "2025-08-20"}],
        }
    )


@pytest.fixture
def bars():
    dates = [d.date() for d in pd.bdate_range(end=AS_OF, periods=260)]
    base = pd.Series(range(260), dtype=float) * 0.4 + 100
    return pd.DataFrame(
        {
            "date": dates,
            "open": base * 0.995, "high": base * 1.01,
            "low": base * 0.99, "close": base, "volume": 1e6,
        }
    )


# ---------------------------------------------------------------------------
# Individual charts
# ---------------------------------------------------------------------------
def test_price_panel_renders_with_event_markers(bars):
    events = ch.build_event_markers(
        earnings_dates=[AS_OF - dt.timedelta(days=30)],
        filings=[{"form": "8-K", "filing_date": AS_OF - dt.timedelta(days=10)}],
        insider_buys=[{"code": "P", "date": AS_OF - dt.timedelta(days=5)}],
    )
    assert len(events) == 3
    out = ch.price_panel("NVDA", bars, events=events, high_52w=204.0, low_52w=100.0)
    assert out.get("img") or out.get("html")


def test_event_markers_drop_undated_entries():
    events = ch.build_event_markers(
        filings=[{"form": "8-K", "filing_date": None}],
        insider_buys=[{"code": "S", "date": AS_OF}],  # sells are not marked
    )
    assert events == []


def test_factor_radar_renders():
    out = ch.factor_radar(
        "NVDA",
        {"trend": 22, "fundamental": 16, "catalyst": 14, "news": 11,
         "macro": 8, "risk": 8},
        median={"trend": 15, "fundamental": 10, "catalyst": 8, "news": 7,
                "macro": 5, "risk": 5},
    )
    assert out.get("img") or out.get("html")


def test_funnel_chart_shows_stage_counts():
    out = ch.funnel_chart(
        {"Stage 0": 6000, "Stage 1": 1200, "Stage 2": 400,
         "Stage 3": 100, "Stage 4": 25, "Stage 5": 10},
        {"Stage 1": {"near_52w_high": 3000}},
    )
    assert out.get("img") or out.get("html")


def test_charts_degrade_on_empty_input():
    assert ch.price_panel("X", pd.DataFrame()) == {"html": ""}
    assert ch.fundamental_trend("X", pd.DataFrame()) == {"html": ""}
    assert ch.news_timeline("X", []) == {"html": ""}
    assert ch.peer_comparison("X", pd.DataFrame()) == {"html": ""}
    assert ch.sector_heatmap({}) == {"html": ""}
    assert ch.funnel_chart({}) == {"html": ""}


def test_sector_heatmap_across_stages():
    out = ch.sector_heatmap(
        {
            "Stage 0": {"Technology": 1500, "Energy": 600},
            "Stage 1": {"Technology": 400, "Energy": 90},
            "Stage 5": {"Technology": 3, "Energy": 1},
        }
    )
    assert out.get("img") or out.get("html")


# ---------------------------------------------------------------------------
# Full report
# ---------------------------------------------------------------------------
def test_report_renders_a_single_portable_html_file(session, tmp_path, bars):
    from src.storage import repository

    repository.save_bars(
        session,
        [{"ticker": "NVDA", **{k: r[k] for k in
                               ("date", "open", "high", "low", "close", "volume")}}
         for _, r in bars.iterrows()],
    )
    session.flush()

    dives = [_dive("NVDA", 22), _dive("AMD", 18)]
    scores = pd.DataFrame(
        {
            "sector": ["Technology", "Technology", "Energy"],
            "factor_composite": [1.4, 1.1, 0.9],
            "data_completeness": [0.92, 0.81, 0.77],
            "completeness_momentum": [1.0, 1.0, 1.0],
            "completeness_quality": [0.83, 0.66, 0.5],
            "completeness_revisions": [1.0, 0.5, 0.5],
            "completeness_pead": [1.0, 1.0, 0.0],
            "completeness_value": [0.66, 0.66, 0.33],
        },
        index=["NVDA", "AMD", "XOM"],
    )
    trend_features = pd.DataFrame(
        {"high_52w": [204.0, 190.0, 120.0], "low_52w": [100.0, 90.0, 80.0],
         "close": [200.0, 180.0, 110.0]},
        index=["NVDA", "AMD", "XOM"],
    )
    macro = MacroState(
        regime="RISK_ON", score=2.1,
        levels={"T10Y2Y": 0.4, "BAMLH0A0HYM2": 3.2, "VIXCLS": 14.0},
        signals={"credit_level": 1.0},
        series={"VIXCLS": [{"date": AS_OF, "value": 14.0}]},
    )

    build_report(
        session,
        as_of=AS_OF, run_id="test-run", dives=dives, scores=scores,
        trend_features=trend_features,
        detail={"NVDA": {"news": {"volume_z": 2.1}}, "AMD": {}},
        macro=macro,
        funnel_counts={"Stage 0 universe": 6000, "Stage 1 trend": 1200,
                       "Stage 2 factors": 400, "Stage 3 catalysts": 100,
                       "Stage 4 triage": 25, "Stage 5 final": 2},
        funnel_rejects={"Stage 1 trend": {"near_52w_high": 3000}},
        stage_sectors={"Stage 0": {"Technology": 1500, "Energy": 600},
                       "Stage 5": {"Technology": 2}},
        near_misses=[{"ticker": "XOM", "sector": "Energy", "score": 71.0,
                      "why": "strong trend, weak revisions"}],
        api_calls={"polygon": 3, "sec": 400, "gdelt": 1600},
        cost={"tokens_in": 41000, "tokens_out": 9000, "cost_usd": 0.42,
              "by_stage": {"triage": {"cost_usd": 0.08},
                           "deep_dive": {"cost_usd": 0.34}}},
        duration_s=412.0,
        warnings=["REGIME: RISK_OFF — RS threshold relaxed to 50."],
        output_dir=str(tmp_path),
    )

    html_path = tmp_path / f"report_{AS_OF.isoformat()}.html"
    assert html_path.exists()
    html = html_path.read_text()

    # Content
    assert "NVDA" in html and "AMD" in html
    assert "RISK_ON" in html
    assert "Invalidation" in html
    assert "SMA200" in html  # the invalidation text itself
    assert "XOM" in html  # near misses
    assert "Nothing here is investment advice" in html
    assert "RS threshold relaxed" in html  # the warning surfaced

    # Portability: CSS inlined, images embedded, nothing fetched at view time.
    # (A bare "http://" match is not a useful check -- inlined plotly.js
    # contains URLs inside error-message strings.)
    assert "<style>" in html
    assert 'rel="stylesheet"' not in html
    import re

    for attr in ("src", "href"):
        for m in re.finditer(rf'{attr}\s*=\s*"([^"]+)"', html):
            url = m.group(1)
            assert not url.startswith(("http://", "https://", "//")), (
                f"report loads a remote asset: {url[:80]}"
            )

    # Funnel counts are shown.
    assert "6,000" in html or "6000" in html


def test_report_handles_an_empty_final_list(session, tmp_path):
    macro = MacroState(regime="RISK_OFF", score=-2.5)
    paths = build_report(
        session, as_of=AS_OF, run_id="empty", dives=[], scores=pd.DataFrame(),
        trend_features=pd.DataFrame(), detail={}, macro=macro,
        funnel_counts={"Stage 0 universe": 5800, "Stage 1 trend": 210},
        funnel_rejects={}, stage_sectors={}, near_misses=[],
        api_calls={}, cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                            "by_stage": {}},
        duration_s=30.0,
        warnings=["REGIME: RISK_OFF"],
        output_dir=str(tmp_path),
    )
    html = (tmp_path / f"report_{AS_OF.isoformat()}.html").read_text()
    assert "No names cleared the funnel today." in html
    assert paths["html"]


def test_deterministic_report_shows_stages_0_3_ranking(session, tmp_path):
    """With the LLM skipped, the report must still show the funnel's own
    ranking -- Stages 0-3 stand on their own."""
    macro = MacroState(regime="LATE_CYCLE", score=0.0)
    build_report(
        session, as_of=AS_OF, run_id="det", dives=[], scores=pd.DataFrame(),
        trend_features=pd.DataFrame(), detail={}, macro=macro,
        funnel_counts={"Stage 0 universe": 5900, "Stage 3 catalysts": 100},
        funnel_rejects={}, stage_sectors={}, near_misses=[],
        api_calls={}, cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                            "by_stage": {}},
        duration_s=45.0,
        warnings=["LLM stages skipped"],
        deterministic_names=[
            {"rank": 1, "ticker": "AAPL", "sector": "Technology",
             "stage3_score": 1.82, "factor_composite": 1.4,
             "catalyst_score": 3.0, "data_completeness": 0.91,
             "categories": {"momentum": 1.6, "quality": 1.1, "revisions": 0.9,
                            "pead": 0.4, "value": -0.3}},
            {"rank": 2, "ticker": "XOM", "sector": "Energy",
             "stage3_score": 1.10, "factor_composite": 1.0,
             "catalyst_score": None, "data_completeness": 0.7,
             "categories": {"momentum": 0.8, "quality": 1.3, "revisions": None,
                            "pead": 0.0, "value": 0.6}},
        ],
        output_dir=str(tmp_path),
    )
    html = (tmp_path / f"report_{AS_OF.isoformat()}.html").read_text()
    assert "Deterministic ranking" in html
    assert "AAPL" in html and "XOM" in html
    assert "1.82" in html
    # A missing catalyst score renders as 0.0, not a crash or the word None.
    assert "No names cleared the funnel today." not in html


def test_pdf_renders_a_valid_document_when_weasyprint_available(session, tmp_path, bars):
    """The spec asks for a PDF alongside the HTML. When weasyprint and its
    system libs are present, `_render_pdf` must produce a structurally valid
    PDF -- not silently skip. Skipped (not failed) where the libs are absent, so
    a bare CI still passes; the deploy image installs them via nixpacks.toml."""
    pytest.importorskip("weasyprint")

    from src.storage import repository

    repository.save_bars(
        session,
        [{"ticker": "NVDA", **{k: r[k] for k in
                               ("date", "open", "high", "low", "close", "volume")}}
         for _, r in bars.iterrows()],
    )
    session.flush()

    macro = MacroState(regime="RISK_ON", score=2.0,
                       series={"VIXCLS": [{"date": AS_OF, "value": 14.0}]})
    paths = build_report(
        session, as_of=AS_OF, run_id="pdf-test", dives=[_dive("NVDA")],
        scores=pd.DataFrame(
            {"sector": ["Technology"], "factor_composite": [1.4],
             "data_completeness": [0.9], "completeness_momentum": [1.0],
             "completeness_quality": [0.8], "completeness_revisions": [1.0],
             "completeness_pead": [1.0], "completeness_value": [0.6]},
            index=["NVDA"]),
        trend_features=pd.DataFrame(
            {"high_52w": [204.0], "low_52w": [100.0], "close": [200.0]},
            index=["NVDA"]),
        detail={"NVDA": {}}, macro=macro,
        funnel_counts={"Stage 0 universe": 6000, "Stage 5 final": 1},
        funnel_rejects={}, stage_sectors={"Stage 0": {"Technology": 6000}},
        near_misses=[], api_calls={"sec": 400},
        cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "by_stage": {}},
        duration_s=100.0, output_dir=str(tmp_path),
    )

    pdf_path = tmp_path / f"report_{AS_OF.isoformat()}.pdf"
    assert paths.get("pdf"), "report builder reported no PDF despite weasyprint present"
    assert pdf_path.exists()
    data = pdf_path.read_bytes()
    assert data[:5] == b"%PDF-", "output is not a PDF"
    assert b"%%EOF" in data[-2048:], "PDF is truncated / has no EOF marker"
    assert len(data) > 5000, "PDF is implausibly small"


def test_report_is_stored_in_db_and_served_without_disk(session, tmp_path, bars):
    """The Railway fix: the api must serve the report from the DB even when the
    rendering filesystem is gone. Render, then read it back from the DB with the
    disk directory deleted."""
    import shutil

    from src.storage import repository

    repository.save_bars(
        session,
        [{"ticker": "NVDA", **{k: r[k] for k in
                               ("date", "open", "high", "low", "close", "volume")}}
         for _, r in bars.iterrows()],
    )
    session.flush()

    macro = MacroState(regime="RISK_ON", score=2.0,
                       series={"VIXCLS": [{"date": AS_OF, "value": 14.0}]})
    build_report(
        session, as_of=AS_OF, run_id="db-test", dives=[_dive("NVDA")],
        scores=pd.DataFrame(
            {"sector": ["Technology"], "factor_composite": [1.4],
             "data_completeness": [0.9], "completeness_momentum": [1.0],
             "completeness_quality": [0.8], "completeness_revisions": [1.0],
             "completeness_pead": [1.0], "completeness_value": [0.6]},
            index=["NVDA"]),
        trend_features=pd.DataFrame(
            {"high_52w": [204.0], "low_52w": [100.0], "close": [200.0]},
            index=["NVDA"]),
        detail={"NVDA": {}}, macro=macro,
        funnel_counts={"Stage 0 universe": 6000, "Stage 5 final": 1},
        funnel_rejects={}, stage_sectors={"Stage 0": {"Technology": 6000}},
        near_misses=[], api_calls={"sec": 400},
        cost={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "by_stage": {}},
        duration_s=100.0, output_dir=str(tmp_path),
    )
    session.flush()

    # Simulate the api service: a different filesystem with no report files.
    shutil.rmtree(tmp_path)

    artifact = repository.get_report_artifact(session, AS_OF)
    assert artifact is not None, "report was not persisted to the DB"
    assert artifact.html and "NVDA" in artifact.html
    assert artifact.html_bytes > 1000
    # PDF is stored too when weasyprint is available.
    if artifact.pdf:
        assert artifact.pdf[:5] == b"%PDF-"
        assert artifact.pdf_bytes > 5000
