# Daily Equity Alpha Funnel

Screens ~6,000 liquid US equities every morning and outputs a ranked top-10 with
scores out of 100, written theses, and charts.

The architecture is a **cascading funnel**. Each stage costs more per name than
the last and cuts the universe hard, so cost and latency scale with the narrow
end rather than the wide end.

```
Stage 0  Universe build      ~10,000 -> 6,000    bulk API, 1 call
Stage 1  Trend gate           6,000  -> 1,200    vectorised, 0 API calls
Stage 2  Multi-factor score   1,200  ->   400    bulk fundamentals, cached
Stage 3  Catalyst + flow        400  ->   100    per-ticker API, parallel
Stage 4  LLM triage             100  ->    25    4 batched LLM calls
Stage 5  LLM deep dive           25  ->    10    25 LLM calls
Stage 6  Report + charts         10  ->    10    render HTML/PDF
```

**No LLM call happens before Stage 4.** Stages 0-3 are deterministic
pandas/numpy and produce a defensible ranked list on their own. That is what
keeps the token bill under $1/day, and it means the model is an enhancement to
the funnel rather than a single point of failure for it.

---

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in the API keys

python -m src.migrate                    # create the schema
python -m src.backfill --days 600        # Stage 1 needs 400 sessions of bars
python -m src.backfill --fundamentals --skip-bars   # SEC XBRL, slow, run once

python -c "import asyncio; from src.pipeline import run_pipeline; asyncio.run(run_pipeline())"
uvicorn src.api:app --reload             # then open /report/latest/html
```

Run the pipeline without spending tokens while you are still wiring things up:

```python
asyncio.run(run_pipeline(skip_llm=True))
```

---

## Layout

```
src/
  ingest/      one module per data source, all behind one rate limiter
  universe/    stage 0
  factors/     stages 1-2  (trend gate, cross-sectional math, composite)
  catalysts/   stage 3      (SEC events, GDELT news, FRED macro, risk screen)
  llm/         stages 4-5   (packets, schemas, triage, deep dive, cost)
  report/      stage 6      (plotly charts + jinja template)
  storage/     models, point-in-time accessors, repository, cache
  validation/  IC tracking, factor decay, purged CV, null benchmark
  config/      settings, factor weights, rate limits
  pipeline.py  orchestrator
  api.py       FastAPI
  scheduler.py APScheduler worker
