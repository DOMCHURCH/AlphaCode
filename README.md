# Company Data Service

SEC as-reported fundamentals for ~6,000 US companies, with price bars and a
sector map alongside. **Descriptive only** — it makes no predictions, produces no
scores, and ranks nothing.

The ranking funnel that used to live here is gone. What is left is the data
plane: load SEC's quarterly Financial Statement Data Sets, extract the
consolidated facts correctly, validate them before they are stored, and serve
them.

```
SEC quarterly ZIP  ──▶  extract (consolidated + right fact type)
                          │
                          ├── validate (reject what cannot be true)
                          │
                          └──▶  Postgres  ──▶  JSON API + /admin
```

---

## The extraction is the whole product

SEC's `num.txt` carries the same XBRL tag many times per period at different
dimensional levels — consolidated, by segment, by geography, by legal entity —
and both *instant* facts (balance sheet, a point in time) and *duration* facts
(income and cash flow, over a period).

A parser that takes the first matching row produces numbers that look real and
are wrong. That is what the previous one did. For JPMorgan's 2025 fiscal year it
stored:

| Metric | Stored | Actual | What it actually grabbed |
|---|---|---|---|
| Total assets | $641.19B | **$4,424.90B** | `segments=Geographical=EMEA` |
| Equity | −$1.43B | **$362.44B** | `segments=EquityComponents=AccumulatedGainLossNetCashFlowHedgeParent` |

`Assets` appeared 23 times in that filing; exactly one row was consolidated. The
old parser could not have filtered these even in principle — it read only
`[adsh, tag, ddate, qtrs, uom, value]` out of `num.txt`, so `coreg` and
`segments`, the columns carrying the dimensional breakdown, were never in memory.

**Rule one — select the right fact** (`src/ingest/xbrl.py`):

- Consolidated only: `coreg` empty **and** `segments` empty.
- Balance-sheet items are instants: `qtrs == 0`. A nonzero `qtrs` on `Assets` is
  a *change* over a period, not a balance — a likely source of negative totals.
- Income and cash-flow items are durations: `qtrs` in `{1, 4}`.
- Money facts must be `uom == USD`.

**Rule two — validate before storing.** Rejected rows are logged with ticker,
tag, value and the rule that caught them, and are never written:

- Total assets must be positive. Zero or negative rejects the whole
  company-period; that filing's fact selection cannot be trusted.
- No component (cash, receivables, inventory, PPE, goodwill…) may exceed total
  assets.
- Assets ≈ liabilities + equity within 1% — **flagged**, not dropped, so a
  drifting identity stays visible instead of being silently deleted.
- A value >100× its own prior period is flagged as a units/scale problem.
- If validation rejects >20% of company-periods (over a meaningful sample), the
  load **fails** rather than writing partial data.

`tests/test_xbrl.py` encodes the real row shapes from a `num.txt` dump, so the
tests assert against the actual dimensional layout rather than an assumed one.
Every test there fails against the old extractor.

---

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # SEC_USER_AGENT is the only required variable

python -m src.migrate                              # create the schema
python -m src.backfill --days 600                  # price bars
python -m src.backfill --fundamentals              # SEC quarterly datasets

uvicorn src.api:app --reload                       # then open /admin
```

To wipe and reload fundamentals with a full extraction report and a check of the
five verification companies:

```bash
python3 scripts/reload_fundamentals.py --wipe --quarters 7
```

To inspect raw `num.txt` rows for a company — the tool that found the bug:

```bash
python3 scripts/dump_jpm_raw_facts.py --year 2026 --quarter 1 --ticker JPM
```

---

## Layout

```
src/
  ingest/      one module per data source, all behind one rate limiter
                 xbrl.py         the extraction filter + validation
                 sec_datasets.py the quarterly ZIP downloader/parser
  universe/    the SEC company list -> the ticker universe
  company/     balance-sheet query layer (point-in-time)
  storage/     models, point-in-time accessors, repository, cache
  config/      settings, rate limits
  report/      /admin page assets
  api.py       FastAPI
  backfill.py  the loaders
scripts/       operational tools (raw dump, reload+verify)
tests/
migrations/
```

---

## Endpoints

| Endpoint | What it does |
|---|---|
| `GET /health` | liveness + DB state |
| `GET /status` | row counts, backfill progress |
| `GET /admin` | the phone-first break-glass page |
| `GET /admin.json` | everything that page renders, in one payload |
| `GET /admin/balance-sheet?tickers=…` | per-concept values, the A = L + E check, and coverage |
| `GET /reconcile` | symbology / coverage / split-adjustment / recency |
| `POST /backfill?kind=…` | `bars` \| `sectors` \| `fundamentals` \| `earnings` |

`/admin` is the only UI. It found every bug in this project, which is why it is
kept — but it is an admin surface, not the product.

---

## Point-in-time correctness

A company with a fiscal quarter ending 2025-09-30 may not file its 10-Q until
2025-11-08. Using that data on 2025-10-01 is lookahead bias.

- Every fundamental datapoint stores `period_end`, `filing_date` (the SEC `filed`
  field), and `ingested_at`.
- Every read goes through `get_fundamentals(session, ticker, as_of)`, which
  enforces `filing_date + 2 business days <= as_of`. Nothing else queries the
  `fundamentals` table.
- **Restatements** resolve to what was actually known: before an amendment was
  filed you get the original figure; after, the restated one. Within a single
  load the earliest `filing_date` wins — as first reported.
- Fundamentals are never forward-filled across a reporting gap, and missing
  values are never imputed. **Missing is missing and says so.**
- The universe snapshot is persisted every day, including names that have since
  delisted.

`tests/test_pit.py` asserts all of this. It sweeps every date in the month before
a filing and fails if any of them can see the data.

---

## Data sources

| Source | Job |
|---|---|
| **SEC EDGAR** | The universe seed, and as-reported XBRL via the quarterly Financial Statement Data Sets. The source of truth. |
| **Stooq** | Keyless bulk daily bars — the whole market in one download. |
| **Polygon** | Optional. Faster whole-market bars when `POLYGON_API_KEY` is set and `POLYGON_TIER=paid`. |
| **FMP** | Optional. Market caps, GICS sectors, batch EOD. |

Rate limiting is one token bucket per source (`src/config/rate_limits.py`),
in-process by default. There is no `time.sleep()` anywhere; every request goes
through `RateLimiter.acquire()`.

---

## Types

Every value leaving the parser is coerced to a real, finite float or `None`
before anything touches it. pandas `NaN` is a `float`, passes an
`isinstance(x, float)` check, survives arithmetic as `NaN`, and lands in the DB
as a null-that-isn't. It has bitten this codebase repeatedly, so it is rejected
once, at the parser boundary, rather than defended against downstream forever.

---

## Tests

```bash
python -m pytest          # 191 tests
```

No network. The SEC datasets are exercised against synthetic ZIPs built to the
documented schema, with the extraction fixtures taken from a real dump.
