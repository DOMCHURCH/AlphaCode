import { test, expect, type Page } from '@playwright/test';

/**
 * The dashboard API-key flow.
 *
 * Keys are stored as SHA-256 digests, so the server cannot return one after
 * the response that issued it. Everything the reader sees afterwards is
 * decided in the browser: whether this device kept a copy, whether that copy
 * still belongs to this account, and what to show when it does not. None of
 * that is reachable from pytest, and all of it is one `undefined` away from
 * showing a signed-in customer an empty box where their key used to be.
 *
 * Each test registers its own address. Sharing one would make the suite
 * order-dependent, and the first thing that breaks then is the regenerate
 * test invalidating the key another test is still using.
 */

const KEY_STORE = 'toscale.api_key';
const PASSWORD = 'correct horse battery staple';

/** A unique address per test, so nothing here depends on run order. */
function freshEmail(label: string): string {
  return `e2e-${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.invalid`;
}

/** Playwright dismisses `confirm()` by default; Regenerate is behind one. */
async function acceptConfirms(page: Page): Promise<void> {
  page.on('dialog', (d) => d.accept());
}

async function storedKey(page: Page): Promise<string | null> {
  return page.evaluate((k) => localStorage.getItem(k), KEY_STORE);
}

/** Register through the form, the way a reader does. Returns the shown key. */
async function registerInBrowser(page: Page, email: string): Promise<string> {
  await page.goto('/dashboard');
  await page.fill('#email', email);
  const terms = page.locator('#accept-terms');
  if (await terms.count()) await terms.check();
  await page.click('#reg-btn');
  await expect(page.locator('#key-value')).not.toHaveText('—', { timeout: 15_000 });
  return (await page.locator('#key-value').innerText()).trim();
}

test.describe('API key, shown once', () => {
  test('registration shows the full key and this browser keeps it', async ({ page }) => {
    const email = freshEmail('reg');
    const key = await registerInBrowser(page, email);

    // A real key, not a prefix and not the em-dash placeholder.
    expect(key).toMatch(/^[A-Za-z0-9_-]{40,}$/);
    expect(key.endsWith('…')).toBe(false);

    // The copy button is only useful next to something copyable.
    await expect(page.locator('#copy-key')).toBeVisible();

    // The response that issued it is the only place it exists, so the page
    // storing it here is what makes it readable on the next load.
    expect(await storedKey(page)).toBe(key);
  });

  test('a reload still shows the key this browser stored', async ({ page }) => {
    const email = freshEmail('reload');
    const key = await registerInBrowser(page, email);

    await page.reload();

    // /api/auth/me answers first on load and carries no key. If its handler
    // called setKey with the missing field, this is where the stored copy
    // would be wiped and the box would go blank.
    await expect(page.locator('#key-value')).toHaveText(key);
    expect(await storedKey(page)).toBe(key);
  });
});

test.describe('Signed in, with no local copy of the key', () => {
  test('shows the prefix and offers to regenerate, never the key', async ({ page }) => {
    const email = freshEmail('prefix');

    // Registered over the API so the browser never sees the key: the same
    // state as signing in on a second device, or clearing site data.
    const created = await page.request.post('/api/auth/register-password', {
      data: { email, password: PASSWORD, accept_terms: true },
    });
    expect(created.status()).toBe(201);
    const issued = (await created.json()).api_key as string;
    expect(issued).toBeTruthy();

    await page.goto('/dashboard');
    await page.evaluate(() => localStorage.clear());
    await page.goto('/dashboard');

    const shown = (await page.locator('#key-value').innerText()).trim();
    expect(shown).toBe(issued.slice(0, 8) + '…');
    expect(shown).not.toContain(issued);

    // Nothing to copy, and a way out that does not require the lost key.
    await expect(page.locator('#copy-key')).toBeHidden();
    await expect(page.locator('#regen-box')).toBeVisible();

    // The state has to explain itself, or it reads as a bug.
    const note = (await page.locator('#key-note').innerText()).trim();
    expect(note.length).toBeGreaterThan(30);
    expect(note.toLowerCase()).toContain('hashed');

    // And the prefix must not be mistaken for a key worth storing.
    expect(await storedKey(page)).toBeNull();
  });

  test('the dataset page keeps its paste box instead of filling it in', async ({ page }) => {
    const email = freshEmail('dataset');
    const created = await page.request.post('/api/auth/register-password', {
      data: { email, password: PASSWORD, accept_terms: true },
    });
    expect(created.status()).toBe(201);

    await page.goto('/dataset');
    await page.evaluate(() => localStorage.clear());
    await page.goto('/dataset');

    // The download authenticates with the KEY, not the session cookie. Being
    // signed in is no longer enough to fill the field in for somebody.
    await expect(page.locator('#dl-key-field')).toBeVisible();
    await expect(page.locator('#dl-key')).toHaveValue('');
  });
});

