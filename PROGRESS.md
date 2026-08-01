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

- **182 tests pass**, ruff clean. `pytest -p no:warnings`. (weasyprint PDF test
  runs where the system libs are present, skips otherwise.)
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

**Unknown.** IC, factor-decay, turnover, and the null benchmark are all
implemented and unit-tested against synthetic signals with known IC, so the
*measuring apparatus* works. But no real scores against real forward returns
exist, so the *actual* edge has never been measured. The honest default holds:
assume worthless until measured on live data.

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
- **Next cycle candidates:** (a) cycle-5 checkpoint (cycle 5) — run end to end,
  read as a user, delete dead code, reconcile this file; (b) continue reading
  the deterministic layers for real defects (this cycle found one, so the read
  is paying off — momentum, quality, PEAD, and the news/insider scorers are the
  places a silent sign/scale error would hide). Do NOT invent work.
