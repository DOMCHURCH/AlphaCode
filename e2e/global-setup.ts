import { execFileSync } from 'node:child_process';

import { REPO_ROOT, SERVER_ENV } from './server-env';

/**
 * Make the database the server is about to use, and put the schema in it.
 *
 * Two separate hazards, both of which produced the same symptom: register
 * answers 500 and the key box stays on its em-dash placeholder.
 *
 * 1. SQLite creates the FILE but never the folder above it, so the data
 *    directory has to exist first -- made in playwright.config.ts, which is
 *    loaded before the server is launched, unlike this.
 * 2. The API binds and answers /health BEFORE `init_db` has finished --
 *    deliberately, so a slow Postgres cannot fail a healthcheck in
 *    production. /health then returns 200 with `database: degraded`, and
 *    Playwright, which only looks at the status code, starts testing against
 *    a database with no tables in it. That is a race, so it passed whenever
 *    a warm server was reused from a previous run and failed on every cold
 *    start -- the worst possible shape for a test to fail in.
 *
 * Running the project's own migration entrypoint here removes the race
 * instead of waiting it out: by the time the first test runs, the tables are
 * there because this put them there. Idempotent, and the server's own boot
 * pass is then a no-op.
 */
export default function globalSetup(): void {
  execFileSync('python', ['-m', 'src.migrate'], {
    cwd: REPO_ROOT,
    env: { ...process.env, ...SERVER_ENV },
    stdio: 'inherit',
  });
}
