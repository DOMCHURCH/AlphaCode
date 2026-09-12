import { resolve } from 'node:path';

export const REPO_ROOT = resolve(__dirname, '..');
export const DATA_DIR = resolve(__dirname, '.data');

/**
 * Everything the API needs to run as a disposable local instance.
 *
 * Shared by `playwright.config.ts`, which starts the server with it, and
 * `global-setup.ts`, which creates the schema with it. One definition,
 * because a setup that migrates a different database from the one the server
 * opens fails in the least obvious way available: the server answers, the
 * tables are missing, and every write 500s.
 */
export const SERVER_ENV: Record<string, string> = {
  // A throwaway file, not the developer's own database.
  DATABASE_URL: 'sqlite:///./e2e/.data/e2e.db',
  ENV: 'dev',
  // Required for sessions to exist at all. Test values, and the only secrets
  // in this directory -- there are no Stripe or SEC keys here, because
  // nothing in this flow touches either.
  SESSION_SECRET: 'e2e-session-secret-not-for-real-use',
  ADMIN_SECRET: 'e2e-admin-secret-not-for-real-use',
  ADMIN_EMAIL: 'e2e@example.invalid',
  // bcrypt work factor. Four rounds, because nothing here asserts anything
  // about how slow a password hash is.
  BCRYPT_ROUNDS: '4',
  // The suite registers a handful of accounts per run and the default is
  // 20/hour, which turns a third local run into a wall of 429s.
  REGISTER_RATE_PER_HOUR: '1000',
  LOGIN_RATE_PER_HOUR: '1000',
  MAGIC_LINK_RATE_PER_HOUR: '1000',
  RESEND_RATE_PER_HOUR: '1000',
  // Off on purpose. With no key the mailer reports itself unconfigured and
  // never reaches the network, so a test run cannot send mail to anybody.
  AGENTMAIL_API_KEY: '',
  // No demo account: it is a shared row with a well-known key, and these
  // tests are about keys that belong to one person.
  DEMO_API_KEY: '',
};
