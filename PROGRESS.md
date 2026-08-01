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
