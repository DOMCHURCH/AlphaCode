# PROGRESS

Working log for a memoryless successor. Read this, then verify it against the
code — this file lies eventually.

## What this is

A 6-stage cascading funnel that screens ~6000 US equities to a ranked top-10
with theses and charts. Full architecture and rationale live in `README.md`.
Source in `src/`, tests in `tests/`.

## Hard blocker — read before planning live work

**This environment cannot reach live market data.** Two independent walls:

1. **No API keys.** `POLYGON_API_KEY`, `FMP_API_KEY`, `FINNHUB_API_KEY`,
   `FRED_API_KEY`, `OPENROUTER_API_KEY` are all empty.
2. **Network policy blocks the free sources too.** `data.sec.gov` and
   `api.gdeltproject.org` are denied at the proxy gateway (403 on CONNECT —
   verified via `curl "$HTTPS_PROXY/__agentproxy/status"`). Only package
   registries (pypi, npm) are in the no-proxy allowlist.

**Consequence:** the funnel cannot be run on real market data here. Stages
0-3 (deterministic) are verified on a *seeded* harness — constructed test data
with known properties, NOT fabricated-to-pass and NOT a substitute for a live
run. Stages 4-5 (LLM) have never executed against a real model. **The system's
actual edge is UNMEASURED and unmeasurable in this environment** — see
"Honest status of edge" below.

Do not fabricate market data to get around this (explicit guardrail). If a live
run is wanted, the user must supply keys AND an environment whose network policy
permits the data hosts.

## Verified working (offline, this environment)

- **184 tests pass**, ruff clean. `pytest -p no:warnings`. (weasyprint PDF test
  runs where the system libs are present, skips otherwise.)
- **Full funnel end-to-end** (cycle 5, seeded harness, skip_llm): 150 → 62 → 62
  → 62 → 10, $0 tokens, renders the deterministic-ranking HTML + a valid PDF.
  `python -m src.pipeline --resume` / `--skip-llm` both exercised.

## Cycle: UI truth + backfill routing + dead sources + Stage-1 timing

Four-part cycle. All diagnostics reachable from `/diagnostics` (mobile, no shell).

- **A — the UI stops lying.** `/status.last_run` reads the CURRENT run's
  `repository.latest_run` (run_id + `error` + `failed_stage` from the max
  checkpoint), never a stale prior error. The dashboard shows the raw exception
  verbatim (`Failed at Stage N.\n\n<error>`), no speculative "this usually means
  X or Y". Fast-mode is only offered when the failure is actually in an LLM
  stage (`failed_stage in {4,5}`) — Fast skips LLM, so offering it for a
  data-quality failure was noise. DataQualityError already carries the gate's
  own FREE/PAID-split text; the UI surfaces it, doesn't overwrite it.
- **B — backfill routing was the five-cycle bug.** Root cause: `_backfill_bg`
  ALWAYS ran `backfill_bars` first regardless of `kind`; the one dashboard
  button only ever POSTed bars. A "fundamentals backfill" loaded bars and
  nothing else. Fix: `/backfill?kind=bars|sectors|fundamentals|earnings`
  dispatches to exactly ONE loader; `backfill_requested` is logged BEFORE any
  work (B3). `/diagnostics` now has four explicit buttons (Bars, Sectors,
  Fundamentals, Earnings), each showing its own last outcome (rows / error /
  when) from `backfill.results[kind]`. No URL construction on mobile.
  (`test_backfill_dispatches_on_kind_not_always_bars` asserts each kind routes
  to itself and nothing else.)
- **C — dead data sources rebuilt.**
  - FUNDAMENTALS: replaced the per-CIK XBRL crawl (~85k requests) with SEC bulk
    **Financial Statement Data Sets** — ONE ZIP per quarter (`src/ingest/
    sec_datasets.py`). URL `https://www.sec.gov/files/dera/data/financial-
    statement-data-sets/{year}q{q}.zip` confirmed current via data.gov + the SEC
    archive page (sandbox can't fetch the ZIP: SEC 403s non-UA + proxy blocks
    sec.gov — live download verified on deploy, which sends `SEC_USER_AGENT`).
    num.txt × sub.txt joined on `adsh` fills fundamentals (PIT filing_date) AND
    the previously-empty EarningsEvent table (`backfill_earnings`); consensus
    stays None (needs a paid feed — never faked). Parser verified against a
    synthetic ZIP in the documented tab-separated schema (6 tests).
  - BARS: Stooq bulk `download_bulk` now sends a browser-like User-Agent before
    concluding the URL is dead (datacenter IPs get a 403 HTML page from a bare
    client). Still fails loudly on a non-ZIP body — never "no data".
