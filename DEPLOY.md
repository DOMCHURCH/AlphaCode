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

   **For the paid API** (`/dashboard`, `/api/*`, `/admin/grant-access`):

   | Variable | Value |
   |---|---|
   | `ADMIN_SECRET` | `openssl rand -hex 32` &nbsp;**(required to grant anything)** |
   | `ADMIN_EMAIL` | the address buyers send payment and their key to |
   | `FREE_TIER_MONTHLY_CALLS` | `10` (default) |
   | `PRO_TIER_MONTHLY_CALLS` | `10000` (default) |
   | `DATASET_PRICE_USD` / `PRO_PRICE_USD` | `29` / `49` (defaults) |

   `ADMIN_SECRET` unset does **not** leave the grant endpoint open — it returns
   503 and nothing can be granted. Set it before taking money, not after.

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

## Dashboard login (magic links)

`/login` takes an address and emails a one-time link; clicking it signs the
person in and, if they are new, creates the account. Requires:

| Variable | Value |
|---|---|
| `SESSION_SECRET` | `openssl rand -hex 32` &nbsp;**(unset = login disabled, 503)** |
| `BASE_URL` | `https://alphacode-production.up.railway.app` — the link's base |
| `AGENTMAIL_API_KEY` | the link is an email; no mail, no login |

Notes that matter when this misbehaves:

* **The emailed link is a GET that does not spend the token.** Mail scanners and
  link prefetchers fetch every URL in a message; a GET that consumed the token
  would mean the recipient's own click always landed on a used link. The page
  POSTs to consume it, which prefetchers do not do.
* **`BASE_URL` wrong = every link broken.** It is the one setting with no
  sensible failure mode — the link simply points elsewhere.
* Links last 15 minutes, work once, and one address can only be mailed every
  20 minutes.
* The session cookie is `HttpOnly; Secure; SameSite=Lax`. **Secure means it will
  not be set over plain http**, so local testing needs https or a tolerant
  browser; on Railway everything is https already.
* Regenerating an API key needs the session, never the key — the reason to press
  it is that the key leaked, and a key that can rotate itself locks its owner out.

## Key recovery (email)

"Resend my key" on `/dashboard` sends through [AgentMail](https://agentmail.to).
Set `AGENTMAIL_API_KEY` and it works; leave it unset and the button explains
that email is off and names `ADMIN_EMAIL` instead. Nothing else is required —
the mailbox is created on first send under a fixed `client_id` and reused, or
pin one with `AGENTMAIL_INBOX_ID`.

**On the free plan mail leaves from an `@agentmail.to` address.** That message
carries an API key, so it is a spam-folder candidate from an unfamiliar domain
— and the entire point is that the person gets their key back. Budget for the
paid plan and a custom domain before relying on this.

Failures are logged as `agentmail_send_failed` with an `HTTP <status>: <message>`
line, never a header dump. The request itself returns immediately: sending runs
in a background task behind a 10s timeout, so a slow upstream costs one worker
thread and not the response.

## Pro subscriptions

`grant_pro` now sets an expiry (`PRO_PERIOD_DAYS`, default 31) and **extends**
rather than resets — renewing early adds to what is left instead of throwing it
away. `revoke_pro` backdates the expiry rather than clearing it, because a NULL
expiry means *never expires* (a comped account), not *expired*.

A lapsed subscription falls back to the **free allowance**, not to nothing: the
key keeps working at 10 calls/month. Nothing is deleted and nothing is emailed
to the customer automatically.

See who is due:

```bash
curl -H "X-Admin-Secret: $ADMIN_SECRET" https://<host>/admin/subscriptions
```

The `subscriptions` scheduler job mails `ADMIN_EMAIL` **one** message listing
everybody expiring within `PRO_REMINDER_DAYS`, once per subscription period.
It needs the scheduler running (`ENV=prod`, or `ENABLE_SCHEDULER=true`) and
`AGENTMAIL_API_KEY` set; without either it reports itself as off on `/admin`
rather than failing every twelve hours. **In dev it never runs at all.**

## Granting paid access (the whole billing system)

There is no payment processor. Somebody e-transfers or PayPals you and emails
their API key; you run one command. Four actions:
`grant_download`, `grant_pro`, `revoke_download`, `revoke_pro`.

```bash
curl -X POST https://<service>.up.railway.app/admin/grant-access \
  -H "X-Admin-Secret: $ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"email":"buyer@example.com","action":"grant_download"}'
```

Notes worth having in front of you when a payment lands:

* **Match on email, not key.** The buyer quotes a key; you grant by the address
  they registered. If the addresses do not match, ask — do not guess.
* **Idempotent.** Running the same grant twice is a no-op, so a re-run after a
  dropped connection is safe.
* **404 means they never registered.** Send them to `/dashboard` first.
* **Every grant, and every rejected secret, is a row in `admin_actions`.** That
  table is the record of what was sold, since container logs rotate away.
* Free/Pro is a per-calendar-month call allowance; the dataset download is a
  separate one-time boolean. Someone can have either, both, or neither.

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
