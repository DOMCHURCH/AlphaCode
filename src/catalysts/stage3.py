"""Stage 3 orchestration -- catalysts, news, flow. 400 -> ~100.

Per-ticker calls are affordable now. Everything runs async with a semaphore of
10 behind the Redis rate limiter.

Order of operations:
  1. fan out per-ticker enrichment (SEC, Finnhub, GDELT, options)
  2. score catalysts
  3. apply the crowding/tradeability screen as a hard filter
  4. rank on factor_composite + catalyst_score, apply the regime sector tilt
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import structlog
from sqlalchemy.orm import Session

from src.catalysts import news as news_scorer
from src.catalysts import options as options_scorer
from src.catalysts import sec_events
from src.catalysts.macro import MacroState
from src.catalysts.risk_screen import estimate_spread_bps, screen_name
from src.config.factor_weights import REGIME_TILT_STRENGTH
from src.config.settings import get_settings
from src.ingest import finnhub as fh
from src.ingest import gdelt as gd
from src.ingest import polygon as pg
from src.ingest import sec_edgar as sec
from src.ingest import yahoo
from src.ingest.base import gather_bounded
from src.storage import repository
from src.storage.pit import (
    get_filings,
    get_insider_transactions,
    get_next_earnings,
)

log = structlog.get_logger(__name__)


@dataclass
class Stage3Result:
    selected: pd.DataFrame  # ticker-indexed, ranked
    all_scored: pd.DataFrame
    rejected: dict[str, list[str]] = field(default_factory=dict)
    detail: dict[str, dict[str, Any]] = field(default_factory=dict)
    api_calls: int = 0


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------
async def enrich_tickers(
    session: Session,
    tickers: Sequence[str],
    universe: pd.DataFrame,
    as_of: dt.date,
    *,
    concurrency: int = 10,
    use_options: bool = True,
) -> dict[str, dict[str, Any]]:
    """Fan out the per-ticker API work and persist everything it returns."""
    uni = universe.set_index("ticker") if "ticker" in universe.columns else universe
    names = uni.get("name", pd.Series(dtype=object))
    ciks = uni.get("cik", pd.Series(dtype=object))

    out: dict[str, dict[str, Any]] = {t: {} for t in tickers}

    s = get_settings()
    sec_client = sec.make_client(concurrency=concurrency)
    gd_client = gd.make_client(concurrency=min(concurrency, 4))
    # Finnhub (estimates/insiders) and Polygon options are optional upgrades:
    # skip them cleanly when their keys aren't set so free-data mode still runs.
    fh_client = (
        fh.make_client(concurrency=min(concurrency, 5)) if s.finnhub_api_key else None
    )
    pg_client = (
        pg._client(concurrency=concurrency)
        if use_options and s.polygon_api_key
        else None
    )

    async with sec_client, gd_client:
        for opt in (fh_client, pg_client):
            if opt is not None:
                await opt.__aenter__()
        try:
            await asyncio.gather(
                _run_sec(sec_client, session, tickers, ciks, as_of, out, concurrency),
                _run_finnhub(fh_client, session, tickers, as_of, out, concurrency),
                _run_gdelt(gd_client, session, tickers, names, as_of, out, concurrency),
                _run_options(pg_client, tickers, out, concurrency),
            )
        finally:
            for opt in (fh_client, pg_client):
                if opt is not None:
                    await opt.__aexit__(None, None, None)

    calls = sum(
        c.calls_made
        for c in (sec_client, fh_client, gd_client, pg_client)
        if c is not None
    )
    log.info("stage3_enrichment_complete", tickers=len(tickers), api_calls=calls)
    for t in out:
        out[t]["_api_calls"] = 0
    if out:
        first = next(iter(out))
        out[first]["_api_calls"] = calls
    return out


async def _run_sec(client, session, tickers, ciks, as_of, out, concurrency):
    since = as_of - dt.timedelta(days=45)

    async def one(ticker: str):
        cik = ciks.get(ticker)
        if not cik or (isinstance(cik, float) and np.isnan(cik)):
            return
        payload = await sec.fetch_submissions(client, cik)
        events = sec.extract_filing_events(payload, ticker, since, as_of)
        if events:
            repository.save_filings(session, events)
        out[ticker]["filings_raw"] = events

    results = await gather_bounded([one(t) for t in tickers], concurrency)
    _log_failures("sec", results)
    session.flush()


async def _run_finnhub(client, session, tickers, as_of, out, concurrency):
    if client is None:  # no FINNHUB_API_KEY -- estimates/insiders skipped
        return
    since = as_of - dt.timedelta(days=90)

    async def one(ticker: str):
        reco = await fh.fetch_recommendation_trend(client, ticker)
        eps = await fh.fetch_eps_estimates(client, ticker)
        rev = await fh.fetch_revenue_estimates(client, ticker)
        insiders = await fh.fetch_insider_transactions(client, ticker, since)
        if insiders:
            repository.replace_insider_transactions(session, ticker, insiders)
        snapshot = {
            "ticker": ticker,
            "captured_on": as_of,
            "horizon": "fq1",
            "eps_consensus": eps.get("eps_consensus"),
            "revenue_consensus": rev.get("revenue_consensus"),
            "n_analysts": eps.get("eps_analysts"),
            "reco_strong_buy": reco.get("reco_strong_buy"),
            "reco_buy": reco.get("reco_buy"),
            "reco_hold": reco.get("reco_hold"),
            "reco_sell": reco.get("reco_sell"),
            "reco_strong_sell": reco.get("reco_strong_sell"),
        }
        repository.save_estimates(session, [snapshot])
        out[ticker]["reco"] = reco
        out[ticker]["estimates"] = {**eps, **rev}

    results = await gather_bounded([one(t) for t in tickers], min(concurrency, 5))
    _log_failures("finnhub", results)
    session.flush()


async def _run_gdelt(client, session, tickers, names, as_of, out, concurrency):
    async def one(ticker: str):
        name = names.get(ticker) or ticker
        bundle = await gd.fetch_news_bundle(client, ticker, str(name), as_of)
        out[ticker]["news"] = bundle
        repository.save_news(
            session,
            [
                {
                    "ticker": ticker,
                    "as_of_date": as_of,
                    "volume_z": bundle.get("volume_z"),
                    "tone_avg": bundle.get("tone_avg"),
                    "tone_slope_7d": bundle.get("tone_slope_7d"),
                    "source_diversity": bundle.get("source_diversity"),
                    "article_count": bundle.get("article_count"),
                    "themes": bundle.get("themes"),
                    "headlines": bundle.get("headlines"),
                }
            ],
        )

    results = await gather_bounded([one(t) for t in tickers], min(concurrency, 4))
    _log_failures("gdelt", results)
    session.flush()


async def _run_options(client, tickers, out, concurrency):
    if client is None:
        return

    async def one(ticker: str):
        out[ticker]["options"] = await pg.fetch_options_snapshot(
            ticker, client=client
        )

    results = await gather_bounded([one(t) for t in tickers], concurrency)
    _log_failures("polygon_options", results)


def _log_failures(source: str, results: list) -> None:
    errors = [r for r in results if isinstance(r, Exception)]
    if errors:
        log.warning(
            "stage3_partial_failures",
            source=source,
            failed=len(errors),
            total=len(results),
            sample=str(errors[0])[:200],
        )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_catalysts(
    session: Session,
    tickers: Sequence[str],
    as_of: dt.date,
    enrichment: dict[str, dict[str, Any]],
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    """Combine SEC, news and options points into one catalyst score per name."""
    filings = get_filings(session, tickers, as_of, days=45)
    insiders = get_insider_transactions(session, tickers, as_of, days=90)

    rows = []
    detail: dict[str, dict[str, Any]] = {}
    for t in tickers:
        enr = enrichment.get(t, {})
        ins_pts, ins_detail = sec_events.score_insiders(insiders.get(t, []), as_of)
        fil_pts, fil_detail = sec_events.score_filings(filings.get(t, []))
        news_pts, news_detail = news_scorer.score_news(enr.get("news") or {})
        opt_pts, opt_detail = options_scorer.score_options(enr.get("options") or {})

        total = ins_pts + fil_pts + news_pts + opt_pts
        rows.append(
            {
                "ticker": t,
                "catalyst_score": total,
                "insider_points": ins_pts,
                "filing_points": fil_pts,
                "news_points": news_pts,
                "options_points": opt_pts,
                "has_dilution_filing": fil_detail.get("has_dilution_filing", False),
            }
        )
        detail[t] = {
            "insiders": ins_detail,
            "filings": fil_detail,
            "news": news_detail,
            "options": opt_detail,
            "reco": enr.get("reco") or {},
        }
    return pd.DataFrame(rows).set_index("ticker"), detail


def apply_risk_screen(
    session: Session,
    tickers: Sequence[str],
    as_of: dt.date,
    trend_features: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    fetch_short_interest: bool = True,
) -> tuple[list[str], dict[str, list[str]], dict[str, dict[str, Any]]]:
    """Hard filters. Returns (survivors, rejects, detail)."""
    uni = universe.set_index("ticker") if "ticker" in universe.columns else universe
    next_earn = get_next_earnings(session, tickers, as_of)

    vols = trend_features["realised_vol_20d"].reindex(tickers)
    median_vol = float(vols.median()) if vols.notna().any() else None

    survivors: list[str] = []
    rejects: dict[str, list[str]] = {}
    detail: dict[str, dict[str, Any]] = {}

    for t in tickers:
        si: dict[str, float | None] = {}
        if fetch_short_interest:
            # Yahoo is fallback-only and wrapped: a failure yields {}, which
            # means "unknown", which never rejects.
            si = yahoo.fetch_short_interest(t)

        spread = estimate_spread_bps(
            _get(trend_features, t, "close"),
            _get(trend_features, t, "high_52w"),
            _get(trend_features, t, "low_52w"),
            _get(uni, t, "adv_20d"),
        )
        res = screen_name(
            t,
            short_interest_pct=si.get("short_interest_pct"),
            days_to_cover=si.get("days_to_cover"),
            next_earnings=next_earn.get(t),
            as_of=as_of,
            realised_vol_20d=_get(trend_features, t, "realised_vol_20d"),
            universe_median_vol=median_vol,
            spread_bps=spread,
        )
        detail[t] = res.detail
        if res.passed:
            survivors.append(t)
        else:
            rejects[t] = res.reasons

    log.info(
        "stage3_risk_screen", entry=len(tickers), exit=len(survivors),
        rejected=len(rejects),
    )
    return survivors, rejects, detail


def _get(df: pd.DataFrame, ticker: str, col: str) -> float | None:
    if col not in df.columns or ticker not in df.index:
        return None
    v = df.at[ticker, col]
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else f


def combine_and_rank(
    factor_scores: pd.DataFrame,
    catalyst_scores: pd.DataFrame,
    macro: MacroState,
    *,
    take: int,
    catalyst_weight: float = 0.35,
) -> pd.DataFrame:
    """Rank on factor_composite + catalyst_score with the regime sector tilt.

    The catalyst score is z-scored across the survivor set first, so it is on
    the same scale as the factor composite and the blend weight means what it
    says.
    """
    df = factor_scores.join(catalyst_scores, how="inner")
    if df.empty:
        return df

    cat = df["catalyst_score"].astype(float)
    sd = cat.std(ddof=1)
    df["catalyst_z"] = (cat - cat.mean()) / sd if sd and np.isfinite(sd) and sd > 0 else 0.0

    df["sector_tilt"] = df["sector"].map(macro.sector_tilt).fillna(1.0)
    df["regime_fit"] = df["sector"].map(macro.regime_fit).fillna(0.5)

    blended = (
        df["factor_composite"] * (1 - catalyst_weight)
        + df["catalyst_z"] * catalyst_weight
    )
    # The macro layer adjusts sector weight, it does not pick names. Apply the
    # tilt ADDITIVELY: (tilt - 1) is positive for a favoured sector and negative
    # for a suppressed one, so the nudge points the right way regardless of the
    # sign of `blended`. A multiplicative tilt (blended * tilt) is wrong here --
    # blended is a z-score and goes negative, and scaling a negative score up by
    # 1.15 would penalise the very sector the regime favours.
    df["stage3_score"] = blended + (df["sector_tilt"] - 1.0) * REGIME_TILT_STRENGTH

    df = df.sort_values("stage3_score", ascending=False)
    return df.head(take)


def checkpoint_payload(result: Stage3Result) -> dict[str, Any]:
    """Everything Stage 4 needs, so a failed LLM stage does not force a re-run
    of the ~1600 API calls Stage 3 just made."""
    return {
        "selected": result.selected.reset_index().to_dict(orient="records"),
        "all_scored": result.all_scored.reset_index().to_dict(orient="records"),
        "detail": result.detail,
        "rejected": result.rejected,
        "api_calls": result.api_calls,
    }


def restore_from_checkpoint(payload: Any) -> Stage3Result | None:
    """Rebuild a Stage3Result from a stored checkpoint. None if unusable."""
    if not isinstance(payload, dict) or not payload.get("selected"):
        return None
    try:
        selected = pd.DataFrame(payload["selected"]).set_index("ticker")
        all_scored = (
            pd.DataFrame(payload.get("all_scored") or payload["selected"])
            .set_index("ticker")
        )
    except (KeyError, ValueError) as exc:
        log.warning("stage3_checkpoint_unreadable", error=str(exc)[:200])
        return None
    return Stage3Result(
        selected=selected,
        all_scored=all_scored,
        rejected=payload.get("rejected") or {},
        detail=payload.get("detail") or {},
        api_calls=int(payload.get("api_calls") or 0),
    )


async def run_stage3(
    session: Session,
    factor_scores: pd.DataFrame,
    trend_features: pd.DataFrame,
    universe: pd.DataFrame,
    as_of: dt.date,
    macro: MacroState,
    *,
    take: int | None = None,
    use_options: bool = True,
) -> Stage3Result:
    s = get_settings()
    take = take or s.stage3_take
    tickers = list(factor_scores.index)

    enrichment = await enrich_tickers(
        session, tickers, universe, as_of,
        concurrency=s.http_concurrency, use_options=use_options,
    )
    api_calls = sum(e.get("_api_calls", 0) for e in enrichment.values())

    catalyst_scores, detail = score_catalysts(session, tickers, as_of, enrichment)

    survivors, rejects, screen_detail = apply_risk_screen(
        session, tickers, as_of, trend_features, universe
    )
    for t, d in screen_detail.items():
        detail.setdefault(t, {})["risk"] = d

    # A shelf/secondary registration is dilution incoming -- treat it as a
    # reject, not merely a negative score.
    for t in list(survivors):
        if bool(catalyst_scores.at[t, "has_dilution_filing"]):
            survivors.remove(t)
            rejects.setdefault(t, []).append("shelf/secondary registration filed")

    scored = combine_and_rank(
        factor_scores.loc[survivors], catalyst_scores.loc[survivors], macro, take=take
    )
    all_scored = combine_and_rank(
        factor_scores.loc[survivors],
        catalyst_scores.loc[survivors],
        macro,
        take=len(survivors),
    )

    log.info("stage3_complete", entry=len(tickers), exit=len(scored))
    return Stage3Result(
        selected=scored,
        all_scored=all_scored,
        rejected=rejects,
        detail=detail,
        api_calls=api_calls,
    )
