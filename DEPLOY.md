# Deploying to Railway

**One repo → one service → Postgres.** No worker (there is no cron), no Redis
(the cache and rate-limiter run in-process).

```
┌──────────────────────────┐      ┌────────────┐
│  service (this repo)     │─────▶│  Postgres  │
│  uvicorn src.api:app     │      └────────────┘
│  • /admin + JSON API     │
│  • on-demand backfills   │   (cache + rate-limits: in-process)
└──────────────────────────┘
```

## Steps

1. **Project + database.** railway.app → New Project → Deploy from GitHub repo →
   pick this repo/branch. Then **+ New → Database → PostgreSQL**. That's the only
   plugin you need.

2. **The service is already created from the repo.** It builds via `nixpacks.toml`
   and starts `uvicorn src.api:app`. Config path is `railway.toml` (the default).

3. **Variables** (Service → Variables):
   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Add Reference → `Postgres.DATABASE_URL` |
   | `SEC_USER_AGENT` | `Your Name your@email.com` &nbsp;**(required)** — SEC 403s without it |
   | `ENV` | `prod` |

   **Everything runs on free data.** The universe comes from SEC's company list,
   fundamentals from SEC's quarterly Financial Statement Data Sets, and prices
   from Yahoo's batched daily download — **no market-data keys are required**.
   `SEC_USER_AGENT` is the one must-set variable.

   Optional upgrades, used automatically when set: `POLYGON_API_KEY` (faster,
   whole-market bars), `FMP_API_KEY` (market caps, GICS sectors).

   No `REDIS_URL` needed. No `API_KEY` needed — `/backfill` is open and
   rate-limited.

   `DATABASE_URL` can be a `postgres://` or `postgresql://` URL — the app
   normalizes it and uses the psycopg2 driver.

4. **Deploy.** On boot the service migrates the schema itself (tables + indexes
   against Postgres). Check `https://<service>.up.railway.app/health` →
   `{"status":"ok","database":"ok"}`.

5. **It keeps itself current — nothing to schedule.** With `ENV=prod` the
   auto-updater is on by default (no extra variable), and the "Keeping itself
   current" card on `/admin` shows what it has done and what it is waiting on.
   It works out what is behind by READING THE DATA, not by watching a clock:

   | Job | Runs when |
   |---|---|
   | `bars` | the newest bar is older than the last completed trading session |
   | `filings` | anything filed since the newest `filing_date` we hold (every 6h) |
   | `fundamentals` | the newest published SEC quarter is not in `fundamentals` yet |
   | `earnings` | the same quarter is not in `earnings_events` yet |

   `filings` is the one that keeps a company page current. It reads SEC's XBRL
   **frames** API — one request returns a concept for every filer in a period,
   so the whole market's newest quarter costs ~48 requests and about half a
   minute, not one request per company. Frames carry no filing date, so each
   fact's accession number is joined to EDGAR's form index to get the real one;
   a fact that cannot be dated honestly is dropped rather than dated by
   guesswork. That gap matters: a 10-Q filed in August does not appear in any
   bulk dataset until November.

   That is what makes late work catch itself up. SEC publishes a quarter's
   dataset weeks after the quarter ends and on no announced date, so the
   quarterly jobs look for it and, if it is not up, look again in 12 hours —
   reported as *waiting*, not as a failure. A deploy in the middle of a
   quarter, a container that was down for a week, or a restored database all
   resolve the same way: the first check after boot sees the gap and closes it.
   Real failures (an unreachable source, a throttle) back off 30 min → 1h → 2h,
   capped at 12h, and are surfaced on `/admin` rather than retried silently.

   Every wait is stored in the `job_state` table, so a restart cannot turn a
   quarterly job into a per-deploy job. Knobs, all optional: `AUTO_UPDATE`
   (`auto`|`on`|`off`), `AUTO_UPDATE_TICK_MINUTES`, `AUTO_UPDATE_QUARTERS`,
   `AUTO_UPDATE_BARS_MIN_HOURS`, `AUTO_UPDATE_SEC_RECHECK_HOURS`,
   `AUTO_UPDATE_BACKOFF_MAX_HOURS`.

6. **Load the data now, if you don't want to wait for the first check.** Open
   `/admin` and tap the backfill buttons, or drive them by hand:
   ```bash
   curl -X POST "https://<service>.up.railway.app/backfill?kind=bars&days=600"
   curl -X POST "https://<service>.up.railway.app/backfill?kind=sectors"
   curl -X POST "https://<service>.up.railway.app/backfill?kind=fundamentals"
   curl      "https://<service>.up.railway.app/status"     # watch progress
   ```

7. **Verify the fundamentals actually reconcile.** The fundamentals load is the
   one that has been wrong before — the old extractor stored segment and equity
   rollforward facts as company totals. After loading, open `/admin` and tap
   **Balance sheet**, or:
   ```bash
   curl "https://<service>.up.railway.app/admin/balance-sheet?tickers=JPM,AAL,MSFT,WMT,FCX"
   ```
   JPM total assets must read ≈ $4.42T and equity ≈ $362B. If they don't, the
   extractor is wrong again — do not build on the numbers. The **XBRL extraction**
   panel on `/admin` shows what the consolidated filter dropped and why.

   To wipe and reload from scratch with a full report, from a shell:
   ```bash
   python3 scripts/reload_fundamentals.py --wipe --quarters 7
   ```

8. **Reconcile the keyless price source (first live load, once).** Synthetic tests
   prove the loader works, not that the real feed is what we assume:
   ```bash
   python -m src.reconcile          # symbology / coverage / adjustment / recency
   ```
   **The one that matters is `adjustment`:** if any names read `unadjusted`, the
   source is serving raw (non-split-adjusted) prices — switch to
   `POLYGON_API_KEY`, which is adjusted. Don't assume — read the numbers.
   (Yahoo is requested with `auto_adjust=True`, so it should read adjusted.)

   **On the price source.** Stooq's bulk archive used to be the keyless
   primary. It is gone: `stooq.com` now answers every request with a JavaScript
   proof-of-work browser challenge, and `/db/h/d_us_txt.zip` returns a real
   "page does not exist". Yahoo took its place — batched 200 symbols per call,
   so a few thousand names cost tens of requests, not thousands. Stooq is still
   in the chain behind Yahoo, because one download of the entire market's
   history is the better source if it ever returns.

## Local dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # leave DATABASE_URL as sqlite for local
python -m src.migrate
uvicorn src.api:app --reload
```

SQLite is the local default; Postgres is used whenever `DATABASE_URL` points at
one. Nothing else changes between the two.

## Egress

Allowlist these hosts if the environment restricts outbound traffic:
`www.sec.gov` + `data.sec.gov` (universe seed, fundamentals datasets, XBRL
frames + form index), `query1.finance.yahoo.com` / `query2.finance.yahoo.com`
(keyless prices, via yfinance). `stooq.com` is still tried as a fallback.
Optional upgrades: `api.polygon.io`, `financialmodelingprep.com`.

## What's verified vs not

Code, config, schema migration, Postgres compatibility, and the XBRL extraction
filter are covered by the offline suite. The extraction tests are built from a
real `num.txt` dump, so they encode the actual dimensional layout rather than an
assumed one.

Verified against live SEC and Yahoo, not just offline: the frames sweep (80,804
rows for one quarter across 4,644 tickers, with the accounting identity holding
to 0.0000% on every name that tags all three sides), the bulk dataset load, and
the Yahoo bar load. What is still **not** verified outside a deploy is the
behaviour under Postgres and Railway's own network. Run step 6 and read the
numbers before trusting anything downstream.