- **D — vectorization IS running; the panel LOAD is the cost.** The 0.09s
  benchmark was `residual_momentum` alone. Stage 1's `_stage` timing box wraps
  `trend.load_panels` (Postgres read of ~600d × ~5k tickers), so its total is
  dominated by the DB read, not the compute. Confirmed at 5000×420:
  `residual_momentum`=0.12s, full `compute_trend_features`=~2.0s. Added a
  `stage1_timing_split` log (panel_load_s vs compute_s) so the deploy log proves
  which is which — no more guessing. Also found `atr` was the single slowest
  indicator (1.9s) via a stack/unstack round-trip; rewrote it element-wise with
  `np.fmax` (0.14s, numerically identical incl. NaN alignment). Deploy timing to
  be read from the split log after this ships.

## Cycle: fundamentals dedup (the last blocker) + momentum-only mode

- **Dedup — CardinalityViolation fixed.** The bulk fundamentals load died with
  "ON CONFLICT DO UPDATE command cannot affect row a second time". TWO mechanisms
  produce the duplicate, both real:
  1. **Tag aliases.** `revenue` maps to 3 XBRL tags; a filing reporting two of
     them emitted two rows with an IDENTICAL natural key. Collapsed in
     `extract_fundamentals` (where the tag is still known), preferring the tag
     listed first in `XBRL_CONCEPTS` — deterministic, not file order.
  2. **Amended filings** (10-K/A, 10-Q/A) repeating prior-period facts.
     Collapsed keeping the **EARLIEST `filing_date`** — as first reported. Keeping
     the latest would import a later restatement into an earlier date, i.e. the
     lookahead bias the PIT layer exists to prevent.
- **Where the collapse happens matters.** `backfill_fundamentals` batches at
  5,000 rows; deduping only inside a batch would let a duplicate split across a
  boundary survive AND let the later batch (the restatement) win the upsert. So
  the collapse runs across the WHOLE quarter before batching, and `backfill_
  earnings` collapses across ALL quarters (a filing appears in more than one
  dataset). `_upsert` also dedupes on `conflict_cols` for EVERY table as a
  structural safety net. Drop counts + pct are logged per quarter
  (`sec_quarter_deduped`) — a large fraction means the key is wrong.
- **Constraint confirmed correct, unchanged.** SEC legitimately has multiple
  values per (ticker, metric, period_end) — restatements — so `filing_date`
  stays in `uq_fundamental_point`. `pit.get_fundamentals` picks the latest
  filing VISIBLE as of the read date, so a restatement filed later is a genuine
  new fact from its own filing date. Within-load collapse ≠ blocking that.
- **Earnings collapse** on (ticker, report_date): `report_date` IS the filing
  date, so earliest can't break a same-day 10-Q/10-K/A tie and neither is
  lookahead. Deterministic instead: prefer a real `actual_eps`, then the latest
  `period_end`.
- **Verified at scale** (synthetic quarter, both mechanisms, 800 companies):
  4,800 facts → 3,200 after alias collapse → 1,600 after earliest-filing
  collapse; natural key unique; earliest filing survived, amendment rejected.