test.describe('Regenerate', () => {
  test('issues a new key, kills the old one, and shows the new one in full',
    async ({ page }) => {
      await acceptConfirms(page);
      const email = freshEmail('regen');

      const created = await page.request.post('/api/auth/register-password', {
        data: { email, password: PASSWORD, accept_terms: true },
      });
      expect(created.status()).toBe(201);
      const oldKey = (await created.json()).api_key as string;

      await page.goto('/dashboard');
      await page.evaluate(() => localStorage.clear());
      await page.goto('/dashboard');

      await page.click('#regen-btn');
      await expect(page.locator('#key-value')).not.toHaveText(
        oldKey.slice(0, 8) + '…', { timeout: 15_000 },
      );

      const newKey = (await page.locator('#key-value').innerText()).trim();
      expect(newKey).toMatch(/^[A-Za-z0-9_-]{40,}$/);
      expect(newKey).not.toBe(oldKey);

      // The point of the button: the leaked key stops working immediately.
      const withOld = await page.request.get('/api/user/status', {
        headers: { 'X-API-Key': oldKey },
      });
      expect(withOld.status()).toBe(401);

      const withNew = await page.request.get('/api/user/status', {
        headers: { 'X-API-Key': newKey },
      });
      expect(withNew.status()).toBe(200);

      // Stored on the way out, so a refresh does not lose the one copy.
      expect(await storedKey(page)).toBe(newKey);
      await page.reload();
      await expect(page.locator('#key-value')).toHaveText(newKey);
    });

  test('a key left over from another device is not rendered as live',
    async ({ page }) => {
      await acceptConfirms(page);
      const email = freshEmail('stale');

      const created = await page.request.post('/api/auth/register-password', {
        data: { email, password: PASSWORD, accept_terms: true },
      });
      expect(created.status()).toBe(201);
      const issued = (await created.json()).api_key as string;

      await page.goto('/dashboard');

      // Regenerating elsewhere is invisible to this browser: the server can
      // no longer hand back the truth for comparison, so the prefix is the
      // only thing that can tell a live copy from a dead one. Without that
      // check this page shows a dead key as current, curl examples included.
      await page.evaluate(
        ([k, v]) => localStorage.setItem(k, v),
        [KEY_STORE, 'ZZZZZZZZ' + issued.slice(8)],
      );
      await page.reload();

      const shown = (await page.locator('#key-value').innerText()).trim();
      expect(shown).toBe(issued.slice(0, 8) + '…');
      expect(shown).not.toContain('ZZZZZZZZ');

      // And it is dropped rather than left to mislead the next load.
      expect(await storedKey(page)).toBeNull();
    });
});

test('the dashboard raises no page errors while doing all of this',
  async ({ page }) => {
    const failures: string[] = [];
    page.on('pageerror', (e) => failures.push(String(e)));
    page.on('console', (m) => {
      // 401s are expected: the page asks /api/auth/me before it knows whether
      // anybody is signed in. An uncaught exception is not.
      const text = m.text();
      if (m.type() === 'error' && !/401|403|Failed to load resource/.test(text)) {
        failures.push(text);
      }
    });

    const key = await registerInBrowser(page, freshEmail('errors'));
    expect(key).toBeTruthy();
    await page.reload();
    await page.evaluate(() => localStorage.clear());
    await page.reload();

    expect(failures).toEqual([]);
  });
