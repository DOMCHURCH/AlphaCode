import { test, expect, type Page } from '@playwright/test';

/**
 * Site smoke: the clicks the server-side tests never make.
 *
 * The homepage restructure passed every pytest check and still lost the plan
 * buttons, because pytest reads HTML and never runs home.js. This suite clicks
 * what a reader clicks, on the home page, /pricing and a company page:
 *
 *   - every plan button posts to /api/billing/checkout, and the signed-out 401
 *     sends the reader to /login?next=... (there is no Stripe secret here, so
 *     nothing reaches Stripe; the 401 comes before it would be used)
 *   - the Monthly / Yearly toggle swaps the price and the plan the button buys
 *   - the search form lands on the company page
 *   - the code card's curl / Python / MCP tabs each show a non-empty panel
 *   - the free-key form's success and already-registered states
 *   - the phone menu opens
 *   - every table is visible, non-empty and styled (labels left, figures right)
 *   - every internal link answers below 400, and no page logs a console error
 *
 * Companies come from e2e/seed_smoke.py (run by global-setup.ts).
 */

const PAGES = ['/', '/pricing', '/company/MSFT'];

// The sign-in sheet (nav.js) opens over the page after a delay on a first
// visit. It is its own feature with its own tests; here it would only cover the
// buttons under test, so it is told it has already been answered -- the same
// flag home.js sets once a reader has taken a key.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    try { localStorage.setItem('bp.prompt_after', 'yes'); } catch { /* no storage */ }
  });
});

function freshEmail(label: string): string {
  return `e2e-smoke-${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.invalid`;
}

/** Console errors and uncaught exceptions while `fn` runs on `page`. */
async function collectErrors(page: Page, fn: () => Promise<void>): Promise<string[]> {
  const errors: string[] = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));
  await fn();
  return errors;
}

for (const path of ['/', '/pricing']) {
  test.describe(`plan buttons on ${path}`, () => {
    test('every plan button posts to checkout and a signed-out reader is sent to sign in', async ({ page }) => {
      await page.goto(path);
      const plans = await page.locator('.plan-cta[data-plan]').evaluateAll(
        (els) => els.map((e) => e.getAttribute('data-plan')),
      );
      expect(plans).toEqual(expect.arrayContaining(['starter', 'pro', 'business', 'dataset']));

      for (const plan of plans) {
        await page.goto(path);
        const cta = page.locator(`.plan-cta[data-plan="${plan}"]`).first();
        await cta.scrollIntoViewIfNeeded();
        const [request] = await Promise.all([
          page.waitForRequest((r) => r.url().endsWith('/api/billing/checkout') && r.method() === 'POST'),
          cta.click(),
        ]);
        expect(JSON.parse(request.postData() || '{}').plan).toBe(plan);
        await page.waitForURL(/\/login\?next=/);
        expect(page.url()).toContain('/login?next=');
      }
    });

    test('Monthly / Yearly swaps the Pro price and the plan its button buys', async ({ page }) => {
      await page.goto(path);
      const price = page.locator('.pro-plan [data-price]').first();
      const cta = page.locator('.pro-plan .plan-cta').first();
      const monthly = (await price.innerText()).trim();

      await page.locator('.bill-lead .bill-opt[data-period="year"]').click();
      await expect(cta).toHaveAttribute('data-plan', 'pro_annual');
      await expect(price).not.toHaveText(monthly);

      await page.locator('.bill-lead .bill-opt[data-period="month"]').click();
      await expect(cta).toHaveAttribute('data-plan', 'pro');
      await expect(price).toHaveText(monthly);
    });

    test('the Enterprise and free-key cards have somewhere to go', async ({ page }) => {
      await page.goto(path);
      await expect(page.locator('.plan-cta[href^="mailto:"]').first()).toBeVisible();
      await expect(page.locator('.plan-cta[href="/dashboard"]').first()).toBeVisible();
    });
  });
}