- **MOMENTUM-ONLY mode** — a separate, LABELLED artifact, not a degraded run.
  `mode_config()` in factor_weights; `run_pipeline(mode=)`, `/run?mode=
  momentum_only`, `--mode`, and a `/diagnostics` button. Scores on the 3
  price-derived momentum factors and **bypasses the completeness gate BY
  DESIGN** — it never claims to be the full composite. The 0.40 floor is
  UNCHANGED and still aborts a full run on the same thin data (locked by a test).
  Label, verbatim: `MOMENTUM ONLY — 3 of 19 factors — not the full composite.`
  It appears on the report header, the executive summary, the ranking block,
  every ticker card, the run warnings, the /diagnostics last-run panel, and the
  **stored thesis text** (which drives /stock/<ticker> — the one path that would
  otherwise have shipped unlabelled).
  `DailyScore.mode` + `RunLog.mode` are stored, and EVERY IC query
  (`compute_ic`, `factor_decay_analysis`, `turnover`, `ic_report`) filters on
  mode — momentum-only scores can never pool with full-composite scores.
  Also fixed: the report's "Factor weights used" table printed the full five
  categories regardless of mode; it now prints the weights that ACTUALLY scored
  the run.
  E2E on a prices-only seeded market (no fundamentals at all): full run aborts
  on the floor; momentum-only returns an ordered top-10, labelled 4× in the HTML.

- **Still deploy-only:** the real SEC bulk download. Re-verified this cycle —
  `sec.gov` and `stooq.com` return 403 on CONNECT at the proxy (allowlist is
  package registries only), so the live row counts must come from the deploy's
  /diagnostics Fundamentals button.

- **/validation** now surfaces a survivorship-bias self-check (keyed off scored
  dates; returns a clear note until ≥2 exist).
- **Point-in-time enforcement** (`tests/test_pit.py`, 41 tests): the guardrail
  suite. Sweeps every date in the month before a filing; fails if any can see
  the data. Restatement resolution + SEC-over-vendor tiebreak covered.
- **Deterministic funnel end-to-end** on the seeded harness via the
  `resume_from=1` path (skips the Polygon universe call, reads the seeded
  snapshot). Last offline run: 150 → 62 → 62 → 62, regime LATE_CYCLE, ~2.9s,
  $0 tokens, renders a real ranked top-10 with the deterministic-mode report.
- **Report** renders a single portable HTML (CSS inlined, PNGs base64'd via
  kaleido 0.2.1). ~706 KB with charts.

## Known-incomplete / half-built

- **IV rank** returns None until a trailing IV history accumulates (by design).
- **Resume is not auto-triggered by the scheduler.** `resume_last()` and the
  CLI `--resume` exist and are verified, but the APScheduler `daily_job` still
  starts fresh on every fire. A crashed run must currently be resumed by hand
  (`python -m src.pipeline --resume --date YYYY-MM-DD`). Auto-resume-on-retry is
  a reasonable future cycle but not obviously wanted (a fresh run next morning
  is often the right call), so left as a manual op for now.

## Guardrails (do not violate)

- `tests/test_pit.py` never gets disabled. If it fails, that is the only work.
- Never fabricate/synthesize market data. Fail loudly instead.
- Log token spend and funnel stage counts every run.
- Don't skip ahead to the LLM layer while deterministic layers are unverified.
- **Every diagnostic is reachable from `/diagnostics` — no CLI-only checks.**
  See the section below; this is a standing requirement, not a one-off.

## Diagnostics — `/diagnostics` is the single pane (phone-first)

The operator is always on mobile with no shell. `/diagnostics` (page) +
`GET /diagnostics.json` (payload) are the one place to look when something
breaks. It shows, top to bottom: a plain-English **verdict banner** (what's
wrong + what to do), **data health** (adjustment/coverage/recency/history from
the reconcile cache + DB), **last run** (per-stage table incl. `peak rss_mb`,
failed stage highlighted with its real error), **config** (every env var as
present/missing/invalid, never the value), **recent logs** (in-memory ring, the
pipeline child's stdout teed in by `src/runner.py`), and **actions** (run / run
fast / backfill, rate-limited with disable reasons). A **"Copy everything"**
button puts the whole state on the clipboard as text to paste into chat.

**RULE for any future check you're asked to add:** wire it into this page. Add
the data to `_diagnostics_*` in `src/api.py` (and the network-backed ones to a
cache like `_RECONCILE_CACHE` so the 10s auto-refresh stays cheap), render it in
`src/report/static/diagnostics.js`, and include it in `buildCopyText`. A check
that only exists as a CLI module (`python -m src.something`) is not done until it
is reachable here. Keep the CLI working, but share the logic (as `/reconcile`
does with `src/reconcile.py`).