tests/
migrations/
```

---

## Data sources

Each source has exactly one job. Nothing is duplicated.

| Source | Job |
|---|---|
| **Polygon** | All OHLCV, universe reference data, options snapshots. Grouped-daily returns every US ticker in one call — the backbone. |
| **FMP** | Bulk fundamentals, ratios, sector/industry mapping, earnings calendar. Bulk endpoints only, never a per-ticker loop. |
| **Finnhub** | Estimate revisions, recommendation trends, earnings surprises, insider transactions. Stage 3 only, once the universe is under 400. |
| **SEC EDGAR** | Ground-truth filings and as-reported XBRL. The source of truth for point-in-time fundamentals. |
| **Yahoo** | Fallback and reconciliation only. Every call is wrapped; a failure degrades to "unknown", never to a hard error. |
| **FRED** | Macro regime state. Drives sector tilts and risk appetite, never individual names. |
| **GDELT 2.0** | News volume, tone trajectory, and event themes at scale. |

Rate limiting is one Redis-backed token bucket per source, configured in
`src/config/rate_limits.py`. There is no `time.sleep()` anywhere in the
codebase; every request goes through `RateLimiter.acquire()`.

---

## Point-in-time correctness

This is the part that separates a real system from a toy, so it is worth being
explicit about.

A company with a fiscal quarter ending 2025-09-30 may not file its 10-Q until
2025-11-08. Using that data on 2025-10-01 is lookahead bias and makes every
backtest result meaningless.

- Every fundamental datapoint stores `period_end`, `filing_date` (the SEC
  `filed` field), and `ingested_at`.
- Every read goes through `get_fundamentals(session, ticker, as_of)`, which
  enforces `filing_date + 2 business days <= as_of`. Nothing else queries the
  `fundamentals` table.
- **Restatements** resolve to what was actually known: on a date before an
  amendment was filed, you get the original figure; after, the restated one.
  SEC as-reported beats a vendor's restated figure on a filing-date tie.
- Fundamentals are never forward-filled across a reporting gap, and missing
  values are never imputed with the universe mean — they stay missing and are
  reported in the completeness table.
- The **universe snapshot is persisted every single day**, including names that
  have since delisted. Screening today's live tickers against 2023 prices is a
  fantasy.

`tests/test_pit.py` asserts all of this. It sweeps every date in the month
before a filing and fails if any of them can see the data.

---

## The factor math

For every raw factor, in order:

1. **Winsorize** at the 1st/99th percentile. One bad datapoint creates a fake
   40-sigma outlier that otherwise dominates the composite.
2. **Z-score within sector.** Without this, the top-10 is just whichever sector
   is currently hot or cheap.
3. **Missing data scores z=0** (sector-neutral), and `data_completeness` is
   tracked per ticker and shown in the report appendix.
4. **Composite** = weighted sum of category z-scores, then rank
   cross-sectionally.

Category weights (`src/config/factor_weights.py`): momentum 0.30, quality 0.25,
estimate revisions 0.20, PEAD 0.15, value 0.10.

Two details that are load-bearing:

- **12-1 momentum skips the last month.** Short-term reversal contaminates raw
  12-month momentum. `test_momentum_12_1_skips_the_last_month` plants a violent
  recent move and asserts the factor does not see it.
- **Accruals and rising leverage are sign-flipped** (`NEGATIVE_FACTORS`) rather
  than given negative weights, so the weight table stays readable.

---

## LLM discipline

- The model never does arithmetic. Everything is computed in Python; the model
  interprets.
- It never receives raw price arrays, raw filings, or raw article text. Triage
  packets are under 200 tokens per ticker; deep-dive packets carry computed
  statistics, an 8-quarter table, and headlines with tone scores.
- The system block is prompt-cached, so its field definitions are paid for once
  rather than once per batch.
- Stage 5 outputs each rubric subscore **separately** and the total is
  recomputed in Python from the parts. A holistic number from the model is
  discarded.
- `invalidation` is mandatory and must be checkable tomorrow — a price level, a
  named moving average, a numeric threshold, or a date. "If the thesis breaks"
  is a validation error and gets retried. A thesis without a falsification
  condition is a story, not an analysis.
- On a schema violation, triage retries once with the parse error appended and
  then **falls back to the deterministic Stage 3 rank** for that batch.
- The model string is verified against `GET /api/v1/models` at startup and fails
  loudly with the available DeepSeek options if it does not resolve.

The final list caps at **3 names per GICS sector**. If that leaves fewer than 10
names, it ships fewer — relaxing the cap under pressure defeats its purpose.

---

## Validation

> A screener that has never been measured is a random number generator with good
> typography.

`GET /validation` returns:

- **Information Coefficient** — Spearman rank correlation of score vs forward
  1d/5d/21d return, computed per date and averaged (pooling would let one wide
  day dominate). Sustained 21-day IC above 0.03 is a real signal; below 0.02 is
  dead weight.
- **Factor decay** — IC per factor category, with a verdict on each.
- **Turnover** — healthy is roughly 20-40%/day for a daily-rebalanced momentum
  system. Complete daily turnover means the signal is noise.
- **Benchmark against the null** — the top-10's forward returns vs an
  equal-weight random 10 drawn from the Stage-1 survivors, and vs SPY. If the
  funnel cannot beat a random draw from the trend-filtered pool, then Stages 2-5
  are adding nothing and only the trend gate matters. The verdict says so in
  those words.

`purged_kfold` implements purged k-fold CV with an embargo equal to the holding
horizon. Standard k-fold leaks on financial time series because adjacent samples
overlap in time.

Weights are re-fit **quarterly**, not daily. The factor-decay output deliberately
does not auto-update `factor_weights.py` — daily refitting overfits to noise.

---

## Railway deployment

Four services:

1. **api** — `railway.toml`, FastAPI, healthcheck on `/health`
2. **worker** — `railway.worker.toml`, APScheduler at 06:00 America/New_York
   weekdays, holiday-guarded with `pandas_market_calendars`
3. **Postgres** plugin
4. **Redis** plugin

`nixpacks.toml` installs cairo/pango for weasyprint and runs `python -m
src.migrate` in the build phase, so a deploy migrates itself.

Operational guarantees built in:

- Structured JSON logging (`structlog`). Every stage logs entry count, exit
  count, duration, and API calls made.
- **Checkpoint after every stage** to Postgres. If Stage 4 fails you resume from
  the Stage 3 output instead of re-running the whole funnel.
- **Cost tracker** logs tokens in/out and USD per run and alerts past
  `MAX_RUN_COST_USD`.
- **Data-quality gates**: the run aborts if the universe is under 4,000 names or
  any stage drops more than 95% of its input, rather than shipping a bad report.
- Target runtime is under 12 minutes; a longer run raises a warning in the
  report.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness + latest run status |
| `GET /reports` | run history |
| `GET /report/{date}` | JSON: names, scores, theses |
| `GET /report/{date}/html` | the rendered report (`latest` works as a date) |
| `GET /report/{date}/pdf` | PDF |
| `GET /ticker/{symbol}/history` | bars, scores, and past theses |
| `GET /validation` | IC, factor decay, turnover, null benchmark |
| `POST /run` | manual trigger (`?skip_llm=true` to run Stages 0-3 only) |

---

## Tests

```bash
pytest                      # 173 tests
pytest tests/test_pit.py    # the lookahead-bias suite specifically
```

The suite covers point-in-time enforcement and restatement handling, trend-gate
correctness, winsorization and sector neutrality, the layoff company-vs-sector
distinction, macro regime classification, LLM schema enforcement and token
budgets, the sector cap, IC and purged CV, report portability, and an end-to-end
run of Stages 0-3 with every external API stubbed.

---

## What this is

A research and idea-generation tool. It surfaces candidates for a human to
evaluate. The scores are the output of a heuristic pipeline plus a language
model's interpretation, **not a prediction**. Every thesis ships with an explicit
invalidation condition specifically so that it can be checked and thrown out.

Nothing here is investment advice. The validation layer exists because the honest
default assumption is that any new signal is worthless until measured.
