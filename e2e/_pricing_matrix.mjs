import { chromium } from 'playwright';
const b = await chromium.launch();
for (const [w, name] of [[1200, 'desk'], [390, 'phone']]) {
  const p = await b.newPage({ viewport: { width: w, height: 900 } });
  await p.goto('http://127.0.0.1:8194/pricing');
  await p.addStyleTag({ content: '#signin-prompt{display:none!important}' });
  const t = p.locator('table.plan-matrix').first();
  await t.scrollIntoViewIfNeeded();
  console.log(name, 'page scrollWidth', await p.evaluate(() => document.documentElement.scrollWidth), 'viewport', w);
  await t.screenshot({ path: 'C:/Users/DOMINI~1/AppData/Local/Temp/claude/C--Users-Dominique/9e9ab2fb-89f0-45d8-bd87-1d819e0d4a5f/scratchpad/matrix-' + name + '.png' });
  await p.close();
}
await b.close();
