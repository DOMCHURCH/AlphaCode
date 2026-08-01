# Claude Code build prompt — Daily Best-Stock Research Engine

Copy everything below the line into Claude Code to build this system from scratch.
(It is the specification the current repo was built from, refined.)

---

Build a production Python system that researches the ~6,000 most liquid US
equities every morning and outputs a ranked **top-10 best ideas**, each with a
score out of 100, a full written thesis, and data charts. Deploy it on Railway
as a single web service that anyone can open in a browser, search a ticker, and
analyze it with a live chart.

Read this whole spec before writing code. Build it in the stage order given. Do
NOT build the LLM layer first — the deterministic stages must produce a
defensible ranked list on their own.

## Core idea: a cascading funnel, cheap → thorough

The analysis gets simpler-per-name at the wide end and more thorough as it
narrows. Cost and latency scale with the narrow end, not the wide end.

```
Stage 0  Universe build      ~10,000 → 6,000   one bulk API call
Stage 1  Trend gate           6,000 → 1,200    "is it going up right now?" — drop the ones that aren't. Vectorized, ZERO API calls.
Stage 2  Multi-factor score   1,200 →   400    institutional factors, sector-neutral
Stage 3  Catalysts + news       400 →   100    per-ticker APIs, run in parallel
Stage 4  LLM triage             100 →    25    4 batched LLM calls
Stage 5  LLM deep dive           25 →    10    one call each, full thesis + score/100
Stage 6  Website + charts         10 →    10    live site + per-name graphs
```

Hard rule: **no LLM call before Stage 4.** Stages 0–3 are deterministic
pandas/numpy. That is what keeps the token bill under ~$1/day.

## Stage 1 — the simple first pass (the user's ask)

Load ~400 trading days of bars for the whole universe into one dataframe and
vectorize. Keep only names in a clean uptrend — drop everything that isn't going
up right now. Gate on: close > EMA20 > SMA50, close > SMA200 with a rising
200-day slope, positive 3-month return, top-30% relative strength vs the
universe, and within 25% of the 52-week high. Zero API calls; runs in seconds.

## Stages get deeper as you narrow (institutional strategies)

**Stage 2 — the factors big shops actually use**, all cross-sectional and
**sector-neutral** (z-score within GICS sector, winsorize outliers first):
- Momentum: 12-1 return (skip the most recent month — short-term reversal
  contaminates it), momentum quality, residual momentum.
- Quality: gross profitability (Novy-Marx), ROIC, accruals (low is good, Sloan),
  FCF yield, debt trend, Piotroski F-score.
- Estimate revisions: 4-week change in consensus EPS/revenue, up/down ratio.
- Earnings drift (PEAD): standardized unexpected earnings, weighted by a decay
  over the 60 days after the report; earnings-day gap.
- Value (a tiebreaker, sector-relative only): EV/EBIT, EV/Sales, P/FCF.
Composite = weighted sum of category z-scores; take the top 400.

**Point-in-time is mandatory.** Lag every fundamental to its actual SEC filing
date, never the fiscal period end. Store `period_end`, `filing_date`, and
`ingested_at`; every read enforces `filing_date <= as_of`. Write a test that a
fundamental is never readable before its filing date. Snapshot the universe
every day so backtests reconstruct history including delisted names.

**Stage 3 — catalysts, news, and big events.** Now per-ticker calls are cheap;
run async with a semaphore + a Redis (optional) or in-process rate limiter:
- SEC EDGAR: insider cluster buys (Form 4 code P), 8-K items, shelf/secondary
  filings (dilution = negative), activist 13D.
- GDELT: news volume z-score vs the ticker's own baseline, tone trajectory, and
  **event themes — layoffs/job losses, bankruptcy, M&A, strikes, recalls,
  lawsuits.** Distinguish layoffs *at the company* (margin story, mild positive)
  from *sector/economy-wide* layoffs (demand signal, negative).
- FRED macro regime (curve, HY spreads, claims, NFCI, VIX) → sector tilts.
- Options positioning; a crowding/tradeability reject screen.
Rank on factor + catalyst; take the top 100.

## Stages 4–5 — DeepSeek via OpenRouter, token-efficient

Model: DeepSeek through OpenRouter. Put the model string in config
(`LLM_TRIAGE_MODEL`, `LLM_DEEP_MODEL`) — at startup, hit `GET /api/v1/models`,
verify it resolves, and fail loudly with the available DeepSeek options if not.
Enable OpenRouter prompt caching on the system block.

Token discipline is the whole point of the funnel: send the model a compressed
numeric fact packet per ticker (<200 tokens at triage), never raw filings,
prices, or news text. Stage 4 batches 25 tickers/call (4 calls) → 25 names.
Stage 5 is one call per name → force six subscores that sum to a score out of
100 (trend 25, fundamental 20, catalyst 20, news 15, macro 10, risk 10), a
3–5-sentence thesis citing real numbers, bull/bear cases, and a **mandatory,
checkable invalidation level**. Cap the final list at 3 names per sector. Never
let the model do arithmetic — compute in Python, let it interpret.

## Stage 6 — the website

A single Railway web service (FastAPI) that serves:
- A homepage where you **search any US ticker or click a pick to analyze it**,
  showing a live **TradingView chart** beside the funnel's read.
- The daily **top-10** as the "best ideas," each opening a full analysis.
- Per-name report with charts: 1-year candles with EMA/SMA overlays and
  **event markers (earnings, insider buys, 8-K filings, news spikes)**, a factor
  radar, 8-quarter fundamentals, a news-volume/tone timeline, peer comparison,
  plus a macro dashboard and a funnel visualization. Export standalone HTML + PDF.
- Run the daily job in-process on a schedule (06:00 America/New_York, weekdays,
  holiday-guarded). Curl-triggerable `/backfill` and `/run` endpoints guarded by
  an API key.

## Stack & deploy
Python 3.11, pandas/numpy/scipy, httpx+asyncio, SQLAlchemy on **Postgres**
(Redis optional), FastAPI, APScheduler, plotly+kaleido, jinja2, pydantic v2,
tenacity. Keys from env vars: `POLYGON_API_KEY`, `FMP_API_KEY`,
`FINNHUB_API_KEY`, `FRED_API_KEY`, `OPENROUTER_API_KEY`, `SEC_USER_AGENT`,
`DATABASE_URL`. Single Railway service; schema migrates itself at startup;
data-quality gates abort a bad run rather than shipping it.

## Validation (do not skip)
Store daily scores; once forward returns exist, compute the Spearman IC of score
vs 1d/5d/21d return, per-factor decay, turnover, and a benchmark against a random
draw from the Stage-1 survivors and vs SPY. If it can't beat the random draw,
only the trend gate is doing work — know that before trusting it. Use purged
k-fold CV with an embargo for any backtest.

## Note
This is a research and idea-generation tool. The scores are a heuristic pipeline
plus a model's interpretation, not a prediction. Every thesis ships with an
explicit invalidation condition so it can be checked and thrown out. Nothing here
is investment advice.
