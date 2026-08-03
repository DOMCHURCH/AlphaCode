"""The orchestrator.

Runs the cascade, checkpoints after every stage, enforces the data-quality
gates, and tracks cost. If Stage 4 fails, `resume_from` replays from the Stage 3
checkpoint instead of re-running the whole funnel.

Every stage logs entry count, exit count, duration and API calls made.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import structlog

from src.catalysts import stage3 as s3
from src.catalysts.macro import load_macro_state
from src.config.factor_weights import (
    MODE_FULL,
    MODE_LABEL,
    MODE_MOMENTUM_ONLY,
    mode_config,
)
from src.config.settings import get_settings
from src.core.stage_timeout import StageTimeout, enforce_stage_timeout
from src.factors import composite, trend
from src.ingest.rate_limiter import get_rate_limiter
from src.llm import deep_dive, triage
from src.llm.client import verify_configured_models
from src.llm.cost import CostTracker
from src.llm.packets import build_deep_packet, build_triage_packet
from src.llm.schemas import DeepDive
from src.logging_config import configure_logging, peak_rss_mb
from src.report.builder import build_report
from src.storage import repository
from src.storage.db import session_scope
from src.storage.pit import assert_no_lookahead, get_next_earnings, get_universe
from src.universe.builder import build_universe
from src.util import or_default

log = structlog.get_logger(__name__)


class DataQualityError(RuntimeError):
    """A gate tripped. We abort and alert rather than ship a bad report."""


@dataclass
class StageTiming:
    stage: int
    name: str
    entry: int
    exit: int
    duration_s: float
    api_calls: int = 0


@dataclass
class PipelineResult:
    run_id: str
    as_of: dt.date
    regime: str
    funnel_counts: dict[str, int]
    dives: list[DeepDive]
    report_paths: dict[str, str] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    timings: list[StageTiming] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"


@contextmanager
def _stage(name: str, number: int, entry: int, timings: list) -> Iterator[dict]:
    # Reset the write ledger on entry so it reflects only THIS stage; log its
    # rows_written/attempted on exit (every stage, even those that only write a
    # checkpoint) and abort on a wholesale zero -- unless the body already raised,
    # so we never mask the real error with the write check.
    repository.reset_write_ledger()
    t0 = time.perf_counter()
    box: dict[str, Any] = {"exit": 0, "api_calls": 0}
    log.info("stage_start", stage=number, name=name, entry=entry,
             rss_mb=peak_rss_mb())
    raised = False
    try:
        yield box
    except BaseException:
        raised = True
        raise
    finally:
        dur = time.perf_counter() - t0
        timings.append(
            StageTiming(number, name, entry, box["exit"], dur, box["api_calls"])
        )
        # rss_mb is the process high-water mark AFTER this stage -- watch it climb
        # stage to stage to see exactly where an OOM allocates.
        log.info(
            "stage_end", stage=number, name=name, entry=entry,
            exit=box["exit"], duration_s=round(dur, 2), api_calls=box["api_calls"],
            rss_mb=peak_rss_mb(),
        )
        stats = repository.get_write_ledger()
        if stats:
            log.info(
                "stage_writes", stage=f"{number} {name}",
                writes={t: f"{d['written']}/{d['attempted']}" for t, d in stats.items()},
            )
        if not raised:
            dead = repository.assert_writes(min_attempts=100)
            if dead:
                detail = ", ".join(f"{t} (0/{stats[t]['attempted']})" for t in dead)
                raise DataQualityError(
                    f"Stage {number} {name}: wrote 0 rows to {detail} despite "
                    f"attempting >100 -- a persist is silently failing, not returning "
                    f"empty. Aborting rather than continuing with missing data."
                )


def _check_drop(name: str, entry: int, exit_: int, warnings: list[str]) -> None:
    """Abort if a stage drops more than 95% of its input.

    Stage 1 legitimately cuts ~80%, Stage 2 ~65%. A 95%+ drop means a data
    problem, not a market signal.
    """
    if entry == 0:
        return
    dropped = 1 - exit_ / entry
    if dropped > 0.95:
        raise DataQualityError(
            f"Stage {name} dropped {dropped:.1%} of its input "
            f"({entry} -> {exit_}). Aborting rather than shipping a bad report."
        )
    if dropped > 0.90:
        warnings.append(
            f"Stage {name} dropped {dropped:.1%} of input ({entry} -> {exit_})."
        )


def _completeness_gate(comp, threshold: float) -> float:
    """Abort if mean per-name factor completeness is below `threshold`.

    A ranking scored on mostly-missing factors is not defensible. The error names
    WHICH factors are empty (e.g. revisions with no FINNHUB key, fundamentals that
    didn't join) so the operator fixes the data instead of the number. Returns the
    mean completeness when it passes.
    """
    mean_comp = float(comp.scores["data_completeness"].mean())
    if mean_comp < threshold:
        from src.config.factor_weights import (
            PAID_ONLY_FACTORS,
            SEC_SUPPLIABLE_FACTORS,
        )

        cov = comp.factor_coverage or {}
        empty = {f for f, c in cov.items() if c < 0.05}
        # Split the empty factors so the operator knows what they're looking at:
        # a data-loading gap they can fix for free, vs a paid-feed spend decision.
        sec_gap = sorted(empty & SEC_SUPPLIABLE_FACTORS)
        paid_gap = sorted(empty & PAID_ONLY_FACTORS)
        thin = ", ".join(f"{f}={c:.0%}" for f, c in sorted(cov.items()))
        raise DataQualityError(
            f"Stage 2 mean factor completeness {mean_comp:.1%} < {threshold:.0%} "
            f"floor -- the ranking would rest on mostly-missing data.\n"
            f"  FREE, fix by loading data (SEC XBRL): {', '.join(sec_gap) or 'none'}. "
            f"Run /backfill?fundamentals=true (quality+value); pead needs an earnings "
            f"backfill.\n"
            f"  PAID, spend decision (no free source, needs Finnhub): "
            f"{', '.join(paid_gap) or 'none'}.\n"
            f"  Per-factor coverage: {thin}.\n"
            f"Fix the data before shipping a ranking; not tuning the floor down."
        )
    return mean_comp


def _check_stage_writes(stage_name: str) -> dict[str, dict[str, int]]:
    """Log rows_written vs rows_attempted for the stage, and abort if a table was
    tried in bulk but wrote nothing.

    A stage that attempts >100 writes to a table and lands 0 is a silent write
    failure (a swallowed upsert error, e.g. the `items` reserved-name bug), not an
    empty result -- so we fail the run loudly rather than let it complete having
    persisted nothing it tried to. Call `repository.reset_write_ledger()` before
    the stage's persistence so the ledger reflects only this stage.
    """
    stats = repository.get_write_ledger()
    if stats:
        log.info(
            "stage_writes", stage=stage_name,
            writes={t: f"{s['written']}/{s['attempted']}" for t, s in stats.items()},
        )
    dead = repository.assert_writes(min_attempts=100)
    if dead:
        detail = ", ".join(
            f"{t} (0/{stats[t]['attempted']})" for t in dead
        )
        raise DataQualityError(
            f"{stage_name}: wrote 0 rows to {detail} despite attempting >100 -- a "
            f"persist is silently failing, not returning empty. Aborting rather "
            f"than continuing with missing data."
        )
    return stats


def _history_depth(session, as_of: dt.date) -> int:
    """Distinct trading days of price history stored on/before `as_of`.

    The ceiling on how much history any ticker can have -- if the store holds
    fewer than a year of sessions, no name can be evaluated on the full-year
    windows the trend gate uses.
    """
    from sqlalchemy import func, select

    from src.storage.models import DailyBar

    return int(
        session.execute(
            select(func.count(func.distinct(DailyBar.date))).where(
                DailyBar.date <= as_of
            )
        ).scalar_one()
        or 0
    )


async def run_pipeline(
    as_of: dt.date | None = None,
    *,
    run_id: str | None = None,
    resume_from: int = 0,
    skip_llm: bool = False,
    persist_universe: bool = True,
    mode: str = MODE_FULL,
) -> PipelineResult:
    """`mode` selects the scoring artifact this run produces.

    MODE_FULL is the real funnel: all 19 factors, and the completeness floor
    applies unchanged. MODE_MOMENTUM_ONLY scores on the 3 price-derived momentum
    factors and skips the completeness gate BY DESIGN -- it is a separate,
    labelled artifact for use while fundamentals load, never a way to get a full
    run past the floor. Every output it produces carries MODE_LABEL.
    """
    configure_logging()
    mode_config(mode)  # validate early: an unknown mode fails before any work
    s = get_settings()
    as_of = as_of or _last_trading_day()
    run_id = run_id or f"{as_of.isoformat()}-{uuid.uuid4().hex[:8]}"

    structlog.contextvars.bind_contextvars(run_id=run_id, as_of=str(as_of))
    limiter = get_rate_limiter()
    limiter.reset_counts()

    timings: list[StageTiming] = []
    warnings: list[str] = []
    cost = CostTracker()
    funnel: dict[str, int] = {}
    stage_sectors: dict[str, dict[str, int]] = {}
    funnel_rejects: dict[str, dict[str, int]] = {}
    t_start = time.perf_counter()

    with session_scope() as session:
        repository.start_run(session, run_id, as_of, mode=mode)

    try:
        # ---------------- model verification, before anything expensive -----
        # A cron run must ALWAYS produce a deterministic ranked list. So if the
        # LLM is misconfigured (bad OPENROUTER_API_KEY / unresolvable model id),
        # we do not abort -- we degrade to the deterministic ranking and say so.
        # This is not weakening a gate: the deterministic Stages 0-3 still run in
        # full and any data-quality abort still propagates.
        run_llm = not skip_llm
        model_info: dict[str, Any] = {}
        if run_llm:
            try:
                model_info = await verify_configured_models()
            except Exception as exc:  # noqa: BLE001 - includes ModelNotAvailable
                run_llm = False
                warnings.append(
                    f"LLM unavailable at startup ({str(exc)[:180]}); produced the "
                    f"deterministic ranking instead of model write-ups."
                )
                log.warning("llm_unavailable_deterministic_fallback", error=str(exc)[:200])

        with session_scope() as session:
            # ---------------- Stage 0: universe --------------------------
            with _stage("universe", 0, 0, timings) as box:
                if resume_from > 0:
                    universe = get_universe(session, as_of)
                else:
                    universe, diag = await build_universe(
                        as_of, session, persist_bars=persist_universe
                    )
                    funnel_rejects["Stage 0 universe"] = diag.get("rejects", {})
                box["exit"] = len(universe)
                funnel["Stage 0 universe"] = len(universe)
                stage_sectors["Stage 0"] = _sector_counts(universe)

            if len(universe) < s.min_universe_size:
                raise DataQualityError(
                    f"Universe is {len(universe)} names, below the {s.min_universe_size} "
                    f"floor. Aborting -- this is a data problem, not a market."
                )

            tickers = universe["ticker"].tolist()
            sectors = universe.set_index("ticker")["sector"]

            # Hard block: the trend gate is only honest with a full year of
            # history (200-day SMA + its slope, 52-week high, 12-month return).
            # On less, we do NOT run degraded -- we stop and surface the
            # shortfall so the operator/UI knows to finish the backfill first.
            depth = _history_depth(session, as_of)
            need = s.min_history_days
            if depth < need:
                raise DataQualityError(
                    f"insufficient history: {depth}/{need} trading days. "
                    f"The trend gate needs a full year; finish the backfill "
                    f"before running (not running degraded)."
                )

            # ---------------- Stage 1: trend gate ------------------------
            with _stage("trend_gate", 1, len(tickers), timings) as box:
                # Split the stage timing into DB-load vs compute. The vectorised
                # trend/residual-momentum math benchmarks at ~0.1s; if Stage 1
                # takes seconds it is the Postgres panel read (600d x ~5k tickers),
                # not the compute. Logging both settles which is which on deploy
                # instead of guessing that "vectorisation isn't running".
                _t_load = time.perf_counter()
                panels = trend.load_panels(session, tickers, as_of)
                _load_s = round(time.perf_counter() - _t_load, 2)
                _t_comp = time.perf_counter()
                tr = trend.run_trend_gate(
                    panels["close"], panels["high"], panels["low"], panels["volume"],
                    sectors=sectors,
                )
                _compute_s = round(time.perf_counter() - _t_comp, 2)
                log.info(
                    "stage1_timing_split",
                    panel_load_s=_load_s,
                    compute_s=_compute_s,
                    tickers=len(tickers),
                    panel_rows=int(panels["close"].shape[0] * panels["close"].shape[1])
                    if not panels["close"].empty else 0,
                )
                box["exit"] = len(tr.survivors)
                funnel["Stage 1 trend"] = len(tr.survivors)
                funnel_rejects["Stage 1 trend"] = tr.reject_counts
                stage_sectors["Stage 1"] = _sector_counts_from_index(
                    tr.survivors.index, sectors
                )
                repository.save_checkpoint(
                    session, run_id, as_of, 1, entry_count=len(tickers),
                    exit_count=len(tr.survivors), duration_s=timings[-1].duration_s
                    if timings else 0.0, api_calls=0,
                    payload={"survivors": list(tr.survivors.index), "regime": tr.regime},
                    rejected=tr.reject_counts,
                )
            _check_drop("1 trend", len(tickers), len(tr.survivors), warnings)
            if tr.regime == "RISK_OFF":
                warnings.append(
                    "REGIME: RISK_OFF — fewer than 300 names passed the strict "
                    "trend gate; the RS threshold was relaxed to 50."
                )

            # ---------------- Stage 2: multi-factor composite ------------
            with _stage("factor_composite", 2, len(tr.survivors), timings) as box:
                assert_no_lookahead(session, as_of)
                comp = composite.run_stage2(
                    session, tr.survivors, universe, as_of, take=s.stage2_take,
                    mode=mode,
                )
                box["exit"] = len(comp.selected)
                funnel["Stage 2 factors"] = len(comp.selected)
                stage_sectors["Stage 2"] = _sector_counts_from_index(
                    pd.Index(comp.selected), sectors
                )
                repository.save_checkpoint(
                    session, run_id, as_of, 2, entry_count=len(tr.survivors),
                    exit_count=len(comp.selected),
                    duration_s=timings[-1].duration_s if timings else 0.0,
                    api_calls=0, payload={"selected": comp.selected},
                )
            _check_drop("2 factors", len(tr.survivors), len(comp.selected), warnings)

            # Completeness gate: a composite built on mostly-NaN factors is not a
            # defensible ranking. Abort (don't ship) with the per-factor coverage.
            # MOMENTUM-ONLY bypasses it BY DESIGN -- it is not claiming to be the
            # full composite, and it says so on every artifact it produces. The
            # floor is untouched and still applies in full to MODE_FULL.
            if mode == MODE_MOMENTUM_ONLY:
                warnings.append(MODE_LABEL[MODE_MOMENTUM_ONLY])
                log.warning(
                    "completeness_gate_bypassed_by_design",
                    mode=mode,
                    label=MODE_LABEL[MODE_MOMENTUM_ONLY],
                    # The honest full-composite number, so the bypass is never
                    # mistaken for the data being complete.
                    full_completeness=round(comp.full_completeness, 3),
                    floor_unchanged=s.min_mean_completeness,
                )
            else:
                _completeness_gate(comp, s.min_mean_completeness)

            # ---------------- macro regime -------------------------------
            macro = await load_macro_state(session, as_of)

            # ---------------- Stage 3: catalysts -------------------------
            # This is the expensive stage (~1600 API calls), so it is the one
            # worth resuming. Stages 1-2 make no API calls and take seconds, so
            # they are always recomputed rather than restored.
            factor_scores = comp.scores.loc[comp.selected]
            with _stage("catalysts", 3, len(comp.selected), timings) as box:
                st3 = None
                if resume_from > 3:
                    st3 = s3.restore_from_checkpoint(
                        repository.load_checkpoint(session, run_id, 3)
                    )
                    if st3 is None:
                        log.warning(
                            "stage3_checkpoint_missing_rerunning", run_id=run_id
                        )
                    else:
                        log.info(
                            "stage3_restored_from_checkpoint",
                            names=len(st3.selected),
                        )
                if st3 is None:
                    # Stage 3 fans out per-ticker persistence (filings, insiders,
                    # estimates, news) through gather_bounded, which isolates a
                    # failing ticker into a warning -- so a SYSTEMATIC persist bug
                    # would silently zero a whole table. The _stage wrapper's write
                    # ledger catches that: it aborts if a table attempted >100 and
                    # wrote 0.
                    try:
                        st3 = await enforce_stage_timeout(
                            s3.run_stage3(
                                session, factor_scores, tr.survivors, universe, as_of, macro,
                                take=s.stage3_take,
                            ),
                            stage=3,
                            stage_name="catalysts",
                            timeout_s=s.stage3_timeout_s or 300,
                        )
                    except StageTimeout as exc:
                        log.error(
                            "stage3_timeout",
                            stage=exc.stage,
                            stage_name=exc.stage_name,
                            elapsed_s=exc.elapsed_s,
                            timeout_s=exc.timeout_s,
                        )
                        raise DataQualityError(
                            f"Stage {exc.stage} {exc.stage_name} timed out after "
                            f"{exc.elapsed_s:.0f}s (limit: {exc.timeout_s}s). "
                            f"Check SEC/Finnhub/GDELT for stalls."
                        ) from exc
                box["exit"] = len(st3.selected)
                box["api_calls"] = st3.api_calls
                funnel["Stage 3 catalysts"] = len(st3.selected)
                funnel_rejects["Stage 3 catalysts"] = _reason_counts(st3.rejected)
                stage_sectors["Stage 3"] = _sector_counts_from_index(
                    st3.selected.index, sectors
                )
                repository.save_checkpoint(
                    session, run_id, as_of, 3, entry_count=len(comp.selected),
                    exit_count=len(st3.selected),
                    duration_s=timings[-1].duration_s if timings else 0.0,
                    api_calls=st3.api_calls,
                    payload=s3.checkpoint_payload(st3),
                    rejected=st3.rejected,
                )
            _check_drop("3 catalysts", len(comp.selected), len(st3.selected), warnings)

            # ---------------- Stage 4-5: LLM triage + deep dive ----------
            dives: list[DeepDive] = []
            triage_df = pd.DataFrame()
            deterministic_top: list[dict[str, Any]] = []

            def _deterministic_fallback(reason: str) -> list[dict[str, Any]]:
                warnings.append(reason)
                top = _deterministic_ranking(st3.selected, sectors, s.stage5_take)
                funnel["Stage 5 final"] = len(top)
                return top

            if not run_llm:
                deterministic_top = _deterministic_fallback(
                    "LLM stages skipped — the ranking below is the deterministic "
                    "Stages 0-3 output, with no thesis and no model scoring."
                )
            else:
                # A cron run must always produce a deterministic ranked list: if
                # the LLM stages fail at runtime, fall back to the deterministic
                # ranking. A data-quality abort still propagates (re-raised).
                try:
                    with _stage("llm_triage", 4, len(st3.selected), timings) as box:
                        packets = [
                            build_triage_packet(
                                t, st3.selected.loc[t], tr.survivors.loc[t],
                                comp.raw.loc[t], st3.detail.get(t, {}), macro,
                            )
                            for t in st3.selected.index
                        ]
                        try:
                            tri = await enforce_stage_timeout(
                                triage.run_triage(
                                    packets, st3.selected, take=s.stage4_take,
                                    model_info=model_info.get(s.llm_triage_model),
                                ),
                                stage=4,
                                stage_name="llm_triage",
                                timeout_s=s.llm_triage_timeout_s or 600,
                            )
                        except StageTimeout as exc:
                            log.error(
                                "stage4_timeout",
                                stage=exc.stage,
                                stage_name=exc.stage_name,
                                elapsed_s=exc.elapsed_s,
                                timeout_s=exc.timeout_s,
                            )
                            raise DataQualityError(
                                f"Stage {exc.stage} {exc.stage_name} timed out after "
                                f"{exc.elapsed_s:.0f}s (limit: {exc.timeout_s}s). "
                                f"LLM model may be unreachable or overloaded."
                            ) from exc
                        cost.record("triage", tri.usage)
                        triage_df = tri.verdicts
                        selected_25 = tri.selected
                        box["exit"] = len(selected_25)
                        box["api_calls"] = tri.llm_calls
                        funnel["Stage 4 triage"] = len(selected_25)
                        stage_sectors["Stage 4"] = _sector_counts_from_index(
                            pd.Index(selected_25), sectors
                        )
                        repository.save_checkpoint(
                            session, run_id, as_of, 4, entry_count=len(st3.selected),
                            exit_count=len(selected_25),
                            duration_s=timings[-1].duration_s if timings else 0.0,
                            api_calls=tri.llm_calls, payload={"selected": selected_25},
                        )

                    # ---------------- Stage 5: LLM deep dive -------------
                    with _stage("llm_deep_dive", 5, len(selected_25), timings) as box:
                        quarterlies = _load_quarterlies(session, selected_25, as_of)
                        next_earn = get_next_earnings(session, selected_25, as_of)
                        deep_packets = [
                            build_deep_packet(
                                t, st3.selected.loc[t], tr.survivors.loc[t],
                                comp.raw.loc[t], st3.detail.get(t, {}), macro,
                                fundamentals=quarterlies.get(t),
                                sector_percentiles=_sector_percentiles(comp.scores, t),
                                next_earnings=next_earn.get(t),
                            )
                            for t in selected_25
                            if t in st3.selected.index
                        ]
                        take = min(s.stage5_take, macro.final_count(s.stage5_take))
                        try:
                            dd = await enforce_stage_timeout(
                                deep_dive.run_deep_dive(
                                    deep_packets,
                                    sectors=sectors.to_dict(),
                                    take=take,
                                    model_info=model_info.get(s.llm_deep_model),
                                ),
                                stage=5,
                                stage_name="llm_deep_dive",
                                timeout_s=s.llm_deep_dive_timeout_s or 900,
                            )
                        except StageTimeout as exc:
                            log.error(
                                "stage5_timeout",
                                stage=exc.stage,
                                stage_name=exc.stage_name,
                                elapsed_s=exc.elapsed_s,
                                timeout_s=exc.timeout_s,
                            )
                            raise DataQualityError(
                                f"Stage {exc.stage} {exc.stage_name} timed out after "
                                f"{exc.elapsed_s:.0f}s (limit: {exc.timeout_s}s). "
                                f"LLM model may be unreachable or overloaded."
                            ) from exc
                        cost.record("deep_dive", dd.usage)
                        dives = dd.final
                        box["exit"] = len(dives)
                        box["api_calls"] = dd.llm_calls
                        funnel["Stage 5 final"] = len(dives)
                        stage_sectors["Stage 5"] = _sector_counts_from_index(
                            pd.Index([d.ticker for d in dives]), sectors
                        )
                        if dd.failures:
                            warnings.append(
                                f"{len(dd.failures)} deep dives failed validation: "
                                + ", ".join(list(dd.failures)[:5])
                            )
                        repository.save_checkpoint(
                            session, run_id, as_of, 5, entry_count=len(selected_25),
                            exit_count=len(dives),
                            duration_s=timings[-1].duration_s if timings else 0.0,
                            api_calls=dd.llm_calls,
                            payload={"final": [d.ticker for d in dives]},
                        )
                        if macro.regime == "RISK_OFF":
                            warnings.append(
                                f"RISK_OFF regime — final list capped at {take} names."
                            )
                except DataQualityError:
                    raise
                except Exception as exc:  # noqa: BLE001 - LLM must not kill the run
                    log.warning(
                        "llm_stage_failed_deterministic_fallback", error=str(exc)[:300]
                    )
                    dives, triage_df = [], pd.DataFrame()
                    deterministic_top = _deterministic_fallback(
                        f"LLM stages failed mid-run ({str(exc)[:180]}); fell back to "
                        f"the deterministic ranking."
                    )

            if not cost.check_budget():
                warnings.append(
                    f"Run cost ${cost.total.cost_usd:.2f} exceeded the "
                    f"${s.max_run_cost_usd:.2f} budget."
                )

            # ---------------- persist scores and theses ------------------
            repository.reset_write_ledger()
            _persist_scores(
                session, as_of, tr, comp, st3, triage_df, dives, sectors,
                deterministic_top, mode=mode,
            )
            _check_stage_writes("persist scores")

            # ---------------- Stage 6: report ---------------------------
            duration = time.perf_counter() - t_start
            with _stage("report", 6, len(dives), timings) as box:
                near = _near_misses(st3, triage_df, sectors, exclude=[d.ticker for d in dives])
                paths = build_report(
                    session,
                    as_of=as_of, run_id=run_id, dives=dives,
                    scores=st3.selected, trend_features=tr.survivors,
                    detail=st3.detail, macro=macro,
                    funnel_counts=funnel, funnel_rejects=funnel_rejects,
                    stage_sectors=stage_sectors, near_misses=near,
                    api_calls=limiter.call_counts(), cost=cost.summary(),
                    duration_s=duration, warnings=warnings, mode=mode,
                    deterministic_names=deterministic_top,
                    fundamentals=_load_quarterlies(
                        session, [d.ticker for d in dives], as_of
                    ),
                )
                box["exit"] = len(dives)

            repository.finish_run(
                session, run_id, status="ok", regime=macro.regime,
                funnel_counts=funnel, api_calls=limiter.call_counts(),
                tokens_in=cost.total.prompt_tokens,
                tokens_out=cost.total.completion_tokens,
                cost_usd=cost.total.cost_usd, report_path=paths.get("html"),
            )

        total = time.perf_counter() - t_start
        if total > 12 * 60:
            warnings.append(f"Run took {total/60:.1f} min, over the 12 min target.")
        log.info(
            "pipeline_complete", duration_s=round(total, 1), funnel=funnel,
            cost_usd=round(cost.total.cost_usd, 4), names=len(dives),
        )
        return PipelineResult(
            run_id=run_id, as_of=as_of, regime=macro.regime, funnel_counts=funnel,
            dives=dives, report_paths=paths, cost=cost.summary(), timings=timings,
            warnings=warnings,
        )

    except Exception as exc:
        log.exception("pipeline_failed", error=str(exc))
        with session_scope() as session:
            repository.finish_run(
                session, run_id, status="failed", funnel_counts=funnel,
                api_calls=limiter.call_counts(), error=str(exc)[:2000],
            )
        raise
    finally:
        structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def resume_last(
    as_of: dt.date | None = None, *, skip_llm: bool = False
) -> PipelineResult:
    """Resume the most recent run for `as_of` from its furthest checkpoint.

    This is the entrypoint that makes the Stage 3 checkpoint worth writing: if a
    run died in the LLM stages, this re-uses the ~1600 Stage 3 API calls instead
    of repeating them. Resolves the resume point from what was actually
    checkpointed:

        Stage 3 checkpointed -> resume_from=4  (restore Stage 3, no enrichment)
        earlier only         -> resume_from=1  (reuse the universe snapshot,
                                                 recompute the cheap stages)

    Raises if there is no prior run to resume.
    """
    as_of = as_of or _last_trading_day()
    with session_scope() as session:
        run_id = repository.latest_run_id(session, as_of)
        if run_id is None:
            raise RuntimeError(
                f"No prior run for {as_of} to resume. Start a fresh run instead."
            )
        max_stage = repository.max_checkpoint_stage(session, run_id)

    resume_from = 4 if max_stage >= 3 else 1
    log.info(
        "resuming_run", run_id=run_id, as_of=str(as_of),
        max_checkpoint_stage=max_stage, resume_from=resume_from,
    )
    return await run_pipeline(
        as_of, run_id=run_id, resume_from=resume_from, skip_llm=skip_llm,
        persist_universe=False,
    )


def _last_trading_day(today: dt.date | None = None) -> dt.date:
    """Most recent completed session, holiday-aware where the calendar exists."""
    today = today or dt.date.today()
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("NYSE")
        sched = cal.schedule(
            start_date=today - dt.timedelta(days=14), end_date=today
        )
        days = [d.date() for d in sched.index]
        past = [d for d in days if d < today] or days
        return past[-1] if past else today - dt.timedelta(days=1)
    except Exception:  # noqa: BLE001 - fall back to weekday arithmetic
        d = today - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d


def is_trading_day(day: dt.date | None = None) -> bool:
    day = day or dt.date.today()
    try:
        import pandas_market_calendars as mcal

        cal = mcal.get_calendar("NYSE")
        sched = cal.schedule(start_date=day, end_date=day)
        return len(sched) > 0
    except Exception:  # noqa: BLE001
        return day.weekday() < 5


def _sector_counts(universe: pd.DataFrame) -> dict[str, int]:
    if universe.empty or "sector" not in universe.columns:
        return {}
    return universe["sector"].fillna("Unknown").value_counts().to_dict()


def _sector_counts_from_index(idx: pd.Index, sectors: pd.Series) -> dict[str, int]:
    if len(idx) == 0:
        return {}
    return sectors.reindex(idx).fillna("Unknown").value_counts().to_dict()


def _reason_counts(rejected: dict[str, list[str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reasons in rejected.values():
        for r in reasons:
            key = r.split(":")[0].split("(")[0].strip()
            counts[key] = counts.get(key, 0) + 1
    return counts


def _deterministic_ranking(
    stage3: pd.DataFrame, sectors: pd.Series, take: int
) -> list[dict[str, Any]]:
    """The Stages 0-3 top-N, for when the LLM layer is skipped.

    Carries scores and the factor breakdown but deliberately no thesis text --
    there is no model, so inventing prose here would be dishonest.
    """
    rows = []
    for rank, (ticker, r) in enumerate(stage3.head(take).iterrows(), start=1):
        rows.append(
            {
                "rank": rank,
                "ticker": ticker,
                "sector": or_default(sectors.get(ticker), "Unknown"),
                "stage3_score": _num(r.get("stage3_score")),
                "factor_composite": _num(r.get("factor_composite")),
                "catalyst_score": _num(r.get("catalyst_score")),
                "categories": {
                    c: _num(r.get(c))
                    for c in ("momentum", "quality", "revisions", "pead", "value")
                },
                "data_completeness": _num(r.get("data_completeness")),
            }
        )
    return rows


def _sector_percentiles(scores: pd.DataFrame, ticker: str) -> dict[str, float]:
    from src.factors.crosssection import sector_percentile

    if ticker not in scores.index or "sector" not in scores.columns:
        return {}
    out = {}
    for cat in ("momentum", "quality", "revisions", "pead", "value"):
        if cat in scores.columns:
            pct = sector_percentile(scores[cat], scores["sector"])
            v = pct.get(ticker)
            if v is not None and np.isfinite(v):
                out[cat] = float(v)
    return out


def _load_quarterlies(
    session, tickers: list[str], as_of: dt.date
) -> dict[str, pd.DataFrame]:
    from src.factors.fundamentals import load_quarterly_wide

    if not tickers:
        return {}
    return load_quarterly_wide(session, tickers, as_of, quarters=8)


def _near_misses(
    st3: s3.Stage3Result,
    triage_df: pd.DataFrame,
    sectors: pd.Series,
    exclude: list[str],
    n: int = 15,
) -> list[dict[str, Any]]:
    """Ranks 11-25: what nearly made it, and why it did not."""
    rows: list[dict[str, Any]] = []
    if not triage_df.empty:
        for t, r in triage_df.iterrows():
            if t in exclude:
                continue
            rows.append(
                {
                    "ticker": t,
                    "sector": or_default(sectors.get(t), "Unknown"),
                    "score": float(r.get("llm_triage_score", 0)),
                    "why": str(r.get("why", "")),
                }
            )
        rows.sort(key=lambda r: r["score"], reverse=True)
    else:
        for t in st3.selected.index:
            if t in exclude:
                continue
            rows.append(
                {
                    "ticker": t,
                    "sector": or_default(sectors.get(t), "Unknown"),
                    "score": float(st3.selected.at[t, "stage3_score"]),
                    "why": "deterministic stage-3 rank",
                }
            )
    return rows[:n]


def _persist_scores(
    session, as_of: dt.date, tr, comp, st3, triage_df, dives, sectors,
    deterministic_top=None, mode: str = MODE_FULL,
) -> None:
    """Store every scored name so the IC tracker can measure this run later."""
    dive_by_ticker = {d.ticker: d for d in dives}
    final_order = {d.ticker: i + 1 for i, d in enumerate(dives)}

    rows = []
    for t in comp.scores.index:
        r = comp.scores.loc[t]
        stage = 2
        catalyst = None
        if t in st3.selected.index:
            stage = 3
            catalyst = float(st3.selected.at[t, "catalyst_score"])
        tri_score = None
        if not triage_df.empty and t in triage_df.index:
            stage = 4
            tri_score = float(triage_df.at[t, "llm_triage_score"])
        dive = dive_by_ticker.get(t)
        if dive:
            stage = 5
        rows.append(
            {
                "as_of_date": as_of,
                "ticker": t,
                "sector": sectors.get(t),
                "sector_source": (
                    str(r.get("sector_source"))
                    if pd.notna(r.get("sector_source"))
                    else "unknown"
                ),
                "stage_reached": stage,
                # Stamped on every stored score so IC can separate momentum-only
                # runs from full runs and never pool the two.
                "mode": mode,
                "factor_composite": _num(r.get("factor_composite")),
                "catalyst_score": catalyst,
                "llm_triage_score": tri_score,
                "llm_total_score": float(dive.total_score) if dive else None,
                "final_rank": final_order.get(t),
                "factor_detail": {
                    k: _num(r.get(k))
                    for k in ("momentum", "quality", "revisions", "pead", "value")
                },
                "data_completeness": _num(r.get("data_completeness")),
            }
        )
    repository.save_scores(session, rows)

    repository.save_theses(
        session,
        [
            {
                "as_of_date": as_of,
                "ticker": d.ticker,
                "total_score": d.total_score,
                "subscores": d.subscores.model_dump(),
                "thesis": d.thesis,
                "bull_case": d.bull_case,
                "bear_case": d.bear_case,
                "invalidation": d.invalidation,
                "time_horizon_days": d.time_horizon_days,
                "conviction": d.conviction,
                "key_risks": d.key_risks,
                "catalysts_ahead": [c.model_dump() for c in d.catalysts_ahead],
            }
            for d in dives
        ],
    )

    # Fast mode (skip_llm): no DeepDives, so persist minimal theses from the
    # deterministic top-N so the site's picks list still populates. No prose --
    # just the funnel score (a bounded map of the composite z) and sector.
    if not dives and deterministic_top:
        repository.save_theses(
            session,
            [
                {
                    "as_of_date": as_of,
                    "ticker": d["ticker"],
                    "total_score": _deterministic_score(d.get("factor_composite")),
                    "subscores": None,
                    # The stored thesis is a user-facing output (it drives the
                    # site's picks list and /stock/<ticker>), so a partial-data
                    # mode must say so HERE too -- not only on the report page.
                    "thesis": (
                        (MODE_LABEL[mode] + " ") if MODE_LABEL.get(mode) else ""
                    ) + (
                        "Fast mode — ranked by the deterministic funnel "
                        "(Stages 0-3); no model write-up."
                    ),
                    "conviction": None,
                    "key_risks": [],
                    "catalysts_ahead": [],
                }
                for d in deterministic_top
            ],
        )


def _deterministic_score(composite: float | None) -> int:
    """Map a composite z-score to a bounded 0-100 funnel score for fast mode."""
    z = composite if isinstance(composite, (int, float)) else 0.0
    return int(max(5, min(99, round(50 + 12 * z))))


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(f) else f


def _cli() -> None:
    """Manual trigger. `--resume` continues the last run for the date from its
    furthest checkpoint; otherwise a fresh run is started."""
    import argparse

    parser = argparse.ArgumentParser(description="Run or resume the alpha funnel")
    parser.add_argument("--date", default=None, help="as-of date YYYY-MM-DD")
    parser.add_argument(
        "--run-id", default=None,
        help="use this run_id instead of generating one (lets a parent process "
        "that spawned this run reconcile its RunLog if it dies)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="resume the last run for the date from its furthest checkpoint",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="run Stages 0-3 only; report the deterministic ranking",
    )
    parser.add_argument(
        "--mode", default=MODE_FULL, choices=[MODE_FULL, MODE_MOMENTUM_ONLY],
        help="scoring mode. 'momentum_only' scores on the 3 price factors and "
        "skips the completeness gate BY DESIGN; every artifact it produces is "
        "labelled as such. It is a separate thing, not a degraded full run.",
    )
    args = parser.parse_args()

    configure_logging()
    from src.storage.db import init_db

    init_db()
    as_of = dt.date.fromisoformat(args.date) if args.date else None

    import asyncio

    if args.resume:
        result = asyncio.run(resume_last(as_of, skip_llm=args.skip_llm))
    else:
        result = asyncio.run(
            run_pipeline(
                as_of, run_id=args.run_id, skip_llm=args.skip_llm, mode=args.mode
            )
        )

    print(
        f"\nrun {result.run_id} | regime {result.regime} | "
        f"funnel {result.funnel_counts} | "
        f"tokens_in {result.cost.get('tokens_in', 0)} "
        f"tokens_out {result.cost.get('tokens_out', 0)} "
        f"cost ${result.cost.get('cost_usd', 0):.4f}"
    )
    if result.report_paths.get("html"):
        print(f"report: {result.report_paths['html']}")


if __name__ == "__main__":
    _cli()