## Live-data run — cycle log (first real end-to-end run)

The user's deploy has real data (312/252 trading days, 15,100 Stooq tickers,
Polygon tier-gating confirmed, deepseek LLM resolves). Stages 1-6 had never run
on real data. This section logs each failure the real run hit and the fix. NOTE:
this sandbox still cannot reach live hosts, so runs happen on the deploy and the
failures come from its /diagnostics blob; fixes are verified here against the
seeded harness + unit tests, not a live run.

- **Cycle A — Stage 0 crash: NaN read as a string.** `is_common_stock`
  (builder.py) did `security_type is not None and security_type.upper()`; a
  missing cell is NaN (a float), not None, so `nan.upper()` raised. `ticker` was
  the same trap. Fixed with `_is_missing()` (None OR NaN) + str() coercion; also
  killed the object-dtype `.fillna` FutureWarning at builder.py:270 with an
  explicit `.astype(bool)`. Commit c6e69f9.
- **Cycle A (sweep) — the same trap across the codebase.** `Series.get(k) or
  default` is silently wrong: a NaN cell is TRUTHY, so the fallback never fires
  and NaN leaks downstream (as a dict key, or `str(nan)=="nan"`). A full sweep
  (factors/catalysts/ingest/report/llm/validation/storage) found 8 genuine
  DataFrame-derived sites: pipeline `_deterministic_ranking`/`_near_misses`
  sector lookups (x3), stage3 GDELT name (`"nan"` company name), packets triage
  + deep sector (x2), deep_dive sector→dict-key, builder sector_source, yahoo
  dividend/split_ratio (x2). All routed through a new NaN-safe `or_default`
  (`src/util.py`), which returns the default for None/NaN and the falsy cases
  `or` already covered. Plain-dict `.get(...) or {}` sites (JSON API responses)
  were correctly left alone -- those return None, not NaN. Tests: `or_default`
  over NaN/None/""/Series.get; is_common_stock over NaN/None/""/whitespace.
  288 tests pass, ruff clean.
- **Cycle B — Stage 3 wrote ZERO filings, silently.** Two bugs. (1) The shared
  `_upsert` built the `on_conflict_do_update` set_ with `getattr(stmt.excluded,
  c)`; for the `items` column (FilingEvent, 8-K item codes) that returns
  ColumnCollection.items -- the bound METHOD, not the column -- so psycopg2 raised
  "can't adapt type 'method'" on every filing insert. Fixed with subscript
  `stmt.excluded[c]`, which never collides. Audit: `_upsert` is the only upsert
  chokepoint, and `items` is the only column whose name collides with a
  ColumnCollection method (no keys/values/count/index columns), so the one fix
  covers every table and future-proofs new ones. (2) MORE IMPORTANT: that error
  was raised per-ticker inside `gather_bounded`, which isolates a failing ticker
  into a warning -- so Stage 3 persisted 0 filings and the run continued. Added a
  write ledger in repository (`_upsert` tallies attempted-vs-written per table,
  recorded even when a batch raises) + `assert_writes`; the pipeline resets it
  before Stage 0 build, Stage 3 enrichment, and score persistence, logs
  rows_written/attempted per table (`stage_writes` -- visible in /diagnostics
  logs), and ABORTS with DataQualityError if any table attempted >100 writes and
  landed 0. A stage can no longer complete having written nothing it tried to.
  Tests: the `items` upsert round-trips; the ledger records 0-written on a failed
  execute; the pipeline aborts on a wholesale zero and does not on real writes.
