/* The sign-in offer, driven the way a reader drives it.
   Asserts the full cycle: it arrives, the X closes it, it stays closed across
   a reload, and it comes back once the browser forgets the dismissal. */
import { chromium } from 'playwright';

const BASE = process.argv[2] || 'http://127.0.0.1:8099';
const OUT = process.argv[3] || '.';
const SEL = '#signin-prompt';

const browser = await chromium.launch();
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();

const fail = [];
const ok = (cond, msg) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + msg); if (!cond) fail.push(msg); };

async function visible() {
  const el = await page.$(SEL);
  if (!el) return false;
  return await el.isVisible();
}

// --- 1. it is not in the way on arrival -----------------------------------
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(1500);
ok(!(await visible()), 'not shown immediately on load (the hero gets the fold)');

// --- 2. it arrives once the reader goes past the fold ---------------------
await page.evaluate(() => window.scrollTo(0, window.innerHeight * 0.8));
await page.waitForSelector(SEL + ':visible', { timeout: 15000 });
ok(await visible(), 'appears after scrolling past the fold');
// The panel fades in over .2s. Screenshotting the moment it becomes visible
// catches a half-opaque frame and reads as a transparency bug that is not
// there -- it cost one round of chasing already.
await page.waitForTimeout(600);
const bg = await page.evaluate(
  () => getComputedStyle(document.querySelector('.sp-sheet')).backgroundColor
);
ok(/^rgb\(/.test(bg), `the sheet is opaque once settled (${bg})`);
await page.screenshot({ path: `${OUT}/signin_prompt.png` });

// --- 3. the X closes it ---------------------------------------------------
await page.click('#sp-close');
await page.waitForTimeout(400);
ok(!(await visible()), 'the X in the corner closes it');

const after = await page.evaluate(() => window.localStorage.getItem('bp.prompt_after'));
const count = await (await page.request.get(BASE + '/api/signin-prompt')).json();
ok(after !== null, 'dismissal is remembered');
ok(Number(after) === count.count + count.interval,
   `next prompt is ${count.interval} fetches away (stored ${after}, count ${count.count})`);

// --- 4. it stays gone -----------------------------------------------------
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.evaluate(() => window.scrollTo(0, window.innerHeight * 0.8));
await page.waitForTimeout(3000);
ok(!(await visible()), 'stays closed on the next page load');

await page.goto(BASE + '/company/JPM', { waitUntil: 'domcontentloaded' });
await page.evaluate(() => window.scrollTo(0, window.innerHeight * 0.8));
await page.waitForTimeout(3000);
ok(!(await visible()), 'stays closed on a different page too');

// --- 5. Escape works as a dismissal as well -------------------------------
await page.evaluate(() => window.localStorage.removeItem('bp.prompt_after'));
await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
await page.evaluate(() => window.scrollTo(0, window.innerHeight * 0.8));
await page.waitForSelector(SEL + ':visible', { timeout: 15000 });
ok(await visible(), 'returns once the browser forgets the dismissal');
await page.keyboard.press('Escape');
await page.waitForTimeout(400);
ok(!(await visible()), 'Escape closes it too');

// --- 6. never on /login or /dashboard -------------------------------------
await page.evaluate(() => window.localStorage.removeItem('bp.prompt_after'));
for (const path of ['/login', '/dashboard']) {
  await page.goto(BASE + path, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  ok(!(await page.$(SEL)), `absent on ${path}`);
}

await browser.close();
console.log(fail.length ? `\n${fail.length} FAILED` : '\nall passed');
process.exit(fail.length ? 1 : 0);
