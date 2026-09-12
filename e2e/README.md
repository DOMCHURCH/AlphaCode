# Browser tests

The flows that only exist in a browser. Right now that is the dashboard's
API-key handling, which pytest cannot reach.

Keys are stored as SHA-256 digests, so the server cannot return one after the
response that issued it. Everything a reader sees afterwards is decided in
JavaScript: whether this device kept a copy, whether that copy still belongs
to this account, and what to show when it does not. pytest can prove
`/api/auth/me` returns a prefix. Only this can prove the page renders the
prefix rather than the string `undefined`, and that a key regenerated on
another device is not shown here as live.

## Running it

```bash
cd e2e
npm install
npx playwright install --with-deps chromium   # first run only, downloads the browser
npx playwright test
```

Playwright starts the API itself on port 8000, against a throwaway SQLite file
at `e2e/.data/e2e.db`. Your own database is never touched and nothing is sent
over the network: the mailer has no key, so it reports itself unconfigured and
never dials out.

`global-setup.ts` runs `python -m src.migrate` against that file first, and
that is load-bearing rather than tidy. The API binds and answers `/health`
before `init_db` has finished, on purpose, so that a slow database cannot fail
a healthcheck in production. Playwright only looks at the status code, so
without the explicit migration it starts testing against a database with no
tables: every write 500s, the key box keeps its placeholder, and the suite
passes or fails depending on whether a warm server happened to be left over
from the last run.

The Python environment has to be able to run `python -m uvicorn src.api:app`
from the repository root. If your interpreter lives in a virtualenv, activate
it before `npx playwright test` in the same shell.

Useful variations:

```bash
npx playwright test --headed          # watch it happen
npx playwright test --debug           # step through
npx playwright show-report            # the HTML report after a failure
E2E_PORT=8123 npx playwright test     # if 8000 is taken
```

Point it at a server you are already running, and Playwright will not start
one:

```bash
E2E_BASE_URL=http://localhost:8000 npx playwright test
```

Use `localhost` and not `127.0.0.1`. The session cookie is `Secure`, and a
browser accepts one over plain http only for an origin it considers
potentially trustworthy. `localhost` qualifies by name; the loopback IP does
not reliably, and every signed-in assertion then fails as "not signed in" —
which looks like a broken endpoint and is really a cookie that was never
stored.

## What is deliberately not here

**No secrets.** `SESSION_SECRET` and `ADMIN_SECRET` are throwaway strings in
`playwright.config.ts`. There is no Stripe key and no SEC key, because nothing
in this flow touches either. If a test ever needs one, it belongs in CI
secrets and not in this directory.

**No demo account.** `DEMO_API_KEY` is blank on purpose. The demo is a shared
row with a well-known key, and these tests are about keys that belong to one
person.

**No production.** The config has no way to point at toscale.pro that does not
involve typing it into `E2E_BASE_URL` yourself. These tests register accounts
and regenerate keys; running them against the live site would create real rows
and invalidate a real customer's credential.

## Why this is its own package

Nothing in the Python project needs Node, and nothing here belongs in
`requirements.txt`. `e2e/node_modules` is gitignored; the only tracked files
are the config, the specs, and this README.

## Adding a test

One address per test, generated with `freshEmail()`. Sharing one makes the
suite order-dependent, and the first thing to break is the regenerate test
invalidating a key another test is still using.

`workers: 1`, and leave it there. Registration is rate limited per hour;
parallelism buys seconds and costs a flaky 429.

If your test clicks something behind a `confirm()`, call `acceptConfirms(page)`
first. Playwright dismisses dialogs by default, so the click silently does
nothing and the failure looks like a broken button.