- **Cycle C — first run reached Stage 3; four issues (all fixed).**
  1. *Sectors 0%.* Not a mapping bug (sic_to_gics is correct) -- a TIMING bug: the
     funnel's Stage 0 read the SectorMap 70s BEFORE the SIC backfill populated it.
     Made it visible: builder logs `sector_map_empty` loudly at build time, and
     /diagnostics has a Sector-map panel (total/mapped/unmapped + sample raw
     SIC->GICS). Operational fix: run /backfill?sectors=true to completion before
     the funnel.
  2. *Mean completeness 0.157.* Composite logs per-factor coverage; pipeline now
     ABORTS if mean completeness < MIN_MEAN_COMPLETENESS (0.40), naming the empty
     factors. This run (no FINNHUB revisions, fundamentals not joining) would now
     stop with a defensible message instead of shipping a 16%-data ranking.
  3. *Speed (Stage 3 = the bottleneck).* Polygon tier check moved inside the
     client: on free tier non-permitted calls (options) raise immediately instead
     of the rate limiter sleeping 12s each (the flood); Stage 3 skips the options
     client on free tier. SEC bucket 8->9/s. Stage 2 output (Stage 3 input)
     400->200. Per-source timing logged (stage3_source_timing). Postgres cache
     for SEC (24h, was NEVER caching -- source "sec" had no CACHE_TTL entry) and
     GDELT (6h), so same-day re-runs skip the network instead of losing the
     in-process cache each subprocess.
  4. *stage_writes on every stage:* moved reset/log/abort into the _stage context
     manager -- every stage logs rows_written/attempted and aborts on a bulk zero.
- **Next real failure:** expected at Stage 1-6 on the deploy. Redeploy, run, read
  the /diagnostics blob, fix the actual cause, repeat. Sanity bands to check each
  stage: S0 4-7k survivors of 15.1k; S1 300-2500; S2 ~400 (+ mapped/unmapped
  sector ratio); S3 ~100; S4/5 25→10. Any stage dropping >95% is a bug until
  explained. Watch rss_mb per stage (<1GB).

## Honest status of edge

**Unknown, and unmeasurable in this environment.** IC, factor-decay, turnover,
and the null benchmark are implemented and unit-tested against synthetic signals
with known IC, so the *measuring apparatus* works. But no real scores against
real forward returns exist, so the *actual* edge has never been measured. I did
NOT run the validation harness on the synthetic seed and present it as an edge
result — that would be fabricating a signal, which the guardrails forbid.

Architectural read (a prior, not a measurement): the funnel implements
well-replicated anomalies — 12-1 momentum with the mandatory last-month skip,
Novy-Marx gross profitability, Sloan accruals, PEAD/SUE with time-decay, estimate
revisions — each winsorized and sector-neutralised correctly (now tested). Those
effects are real in the literature. But they are crowded and decay; the daily
rebalance implies high turnover that transaction costs can eat; and the LLM
layer's contribution is entirely unvalidated. Net-of-costs edge for *this*
implementation is unproven. The honest prior is skepticism until the IC tracker
and null benchmark run on live data across enough days to clear the noise.

## Deploy-readiness pass (post-cycle-5, user-requested) — DONE

Made the system genuinely Railway-ready. Six fixes, each verified:

1. **Reports served from the DB.** Worker and api are separate services with
   separate ephemeral filesystems, so a disk-only report 404'd on the api. Now
   the rendered HTML + PDF persist to Postgres (`report_artifacts`); the api
   serves from the DB. Proven: ran the funnel, **wiped the report dir**, api
   still served HTML (200) and a valid PDF (200, `%PDF`).
2. **Startup migration.** `init_db()` (called by both services at boot) now
   creates the performance indexes too, so the real Postgres is fully migrated
   on deploy — not a build-phase step where `DATABASE_URL` may be unresolved.
   Dropped the build-phase DB command from `nixpacks.toml`.
3. **FastAPI lifespan** instead of the deprecated `@app.on_event`.
4. **Optional API-key guard** on `POST /run` (the token-spending endpoint) via
   `API_KEY`; unset = open (dev), set = `X-API-Key` required. Reads stay open.
5. **Clean prod-only install verified** — a fresh venv from `requirements.txt`
   alone imports every module and all four entrypoints; weasyprint works.
6. **API surface tests** (`tests/test_api.py`) — the api had zero coverage.

Added `DEPLOY.md` (topology, env vars, backfill, endpoints, egress hosts).
**192 tests, ruff clean.** Everything verifiable offline is done. The only
remaining unknown is the live API pass (real Polygon/FMP/etc. payloads), which
needs keys + egress and can only happen in the Railway environment.

