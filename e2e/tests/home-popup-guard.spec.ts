import { test, expect, type Page } from '@playwright/test';

/**
 * The first-visit sign-in sheet (nav.js) is a modal. On the home page the demo
 * command (#example, the request and response card) is the argument of the
 * page, so the sheet must not open over it: not on the 10 second timer while
 * somebody is reading it, and not at the half-screen scroll while it is still
 * in view. It waits until the card has scrolled out of view.
 *
 * The state /api/signin-prompt returns is faked, because the test environment
 * has the sign-in feature off. Everything else is the real page.
 */

async function offer(page: Page): Promise<void> {
  await page.route('**/api/signin-prompt', (route) => route.fulfill({
    contentType: 'application/json',
    body: JSON.stringify({
      signed_in: false, login_enabled: true, answered: null, after: null, count: 0, offer: null,
    }),
  }));
}

async function cardInView(page: Page): Promise<boolean> {
  return page.locator('#example').evaluate((el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < window.innerHeight;
  });
}

for (const [name, size] of [['desktop', { width: 1440, height: 900 }], ['phone', { width: 390, height: 844 }]] as const) {
  test(`${name}: the sheet does not open over the demo command, even after the 10 s timer`, async ({ page }) => {
    await page.setViewportSize(size);
    await offer(page);
    await page.goto('/');
    expect(await cardInView(page)).toBe(true);
    await page.waitForTimeout(11_000);
    await expect(page.locator('#signin-prompt')).toBeHidden();
    expect(await cardInView(page)).toBe(true);
  });

  test(`${name}: scrolling a little, with the card still in view, does not open it either`, async ({ page }) => {
    await page.setViewportSize(size);
    await offer(page);
    await page.goto('/');
    // past half a screen is the old trigger; scroll just past that, card still partly in view
    await page.evaluate(() => window.scrollTo(0, Math.floor(window.innerHeight * 0.55)));
    await page.waitForTimeout(500);
    if (await cardInView(page)) {
      await expect(page.locator('#signin-prompt')).toBeHidden();
    }
  });

  test(`${name}: once the card has scrolled away the sheet opens, and the card is not under it`, async ({ page }) => {
    await page.setViewportSize(size);
    await offer(page);
    await page.goto('/');
    await page.locator('#pricing').evaluate((el) => el.scrollIntoView({ block: 'start' }));
    await expect(page.locator('#signin-prompt')).toBeVisible({ timeout: 5_000 });
    expect(await cardInView(page)).toBe(false);
  });
}

test('on another page the sheet still opens on the old rule (no demo card there)', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await offer(page);
  await page.goto('/about');
  await page.evaluate(() => window.scrollTo(0, Math.floor(window.innerHeight * 0.7)));
  // /about may be too short to scroll; the 10 s timer is the fallback
  await expect(page.locator('#signin-prompt')).toBeVisible({ timeout: 12_000 });
});
