import { mkdirSync } from 'node:fs';

import { defineConfig, devices } from '@playwright/test';

import { DATA_DIR, SERVER_ENV } from './server-env';

// Here rather than in globalSetup, which Playwright runs AFTER it launches
// the server. The suite passed either way, but a server booting against a
// directory that does not exist yet logs a screenful of "unable to open
// database file" first, and a contributor's first run should not open with
// errors that turn out not to matter.
mkdirSync(DATA_DIR, { recursive: true });

/**
 * The dashboard's API-key flow lives almost entirely in the browser, which is
 * why it needs a browser to test. pytest can prove that /api/auth/me returns
 * a prefix; only this can prove the page renders the prefix rather than
 * "undefined", and that a key stored on another device is not shown as live.
 *
 * `localhost` and NOT `127.0.0.1`. The session cookie is `Secure`, and
 * browsers accept a Secure cookie over plain http only for a "potentially
 * trustworthy" origin. `localhost` qualifies by name; swap it for the loopback
 * IP and every signed-in assertion here fails as "not signed in", which looks
 * like a broken endpoint and is a cookie that was never stored.
 */
const PORT = Number(process.env.E2E_PORT ?? 8000);
const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`;

// Started by Playwright unless E2E_BASE_URL points somewhere already running.
const START_SERVER = !process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: './tests',
  // Creates (and empties) the directory the throwaway database lives in.
  // SQLite makes the file, never the folder above it.
  globalSetup: './global-setup.ts',
  // Serial. These tests register accounts against one SQLite file and the
  // register endpoint is rate limited per hour; running them in parallel buys
  // seconds and costs a flaky 429.
  workers: 1,
  fullyParallel: false,
  // A failing assertion here is a real bug, not a timing fluke. Retrying would
  // turn "the key did not render" into an intermittent pass.
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  webServer: START_SERVER
    ? {
        command:
          'python -m uvicorn src.api:app --host 127.0.0.1 --port ' + PORT,
        cwd: '..',
        url: `${BASE_URL}/health`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        stdout: 'pipe',
        stderr: 'pipe',
        env: SERVER_ENV,
      }
    : undefined,
});
