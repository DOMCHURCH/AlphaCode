import { chromium } from 'playwright';
const BASE = 'http://127.0.0.1:8193';
const OUT = 'C:/Users/DOMINI~1/AppData/Local/Temp/claude/C--Users-Dominique/9e9ab2fb-89f0-45d8-bd87-1d819e0d4a5f/scratchpad';
for (const who of ['starter', 'pro']) {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1100, height: 1800 } })).newPage();
  page.on('pageerror', e => console.log('PAGEERROR', e.message));
  await page.request.post(BASE + '/api/auth/login', { data: { email: who + '@plans.test', password: 'correct horse battery' } });
  await page.goto(BASE + '/dashboard');
  await page.addStyleTag({ content: '#signin-prompt{display:none!important}' });
  await page.waitForSelector('#tabs:not([hidden])', { timeout: 15000 });
  await page.click('button.tab[data-tab="data"]');
  await page.fill('#hist-q', 'AAA');
  await page.click('#hist-btn');
  await page.waitForTimeout(2500);
  console.log(who, 'hist:', await page.textContent('#hist-note'));
  console.log(who, 'st:', await page.textContent('#st-note'));
  await page.click('[data-st-period="quarterly"]');
  await page.waitForTimeout(2000);
  console.log(who, 'st quarterly:', await page.textContent('#st-note'));
  console.log(who, 'asof visible:', await page.isVisible('#hist-asof'), 'ex visible:', await page.isVisible('#ex-form'));
  if (who === 'pro') {
    await page.click('#ex-btn');
    await page.waitForTimeout(4000);
    console.log(who, 'ex:', await page.textContent('#ex-note'));
    const past = new Date(Date.now() - 200 * 864e5).toISOString().slice(0, 10);
    await page.fill('#hist-asof', past);
    await page.click('#hist-btn');
    await page.waitForTimeout(2500);
    console.log(who, 'hist asof:', await page.textContent('#hist-note'));
    await page.click('[data-st-period="quarterly"]');
    await page.waitForTimeout(1500);
  }
  await page.locator('#panel-data').screenshot({ path: OUT + '/dash-' + who + '.png' });
  await browser.close();
}
