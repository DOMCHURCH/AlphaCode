# Deploying to Railway

The system is **one Python codebase** that runs as **two services + two plugins**.
There is no separate frontend — the "UI" is HTML the FastAPI service renders and
serves.

```
┌─────────────┐     reads/writes      ┌────────────┐
│  worker     │──────────────────────▶│  Postgres  │◀───────┐
│ (scheduler) │                       └────────────┘        │
│ runs funnel │                       ┌────────────┐        │ reads
│ 06:00 ET    │──────────────────────▶│   Redis    │◀──┐    │
└─────────────┘   rate limits/cache   └────────────┘   │    │
                                                        │    │
┌─────────────┐                                         │    │
│   api       │─────────────────────────────────────────┴────┘
│ (FastAPI)   │  serves /report /ticker /validation /run
└─────────────┘
```

Both `worker` and `api` deploy from **this same repo**; only the start command
differs. Rendered reports are stored in Postgres (`report_artifacts` table), so
the `api` service can serve a report the `worker` rendered even though they have
separate, ephemeral filesystems.

## One-time setup

### 1. Create the project and plugins
- New Railway project → add a **PostgreSQL** plugin and a **Redis** plugin.

### 2. Create the `api` service
- New service → deploy from this repo.
- Settings → **Config-as-code path**: `railway.toml` (this is the default).
- It builds via `nixpacks.toml` (installs cairo/pango for PDF, runs
  `python -m src.migrate` so the schema is created/updated on every deploy).
- Start command (from `railway.toml`): `uvicorn src.api:app --host 0.0.0.0 --port $PORT`.
- Healthcheck: `/health`.

### 3. Create the `worker` service
- New service → deploy from the **same repo**.
- Settings → **Config-as-code path**: `railway.worker.toml`.
- Start command: `python -m src.scheduler` (in-process APScheduler cron at
  06:00 America/New_York, weekdays, holiday-guarded).

### 4. Set environment variables on BOTH services
Railway injects `DATABASE_URL`/`REDIS_URL` when you add plugin references. Set
the rest from `.env.example`:

| Variable | Where it comes from |
|---|---|
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (reference) |
| `REDIS_URL` | `${{Redis.REDIS_URL}}` (reference) |
| `POLYGON_API_KEY` | polygon.io (paid tier for the daily grouped call) |
| `FMP_API_KEY` | financialmodelingprep.com |
| `FINNHUB_API_KEY` | finnhub.io (free tier is fine) |
| `FRED_API_KEY` | fred.stlouisfed.org (free) |
| `OPENROUTER_API_KEY` | openrouter.ai |
| `SEC_USER_AGENT` | `"Your Name your@email.com"` — SEC 403s without a real UA |
| `ENV` | `prod` |
| `MAX_RUN_COST_USD` | `2.0` (alerts if a run exceeds it) |

The `api` strictly needs only `DATABASE_URL`/`REDIS_URL`, but setting the full
set on both is simplest and harmless.

## Before the first run: backfill

Stage 1 needs ~200 trading days of history before it can compute anything, and
the pipeline **aborts loudly** on an empty universe rather than shipping a bad
report. So seed history once (needs the Polygon key + working egress). Run these
from a one-off Railway shell on the `worker` service (or locally against the
same `DATABASE_URL`):

```bash
python -m src.backfill --days 600                    # ~500 grouped-daily calls
python -m src.backfill --fundamentals --skip-bars    # SEC XBRL, slow (hours for a full universe)
```

## Trigger and read

- Manual run: `POST /run` (or `POST /run?skip_llm=true` for a deterministic-only
  run that spends no tokens).
- Then: `GET /report/latest/html`, `GET /report/{date}/pdf`,
  `GET /report/{date}` (JSON), `GET /ticker/{symbol}/history`, `GET /validation`.
- Resume a crashed run: `python -m src.pipeline --resume --date YYYY-MM-DD`.

## Network policy

Egress must permit the data hosts: `api.polygon.io`, `financialmodelingprep.com`,
`finnhub.io`, `api.stlouisfed.org`, `data.sec.gov`, `api.gdeltproject.org`,
`openrouter.ai`. If your Railway environment restricts egress, allowlist these
or the ingest stages will fail.

## What "ready" means here

The code, config, schema migration, and cross-service report serving are done
and verified offline (185 tests, deterministic funnel + DB-served report proven
end-to-end). What has **not** been run is a live pass against the real APIs —
that requires the keys and egress above, and can only happen in your Railway
environment. The honest expectation: first live run may surface
vendor-response-shape quirks (field names, pagination) that only real payloads
reveal; the ingest modules degrade and log rather than crash, but read the first
run's logs.
