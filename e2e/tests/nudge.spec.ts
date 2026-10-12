import { test, expect, type Page } from '@playwright/test';

/**
 * The sign-up prompt on company pages and blog posts (nudge.js).
 *
 * Not on load, not on a timer; it appears once the reader reaches the end of
 * the company facts or the post. Close or Esc, or a sign-up, and it never
 * shows again. It must not land on what was just read, the end of the page must
 * stay reachable above it, and it must not exist at all on the pages it is not
 * meant for.
 */

const COMPANY = '/company/MSFT';
const POST = '/blog/sec-xbrl-data-wrong-one-in-five';

test.beforeEach(async ({ page }) => {
  // the site-wide sign-in sheet (nav.js) is a separate feature; keep it out of the way
  await page.addInitScript(() => {
    try { localStorage.setItem('bp.prompt_after', 'yes'); } catch { /* no storage */ }
  });
});

async function reachEnd(page: Page): Promise<void> {
  await page.locator('[data-nudge-end]').evaluate((e) => e.scrollIntoView({ block: 'start' }));
}

for (const path of [COMPANY, POST]) {
  test(`${path}: hidden on load, shown at the end of the content, nothing focused`, async ({ page }) => {
    await page.goto(path);
    const nudge = page.locator('#signup-nudge');
    await expect(nudge).toBeHidden();
    await page.waitForTimeout(2500);
    await expect(nudge, 'no timer').toBeHidden();
    await reachEnd(page);
    await expect(nudge).toBeVisible();
    expect(await nudge.evaluate((n) => n.contains(document.activeElement))).toBe(false);
    await expect(nudge.locator('.nudge-line')).toHaveText('1,000 calls a month. No card, no password.');
  });
}

test('a company page names the company', async ({ page }) => {
  await page.goto(COMPANY);
  await reachEnd(page);
  await expect(page.locator('#signup-nudge .nudge-h')).toHaveText('Get MSFT as JSON, free');
});

test('Close, then reload: it does not come back', async ({ page }) => {
  await page.goto(COMPANY);
  await reachEnd(page);
  await page.locator('#signup-nudge [data-nudge-close]').click();
  await expect(page.locator('#signup-nudge')).toBeHidden();
  await page.reload();
  await reachEnd(page);
  await page.waitForTimeout(800);
  await expect(page.locator('#signup-nudge')).toBeHidden();
});

test('Esc closes it, and it stays closed on another page', async ({ page }) => {
  await page.goto(POST);
  await reachEnd(page);
  await expect(page.locator('#signup-nudge')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('#signup-nudge')).toBeHidden();
  await page.goto(COMPANY);
  await reachEnd(page);
  await page.waitForTimeout(800);
  await expect(page.locator('#signup-nudge')).toBeHidden();
});

for (const path of ['/', '/pricing', '/dashboard', '/login', '/dataset', '/api', '/about']) {
  test(`never on ${path}`, async ({ page }) => {
    await page.goto(path);
    await expect(page.locator('#signup-nudge')).toHaveCount(0);
    await expect(page.locator('[data-nudge-end]')).toHaveCount(0);
  });
}

for (const width of [390, 1440]) {
  test(`at ${width}px it does not cover what was just read, and the page end stays reachable`, async ({ page }) => {
    await page.setViewportSize({ width, height: width < 500 ? 844 : 900 });
    await page.goto(COMPANY);
    await reachEnd(page);
    const nudge = page.locator('#signup-nudge');
    await expect(nudge).toBeVisible();
    await page.waitForTimeout(400); // the slide-in
    const at = await page.evaluate(() => ({
      marker: document.querySelector('[data-nudge-end]')!.getBoundingClientRect().top,
      top: document.getElementById('signup-nudge')!.getBoundingClientRect().top,
      dimmed: [...document.body.querySelectorAll('*')].some((e) => {
        const c = getComputedStyle(e);
        return c.position === 'fixed' && e.id !== 'signup-nudge' && !e.closest('#signup-nudge')
          && e.getBoundingClientRect().width >= innerWidth && e.getBoundingClientRect().height >= innerHeight
          && c.display !== 'none' && c.visibility !== 'hidden' && parseFloat(c.opacity) > 0
          && c.pointerEvents !== 'none' && !e.classList.contains('backdrop');
      }),
    }));
    expect(at.marker, 'the end of the facts is above the card').toBeLessThan(at.top);
    expect(at.dimmed, 'no full-screen overlay').toBe(false);
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    await page.waitForTimeout(300);
    const end = await page.evaluate(() => ({
      footer: document.querySelector('footer')!.getBoundingClientRect().bottom,
      top: document.getElementById('signup-nudge')!.getBoundingClientRect().top,
    }));
    expect(end.footer, 'the last of the page scrolls clear of the card').toBeLessThanOrEqual(end.top + 1);
  });
}

test('signing up from it shows the key and Copy key, and it never shows again', async ({ page }) => {
  await page.goto(COMPANY);
  await reachEnd(page);
  const nudge = page.locator('#signup-nudge');
  await nudge.locator('#nudge-email').fill(`e2e-nudge-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.invalid`);
  await nudge.locator('button[type=submit]').click();
  const out = nudge.locator('[data-keyout]');
  await expect(out).toBeVisible();
  await expect(out.locator('[data-keyvalue]')).toHaveText(/^[A-Za-z0-9_-]{20,}$/);
  await expect(out.getByRole('button', { name: 'Copy key' })).toBeVisible();
  await expect(out.locator('[data-keycurl]')).toContainText('/api/company/MSFT');
  await page.reload();
  await reachEnd(page);
  await page.waitForTimeout(800);
  await expect(page.locator('#signup-nudge')).toBeHidden();
});

/* One offer per page: on company pages and posts the nav.js sign-in sheet
   stands down (it never even asks /api/signin-prompt), and the bottom prompt
   is the only offer. Elsewhere the sheet's logic still runs. These tests do
   not pre-answer the sheet, so it is free to try. */
test.describe('one offer per page', () => {
  for (const path of [COMPANY, POST]) {
    test(`${path}: the sign-in sheet stays down, the prompt appears at the bottom`, async ({ browser }) => {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const asked: string[] = [];
      page.on('request', (r) => { if (r.url().includes('/api/signin-prompt')) asked.push(r.url()); });
      await page.goto(path);
      await page.mouse.wheel(0, 600);
      await page.waitForTimeout(11000); // past the sheet's 10-second timer
      await expect(page.locator('#signin-prompt')).toBeHidden();
      expect(asked, 'nav.js did not ask for the sheet').toEqual([]);
      await page.locator('[data-nudge-end]').evaluate((e) => e.scrollIntoView({ block: 'start' }));
      await expect(page.locator('#signup-nudge')).toBeVisible();
      await expect(page.locator('#signin-prompt')).toBeHidden();
      await ctx.close();
    });
  }

  test('elsewhere the sheet is still alive: /about asks /api/signin-prompt', async ({ browser }) => {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const asked = page.waitForRequest((r) => r.url().includes('/api/signin-prompt'), { timeout: 15000 });
    await page.goto('/about');
    await page.mouse.wheel(0, 600);
    expect((await asked).method()).toBe('GET');
    await expect(page.locator('#signup-nudge')).toHaveCount(0);
    await ctx.close();
  });
});
