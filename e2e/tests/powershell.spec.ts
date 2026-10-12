import { test, expect, type Page, type BrowserContext } from '@playwright/test';

/**
 * Windows PowerShell aliases `curl` to Invoke-WebRequest, so the site's
 * `curl -H "X-API-Key: ..."` failed there as typed. The home code card has a
 * PowerShell tab (curl.exe and Invoke-RestMethod), opens it by default on
 * Windows, and the post-signup "Try it" command uses curl.exe on Windows.
 *
 * The platform is faked in the page (navigator.platform / userAgentData), not
 * read from the machine running the test, so both cases run anywhere.
 */

async function as(context: BrowserContext, platform: 'Windows' | 'macOS'): Promise<void> {
  await context.addInitScript((p) => {
    const win = p === 'Windows';
    Object.defineProperty(navigator, 'platform', { get: () => (win ? 'Win32' : 'MacIntel') });
    Object.defineProperty(navigator, 'userAgentData', { get: () => ({ platform: p, brands: [], mobile: false }) });
    Object.defineProperty(navigator, 'userAgent', {
      get: () => (win
        ? 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36'
        : 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36'),
    });
    try { localStorage.setItem('bp.prompt_after', 'yes'); } catch { /* no storage */ }
  }, platform);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
}

/** What was copied. Line endings are normalised because the Windows clipboard
 *  hands LF back as CRLF, which is the operating system and not the page. */
async function clipboard(page: Page): Promise<string> {
  const text = await page.evaluate(() => navigator.clipboard.readText());
  return text.split('\r\n').join('\n');
}

test('the PowerShell tab exists, shows curl.exe and Invoke-RestMethod, and Copy copies it', async ({ page, context }) => {
  await as(context, 'macOS');
  await page.goto('/');
  await page.locator('label[for="cc-t-ps"]').click();
  const panel = page.locator('.cc-p-ps');
  await expect(panel).toBeVisible();
  const code = page.locator('#cc-ps-req');
  await expect(code).toContainText('curl.exe -H "X-API-Key: YOUR_KEY"');
  await expect(code).toContainText('curl.exe https://');
  await expect(code).toContainText('Invoke-RestMethod -Uri');
  await expect(code).toContainText('-Headers @{"X-API-Key"="YOUR_KEY"}');
  await expect(code).not.toContainText(/(^|\n)curl /);

  await panel.locator('.cc-copy').click();
  await expect(panel.locator('.cc-copy')).toHaveText('Copied');
  // Copy copies the element's text exactly (textContent), line breaks included
  expect(await clipboard(page)).toBe(await code.textContent());
});

test('Windows opens the PowerShell tab by default', async ({ page, context }) => {
  await as(context, 'Windows');
  await page.goto('/');
  await expect(page.locator('#cc-t-ps')).toBeChecked();
  await expect(page.locator('.cc-p-ps')).toBeVisible();
  await expect(page.locator('.cc-p-curl')).toBeHidden();
});

test('elsewhere curl stays the default', async ({ page, context }) => {
  await as(context, 'macOS');
  await page.goto('/');
  await expect(page.locator('#cc-t-curl')).toBeChecked();
  await expect(page.locator('.cc-p-curl')).toBeVisible();
  await expect(page.locator('.cc-p-ps')).toBeHidden();
});

for (const platform of ['Windows', 'macOS'] as const) {
  test(`after signup, "Try it" uses ${platform === 'Windows' ? 'curl.exe' : 'curl'} and Copy command copies what is shown (${platform})`, async ({ page, context }) => {
    await as(context, platform);
    await page.goto('/');
    await page.fill('#hero-email', `e2e-ps-${platform}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.invalid`);
    await page.locator('form[data-keyform]').first().locator('button[type=submit]').click();
    const out = page.locator('[data-keyout]:not([hidden])').first();
    await expect(out).toBeVisible();
    const cmd = out.locator('[data-keycurl]');
    const want = platform === 'Windows' ? /^curl\.exe -H "X-API-Key: [A-Za-z0-9_-]{20,}" http/ : /^curl -H "X-API-Key: [A-Za-z0-9_-]{20,}" http/;
    await expect(cmd).toHaveText(want);
    await out.getByRole('button', { name: 'Copy command' }).click();
    expect(await clipboard(page)).toBe(await cmd.textContent());
  });
}