## Loop status: STOPPED after cycle 5

The autonomous loop stopped itself here, on purpose. The remaining unknown that
matters — live-path correctness and real edge — is gated on live market data,
which needs BOTH API keys and a network policy that permits the data hosts.
Neither exists here (see the hard-blocker section). Every further cycle would
either hit that same wall or manufacture make-work, and inventing work is
explicitly forbidden. To resume meaningfully: supply the keys AND an environment
whose egress allows polygon/fmp/finnhub/fred/openrouter/sec/gdelt, then run
`python -m src.pipeline` for several sessions and read `/validation`.

## Cycle log

- **Cycle 0 (build):** commit `087d0ba`. Full system, 176 tests. See git.
- **Cycle 1 (done):** created this file. Finished the resume feature, which was
  a mechanism with no trigger. Added `resume_last()`, a `python -m src.pipeline
  [--resume] [--date] [--skip-llm]` CLI, and a pipeline-level integration test
  (`tests/test_pipeline_resume.py`, 3 tests). Fixed a real bug: `start_run` did
  a plain INSERT, so reusing a run_id on resume would have violated the unique
  constraint — now idempotent. Verified by reading the log output: a resume
  reports `resume_from=4`, restores Stage 3 in 0.01s with **0 API calls**, and
  the enrichment fn (patched to raise) is never called. 179 tests pass, ruff
  clean. Funnel on the seeded harness: 150→66→66→66→10, $0 tokens.
- **Cycle 2 (done):** verified the PDF export path, which had never been run.
  The system libs (cairo/pango/gobject/gdk-pixbuf) ARE present in this
  environment; weasyprint installs and `_render_pdf` produces a valid 354 KB
  PDF (%PDF header + %%EOF, verified by reading the bytes). The code was correct
  all along — it only "silently skipped" because weasyprint wasn't in the venv.
  Added a test (`test_pdf_renders_a_valid_document_when_weasyprint_available`)
  that verifies real PDF structure where the libs exist and skips otherwise, so
  the path is no longer untested. Note: the venv is not committed, so a fresh
  container needs `pip install -r requirements-dev.txt` for the test to run
  (else it skips).
- **Cycle 3 (done):** found and fixed a real correctness bug in the Stage 3
  macro tilt. `combine_and_rank` multiplied the blended score by the sector
  tilt, but the blended score is a z-score that goes negative — so for any name
  below the survivor-set mean (~half of them) the tilt inverted: in RISK_ON a
  suppressed-sector name out-ranked an identically-scored favoured-sector name.
  Verified empirically (Utilities beat Technology at blend=-0.5), then switched
  to an additive tilt `blended + (tilt-1)*REGIME_TILT_STRENGTH`, which is
  sign-correct. Two regression tests: favoured sector wins at negative/zero/
  positive blends, and the tilt nudges without overwhelming a 3-sigma gap.
- **Cycle 4 (done):** read the PEAD path. The `pead_window` double-use (it both
  decays SUE and is a standalone factor) is a defensible reading of the spec,
  NOT a bug — left alone rather than rewritten to taste. But the PEAD
  time-decay, which the spec explicitly requires, had zero test coverage. Added
  two tests: `get_last_earnings` computes SUE = (actual-consensus)/stdev and the
  linear decay to zero over 60 days (a stale surprise is discounted, a
  <3-report history yields NaN SUE not a fake number), and the composite zeroes
  out a stale surprise end-to-end.
- **Cycle 5 (done, reconciliation checkpoint):** ran the funnel end to end and
  read the report as a user (deterministic top-10 renders sensibly; PDF valid).
  Deleted genuinely dead code: `dives_to_frame` (llm/deep_dive.py, unused +
  freed the `pandas` import there), `median_or_none` (macro.py, unused + freed
  `import statistics`), and dead internals inside the survivorship check.
  `walk_forward_universe_check` was a real survivorship-bias guardrail that was
  simply disconnected — rather than delete it, wired it into `/validation`
  (`ic_report`), so a system whose whole PIT design fights survivorship bias now
  actually reports on it. Reconciled this file.
