# Deploying to Railway

The whole thing is **one repo → one service → Postgres**. No separate frontend
(the API renders and serves the HTML), no worker (the daily cron runs inside the
API process), and no Redis (the cache and rate-limiter run in-process).

```
┌──────────────────────────┐      ┌────────────┐
│  service (this repo)      │─────▶│  Postgres  │
│  uvicorn src.api:app      │      └────────────┘
│  • serves reports + JSON  │
│  • runs the 06:00 funnel  │   (cache + rate-limits: in-process)
│    (ENABLE_SCHEDULER=true)│
└──────────────────────────┘
```

## Steps

1. **Project + database.** railway.app → New Project → Deploy from GitHub repo →
   pick this repo/branch. Then **+ New → Database → PostgreSQL**. That's the only
   plugin you need.

2. **The service is already created from the repo.** It builds via `nixpacks.toml`
   (installs cairo/pango for the PDF) and starts `uvicorn src.api:app`. Config
   path is `railway.toml` (the default).

3. **Variables** (Service → Variables):
   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Add Reference → `Postgres.DATABASE_URL` |
   | `ENABLE_SCHEDULER` | `true` &nbsp;← makes this one service run the daily cron |
   | `SEC_USER_AGENT` | `Your Name your@email.com` &nbsp;**(required)** — SEC 403s without it |
   | `OPENROUTER_API_KEY` | your key — only for the Stage 4–5 LLM write-ups |
   | `ENV` | `prod` |

   **The funnel runs on free data by default** — the universe comes from SEC's
   company list and prices from Yahoo, so **no market-data keys are required**.
   `SEC_USER_AGENT` is the one must-set variable. `OPENROUTER_API_KEY` is only
   needed for the LLM thesis stages (without it, run with `skip_llm` and you
   still get the ranked, deterministic top-10).

   Optional upgrades (set any of these and the funnel uses them automatically):
   `POLYGON_API_KEY` (faster, whole-market bars + market caps via `FMP_API_KEY`),
   `FINNHUB_API_KEY`, `FRED_API_KEY` (macro regime tilt).

   (No `REDIS_URL` needed — leave it unset. No `API_KEY` needed either — the
   site's one button drives `/backfill` and `/run` with no token.)

   `DATABASE_URL` can be a `postgres://` or `postgresql://` URL — the app
   normalizes it and uses the psycopg2 driver.

4. **Deploy.** On boot the service migrates the schema itself (tables + indexes
   against Postgres) and, with `ENABLE_SCHEDULER=true`, starts the 06:00
   America/New_York weekday cron. Check `https://<service>.up.railway.app/health`
   → `{"status":"ok","database":"ok"}`.

5. **Just open the site and press the button.** On a fresh deploy the one button
   (`Research today's best stocks`) loads about a year of history itself
   (Stage 1 needs 252 trading days), then runs the funnel and shows the top-10 —
   no shell and no token. The first-time history load is slow on a rate-limited
   data plan and keeps running server-side, so you can leave and come back.

   Prefer to drive it by hand? The same endpoints are open (no `X-API-Key`):
   ```bash
   curl -X POST "https://<service>.up.railway.app/backfill?days=600"   # load history
   curl -X POST "https://<service>.up.railway.app/run"                 # run the funnel
   curl      "https://<service>.up.railway.app/status"                 # watch progress
   ```
   Then open `/report/latest/html`. After that the cron runs it every weekday
   morning. Endpoints: `/report/{date}` (JSON), `/report/{date}/pdf`,
   `/ticker/{symbol}/history`, `/validation`.

## Local dev

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # fill keys; leave DATABASE_URL as sqlite for local
python -m src.migrate
uvicorn src.api:app --reload    # ENABLE_SCHEDULER unset -> no cron locally
```

SQLite is the local default; Postgres is used whenever `DATABASE_URL` points at
one. Nothing else changes between the two.

## If you outgrow one service

Split into `api` + `worker`: add a second service from the same repo, set its
config path to `railway.worker.toml` (starts `python -m src.scheduler`), and set
`ENABLE_SCHEDULER` back to unset/false on the api service so the cron only runs
in one place. Reports are stored in Postgres, so either service can serve them.

## Egress

Allowlist these hosts if the environment restricts outbound traffic:
`api.polygon.io`, `financialmodelingprep.com`, `finnhub.io`,
`api.stlouisfed.org`, `data.sec.gov`, `api.gdeltproject.org`, `openrouter.ai`.

## What's verified vs not

Code, config, schema migration, Postgres compatibility, single-service scheduler,
and cross-service report serving are done and tested offline (194 tests). The
one thing not run is a live pass against the real vendor APIs — that needs the
keys + egress above and happens in your Railway environment. Read the first
run's logs; live payloads occasionally differ from the documented shapes.
