# Session Summary

Working log of one build session on Alpha Funnel. Branch
`claude/new-session-8fhpvp`. For the durable architecture see `README.md`; for
the successor's log see `PROGRESS.md`.

## What the system is
A single FastAPI service (Postgres, APScheduler in-process) that screens ~6,000
liquid US equities through a cascading funnel to a ranked top-10 with scores,
theses, and charts. Sequential, not a weighted blend: **Stage 1 is a hard trend
gate** (pure numpy, zero network); only survivors are ranked by the Stage-2
composite (momentum 0.30 / quality 0.25 / revisions 0.20 / PEAD 0.15 / value
0.10), then catalysts/news, then LLM triage + deep dive. The same service serves
the JSON API and the static front-end.

## What changed this session
1. **One-button research + live progress** — `/status.current_run` reads the
   pipeline's `StageResult` checkpoints so the UI shows real per-stage progress.
2. **Front-end split** into `dashboard.html` + `/static/dashboard.css` + `.js`.
3. **Removed the app token** — `/run` and `/backfill` open (each lock-guarded).
4. **Readiness by trading days** — `/status.bar_dates` vs a target, replacing a
   meaningless raw-bar-count threshold.
5. **Free-data mode** (SEC universe seed + Yahoo bars) as a keyless fallback,
   auto-selected when `POLYGON_API_KEY` is unset; Polygon grouped-daily remains
   the primary wide-end.
6. **Per-stock detail pages** (`/stock/{symbol}`) built from stored data.
7. **Backfill made unfreezable** — per-call timeouts + a source-agnostic
   `/status.backfill` diagnostics block (phase, source, units done, last_error).
8. **Skip already-loaded days** in the Polygon backfill (resumable; fetches only
   the gap instead of re-downloading from today every run).
9. **Bauhaus redesign + numbered progress timeline.**
10. **Failure surfacing** — `/status.last_run` exposes the real error; the button
    auto-falls-back to fast mode; fast mode persists minimal picks.

> Reverted next session (see fix queue): the temporary 252→205 readiness floor
> and the 200-day-SMA slope tolerance. Insufficient history must **block** the
> run and say so, not run degraded.

## Deploy (Railway, single service + Postgres)
- **Required:** `DATABASE_URL`, `SEC_USER_AGENT`, `ENABLE_SCHEDULER=true`.
- **Recommended:** `POLYGON_API_KEY`, `OPENROUTER_API_KEY` (+ valid model ids).
- **Optional:** `FMP_API_KEY` (caps + sectors), `FINNHUB_API_KEY`, `FRED_API_KEY`.
- **Unused:** `API_KEY`, `REDIS_URL`.

## Known limits / what to improve
- **Free Polygon tier is 5 calls/min** → a fresh 252-day backfill is slow. The
  right fix is a bulk keyless wide-end (Stooq bulk daily: one download, whole
  market), not paying to escape the cap.
- **No free grouped-daily equivalent**; keyless Yahoo per-ticker looping is
  unreliable from datacenter IPs — should be dropped as the default.
- **Sector data absent without FMP** → sector-neutral scoring silently degrades
  to universe-neutral. Add a SEC SIC → GICS-bucket map, cached.
- **Fast-mode fallback is front-end only** → scheduled/cron runs get nothing
  when the LLM fails. Move it server-side.
- **`/run` and `/backfill` are open** with a live LLM key behind them — rate
  limit them.
- **Edge is unmeasured** — IC tracking needs forward returns to accrue; nothing
  has been shown to beat a random draw from the Stage-1 survivors yet.
- This environment **cannot reach live data** (proxy blocks the hosts); all
  verification here is on the seeded harness.