- **Open refinement (not urgent):** the survivorship check keys off scored dates
  (`iter_score_dates`); universe snapshots can exist for more dates than were
  scored (e.g. after a bare backfill). Keying off universe-snapshot dates would
  cover more. Left as-is — the check is correct for the daily-run case and
  keying it differently is a judgement call, not a bug.
- **Honest edge status unchanged:** still unmeasurable here (no live data). The
  validation machinery is verified; the actual signal is unknown. Any further
  cycle that cannot reach live data should keep to deterministic-layer
  correctness and coverage, and must not invent work.

## Fix queue (autonomous loop, correctness-first)

Directive: never weaken a gate/threshold/test to make a run succeed; surface
emptiness, don't tune it. No new features until the queue clears.

- [x] **#1 Revert gate loosening.** slope() back to strict (NaN on a
  just-warmed SMA200); `settings.min_history_days`=252 is the one source of
  truth; pipeline hard-blocks a run with `insufficient history: N/252` rather
  than running degraded. `_history_depth()` measures it. Tests updated to assert
  the block, not the degrade.
- [x] **#2 Stooq bulk daily** (done — src/ingest/stooq.py; keyless backfill uses it; Yahoo per-ticker dropped as default; verified offline on a synthetic bundle: 300×300 → 90k rows).

- [x] **#3 SEC SIC -> GICS sector map** (done — src/ingest/sic.py + SectorMap table + backfill_sectors; free universe attaches cached sectors; verified offline).
  HARDENED (v2): sector_source (fmp|sic|unknown) on SectorMap/UniverseSnapshot/
  DailyScore so IC can separate real vs SIC-derived vs guessed sectors;
  unmapped SIC (incl. 6770 blank-check, 6719 holdco, 9995/7/9) -> None and
  EXCLUDED from sector-neutral z-scoring (composite no longer pools them into an
  'Unknown' bucket -> universe residual instead); mapped/unmapped ratio logged
  each run; SIC->GICS hand-checked on the named cases. Live coverage/symbology/
  adjustment/recency reconciliation: `python -m src.reconcile` (deploy-only).
- [x] **#4 Fast-mode fallback server-side** (done — pipeline: verification failure + runtime LLM failure both degrade to the deterministic ranking+report; DataQualityError still propagates; test added).
- [x] **#5 Rate-limit /run and /backfill** (done — _RateGate sliding-window, RUN_RATE_PER_HOUR/BACKFILL_RATE_PER_HOUR, 429+Retry-After; cron unaffected).
- [x] **#6 LLM model resolution** (done — verify_model against /api/v1/models
  already existed + degrades post-#4; added self-serve GET /llm-check so the
  operator can confirm the reason write-ups are missing).
  BLOCKED: confirming the ACTUAL prod failure needs the live deploy/OpenRouter
  (sandbox can't reach it). Hit /llm-check on the deploy to confirm.

## Queue cleared (code) — offline funnel read (seeded harness)

Ran the deterministic funnel on a seeded 180-name market (2/3 uptrend):
counts 180 -> 67 -> 67 -> 67 -> 10. Every downtrend name cut by Stage 1 (gate
gates). Top-10 ordered by descending composite z (+0.86..+0.16); scores track
it. Sector spread across IT/Energy/Financials/Industrials/Staples (sector-neutral
working). Gates gate and factors fire on the harness.

STOP-AND-ASK / remaining (all need the live deploy — cannot be done here):
- Confirm the real prod failure: hit `/llm-check` and `/status.last_run` on the
  deploy and share them.
- "Run on complete history + read the top-10 as a user": needs live data
  (network blocked here); verified on the seeded harness only.
- "Start IC tracking vs a random Stage-1 draw": the machinery exists
  (/validation, benchmark_against_null, backfill_forward_returns) but only
  accrues signal from real forward returns over live daily runs.

NOTE: this environment still cannot reach live data (proxy blocks SEC/Yahoo/
Stooq/Polygon; verified). Items are built + verified on the seeded/synthetic
harness offline; live verification requires a deploy with keys + open egress.