test.describe('home page', () => {
  test('the search box lands on the company page', async ({ page }) => {
    await page.goto('/');
    await page.fill('#q', 'MSFT');
    await page.press('#q', 'Enter');
    await page.waitForURL(/\/company\/MSFT/);
    await expect(page.locator('h1')).toContainText(/MICROSOFT/i);
  });

  test('each code-card tab shows its own non-empty panel', async ({ page }) => {
    await page.goto('/');
    for (const tab of ['py', 'mcp', 'curl']) {
      await page.locator(`label[for="cc-t-${tab}"]`).click();
      const panel = page.locator(`.cc-p-${tab}`);
      await expect(panel).toBeVisible();
      expect((await panel.innerText()).trim().length).toBeGreaterThan(20);
      for (const other of ['curl', 'py', 'mcp'].filter((t) => t !== tab)) {
        await expect(page.locator(`.cc-p-${other}`)).toBeHidden();
      }
    }
  });

  test('the free-key form shows the key, and says so when the address already has one', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const email = freshEmail('key');
    await page.goto('/');
    await page.fill('#hero-email', email);
    await page.locator('form[data-keyform]').first().locator('button[type=submit]').click();
    const out = page.locator('[data-keyout]:not([hidden])').first();
    await expect(out).toBeVisible();
    await expect(out.locator('[data-keyvalue]')).toHaveText(/^[A-Za-z0-9_-]{20,}$/);
    await out.locator('.keycopy').first().click();
    await expect(out.locator('.keycopy').first()).toHaveText(/Copied|Copy failed/);

    await page.goto('/');
    await page.fill('#hero-email', email);
    await page.locator('form[data-keyform]').first().locator('button[type=submit]').click();
    await expect(page.locator('[data-keynote]').first()).toContainText('already has a key');
  });

  test('the phone menu opens', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/');
    await expect(page.locator('.navcta-bar')).toBeVisible();
    await page.locator('#nav-toggle').click();
    await expect(page.locator('#nav-links')).toBeVisible();
    await expect(page.locator('#nav-links a', { hasText: 'Pricing' })).toBeVisible();
  });
});

test.describe('tables', () => {
  const TABLES: Array<[string, string[]]> = [
    ['/company/MSFT', ['table.facts-ratios', 'table.facts-history']],
    ['/pricing', ['table.plan-matrix', 'table.compare-3']],
    ['/methodology', ['table.mtable']],
  ];
  for (const [path, selectors] of TABLES) {
    test(`every table on ${path} is visible, non-empty and styled`, async ({ page }) => {
      await page.goto(path);
      for (const sel of selectors) {
        const table = page.locator(sel).first();
        await expect(table).toBeVisible();
        const box = await table.boundingBox();
        expect(box && box.width > 0 && box.height > 0).toBeTruthy();
        const facts = await table.evaluate((t: HTMLTableElement) => {
          const th = t.querySelector('th') as HTMLElement;
          const cell = t.querySelector('td') as HTMLElement;
          return {
            rows: t.rows.length,
            thAlign: getComputedStyle(th).textAlign,
            pad: parseFloat(getComputedStyle(cell).paddingLeft),
            sep: parseFloat(getComputedStyle(cell).borderBottomWidth)
              + parseFloat(getComputedStyle(t.rows[0].cells[0]).borderBottomWidth),
          };
        });
        expect(facts.rows, sel).toBeGreaterThanOrEqual(2);
        expect(['left', 'start'], sel).toContain(facts.thAlign);
        expect(facts.pad, `${sel} has real cell padding, not the browser's 1px`).toBeGreaterThan(4);
        expect(facts.sep, `${sel} separates its rows`).toBeGreaterThan(0);
      }
    });
  }

  test('the company tables put figures on the right and scroll on a phone', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto('/company/MSFT');
    const align = await page.locator('table.facts-history tbody tr').first().evaluate((tr: HTMLTableRowElement) =>
      [...tr.cells].map((c) => getComputedStyle(c).textAlign));
    expect(align[0]).toMatch(/left|start/);
    expect(align[2]).toBe('right');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
    expect(overflow, 'the page itself does not scroll sideways').toBeLessThanOrEqual(0);
  });
});

test.describe('no errors and no dead links', () => {
  for (const path of PAGES) {
    test(`${path} logs no console error`, async ({ page }) => {
      const errors = await collectErrors(page, async () => {
        await page.goto(path);
        await page.waitForLoadState('networkidle');
      });
      expect(errors).toEqual([]);
    });
  }

  test('every internal link on /, /pricing and a company page answers below 400', async ({ page, request }) => {
    const hrefs = new Set<string>();
    for (const path of PAGES) {
      await page.goto(path);
      for (const href of await page.locator('a[href^="/"]').evaluateAll((as) => as.map((a) => a.getAttribute('href') || ''))) {
        // JSON endpoints are pytest's; the keyless demo is off in this environment.
        if (!href.startsWith('//') && !href.startsWith('/api/')) hrefs.add(href.split('#')[0] || '/');
      }
    }
    const bad: string[] = [];
    for (const href of hrefs) {
      const r = await request.get(href, { maxRedirects: 5 });
      if (r.status() >= 400) bad.push(`${r.status()} ${href}`);
    }
    expect(bad).toEqual([]);
  });
});
