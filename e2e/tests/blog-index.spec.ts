import { test, expect } from '@playwright/test';

/**
 * /blog is sectioned. The server renders every post under its section, so the
 * page is a complete list with the script blocked; the filter and sort buttons
 * are the enhancement, and none of them reloads the page.
 */

const SECTIONS = ['method', 'data-quality', 'comparisons', 'guides'];

test.beforeEach(async ({ page }) => {
  // the first-visit sign-in sheet (nav.js) is a separate feature; keep it out of the way
  await page.addInitScript(() => {
    try { localStorage.setItem('bp.prompt_after', 'yes'); } catch { /* no storage */ }
  });
});

test('with JavaScript off every post is listed and the sections can be jumped to', async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto('/blog');
  await expect(page.locator('.blog-sec')).toHaveCount(SECTIONS.length);
  const posts = await page.locator('.post-item').count();
  expect(posts).toBeGreaterThanOrEqual(14);
  await expect(page.locator('#blog-tools')).toBeHidden();
  await expect(page.locator('#blog-jump')).toBeVisible();
  for (const id of SECTIONS) await expect(page.locator(`.blog-sec#${id}`)).toBeVisible();
  await context.close();
});

test('the filter shows one section without a page load, and All brings the rest back', async ({ page }) => {
  await page.goto('/blog');
  await page.evaluate(() => { (window as any).__marker = 1; });
  await expect(page.locator('#blog-tools')).toBeVisible();
  await expect(page.locator('#blog-jump')).toBeHidden();
  const all = await page.locator('.post-item:visible').count();

  await page.locator('[data-filter="data-quality"]').click();
  await expect(page.locator('.blog-sec#data-quality')).toBeVisible();
  await expect(page.locator('.blog-sec#guides')).toBeHidden();
  const some = await page.locator('.post-item:visible').count();
  expect(some).toBeGreaterThan(0);
  expect(some).toBeLessThan(all);
  await expect(page.locator('[data-filter="data-quality"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#blog-status')).toContainText(`${some} notes`);
  expect(await page.evaluate(() => (window as any).__marker)).toBe(1); // same document

  await page.locator('[data-filter="all"]').click();
  expect(await page.locator('.post-item:visible').count()).toBe(all);
});

test('a link to /blog#guides opens with that section chosen', async ({ page }) => {
  await page.goto('/blog#guides');
  await expect(page.locator('.blog-sec#guides')).toBeVisible();
  await expect(page.locator('.blog-sec#method')).toBeHidden();
});

test('the sort buttons reorder the list', async ({ page }) => {
  await page.goto('/blog');
  const dates = async () => page.locator('.blog-sec#data-quality .post-item').evaluateAll(
    (els) => els.map((e) => e.getAttribute('data-date') as string),
  );
  const mins = async () => page.locator('.blog-sec#data-quality .post-item').evaluateAll(
    (els) => els.map((e) => Number(e.getAttribute('data-min'))),
  );
  const newest = await dates();
  expect([...newest].sort().reverse()).toEqual(newest);

  await page.locator('[data-sort="old"]').click();
  const oldest = await dates();
  expect([...oldest].sort()).toEqual(oldest);

  await page.locator('[data-sort="short"]').click();
  const short = await mins();
  expect([...short].sort((a, b) => a - b)).toEqual(short);
  await expect(page.locator('[data-sort="short"]')).toHaveAttribute('aria-pressed', 'true');
});

for (const width of [390, 360]) {
  test(`no horizontal scroll at ${width}px, tools wrap`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto('/blog');
    const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(over).toBeLessThanOrEqual(0);
    // the buttons wrap onto more than one row rather than scrolling sideways
    const tops = await page.locator('#blog-tools .blog-chips').first().locator('.tab')
      .evaluateAll((els) => new Set(els.map((e) => Math.round(e.getBoundingClientRect().top))).size);
    expect(tops).toBeGreaterThan(1);
    await page.locator('[data-filter="guides"]').click();
    const over2 = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(over2).toBeLessThanOrEqual(0);
  });
}
