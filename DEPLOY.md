# Deploying to Railway

The whole thing is **one repo → one service → Postgres + Redis**. No separate
frontend (the API renders and serves the HTML), and no separate worker unless
you want one — the daily cron runs inside the API process.

```
┌──────────────────────────┐      ┌────────────┐
│  service (this repo)      │─────▶│  Postgres  │
│  uvicorn src.api:app      │      └────────────┘
│  • serves reports + JSON  │      ┌────────────┐
│  • runs the 06:00 funnel  │─────▶│   Redis    │
│    (ENABLE_SCHEDULER=true)│      └────────────┘
└──────────────────────────┘
```

## Steps

1. **Project + plugins.** railway.app → New Project → Deploy from GitHub repo →
   pick this repo/branch. Then **+ New → Database → PostgreSQL**, and again for
   **Redis**.

2. **The service is already created from the repo.** It builds via `nixpacks.toml`
   (installs cairo/pango for the PDF) and starts `uvicorn src.api:app`. Config
   path is `railway.toml` (the default).

3. **Variables** (Service → Variables):
   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | Add Reference → `Postgres.DATABASE_URL` |
   | `REDIS_URL` | Add Reference → `Redis.REDIS_URL` |
   | `ENABLE_SCHEDULER` | `true` &nbsp;← makes this one service run the daily cron |
   | `POLYGON_API_KEY` `FMP_API_KEY` `FINNHUB_API_KEY` `FRED_API_KEY` `OPENROUTER_API_KEY` | your keys |
   | `SEC_USER_AGENT` | `Your Name your@email.com` (SEC 403s without it) |
   | `API_KEY` | a long random string (protects `POST /run`) |
   | `ENV` | `prod` |

   `DATABASE_URL` can be a `postgres://` or `postgresql://` URL — the app
   normalizes it and uses the psycopg2 driver.

4. **Deploy.** On boot the service migrates the schema itself (tables + indexes
   against Postgres) and, with `ENABLE_SCHEDULER=true`, starts the 06:00
   America/New_York weekday cron. Check `https://<service>.up.railway.app/health`
   → `{"status":"ok","database":"ok"}`.

5. **Backfill once** (Stage 1 needs ~200 days of history; the pipeline aborts on
   an empty universe rather than shipping junk). In the service shell (or
   `railway run` locally against the same `DATABASE_URL`):
   ```bash
   python -m src.backfill --days 600                    # ~500 Polygon calls
   python -m src.backfill --fundamentals --skip-bars    # SEC XBRL, slow
   ```

6. **First run + read it.**
   ```bash
   curl -X POST "https://<service>.up.railway.app/run" -H "X-API-Key: <API_KEY>"
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
