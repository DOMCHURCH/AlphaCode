import { test, expect } from '@playwright/test';

/**
 * The home page stats strip, at every width a reader is likely to have.
 *
 * Production grew a seventh cell ("Updated · Nd ago", shown once the
 * scheduler has a last success to report) and at 621-1440px the longest label
 * ran into its neighbour -- "REASON" + "UPDATED" read as "REASODATED". The
 * pytest suite and a 6-cell preview never saw it. This asserts, per width:
 *
 *   - no two cells' boxes overlap
 *   - nothing inside a cell spills out of it (cell scrollWidth <= clientWidth,
 *     and every label and value sits inside its cell's box)
 *   - figures that share a row still share one baseline
 *
 * The e2e seed (seed_smoke.py) writes a scheduler success so the seventh cell
 * renders here as it does in production. Read-only: it only loads `/`.
 */

const WIDTHS = [390, 480, 620, 700, 800, 1024, 1280, 1440, 1536, 1920];

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    try { localStorage.setItem('bp.prompt_after', 'yes'); } catch { /* no storage */ }
  });
});

for (const width of WIDTHS) {
  test(`the stats strip never overlaps or spills at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    await page.locator('.strip').scrollIntoViewIfNeeded();
    // measured after the web fonts land, or a fallback face decides the widths
    await page.evaluate(() => document.fonts.ready);

    const r = await page.evaluate(() => {
      const text = (e: Element) => { const g = document.createRange(); g.selectNodeContents(e); return g.getBoundingClientRect(); };
      const cells = [...document.querySelectorAll<HTMLElement>('.strip .scell')].map((c) => {
        const box = c.getBoundingClientRect();
        const sk = c.querySelector('.sk')!;
        const sv = c.querySelector('.sv')!;
        // a zero-size inline marker at the end of the value gives its baseline
        const probe = document.createElement('span');
        probe.style.cssText = 'display:inline-block;width:0;height:0;vertical-align:baseline';
        sv.appendChild(probe);
        const baseline = probe.getBoundingClientRect().bottom;
        probe.remove();
        return {
          label: (sk.textContent || '').trim(),
          box: { l: box.left, r: box.right, t: box.top, b: box.bottom },
          scroll: c.scrollWidth, client: c.clientWidth,
          sk: text(sk), sv: text(sv), baseline,
        };
      });
      return { cells, cellCount: cells.length };
    });

    expect(r.cellCount, 'the strip renders').toBeGreaterThanOrEqual(6);

    const problems: string[] = [];
    const cells = r.cells;
    for (let i = 0; i < cells.length; i++) {
      const a = cells[i];
      if (a.scroll > a.client) problems.push(`"${a.label}" content is ${a.scroll - a.client}px wider than its cell`);
      for (const [part, t] of [['label', a.sk], ['value', a.sv]] as const) {
        if (t.left < a.box.l - 1 || t.right > a.box.r + 1) {
          problems.push(`"${a.label}" ${part} spills ${Math.round(Math.max(a.box.l - t.left, t.right - a.box.r))}px out of its cell`);
        }
      }
      for (let j = i + 1; j < cells.length; j++) {
        const b = cells[j];
        const w = Math.min(a.box.r, b.box.r) - Math.max(a.box.l, b.box.l);
        const h = Math.min(a.box.b, b.box.b) - Math.max(a.box.t, b.box.t);
        if (w > 0.5 && h > 0.5) problems.push(`"${a.label}" and "${b.label}" overlap by ${Math.round(w)}x${Math.round(h)}px`);
      }
    }
    // Figures on one row share a baseline (cells whose tops match are a row).
    const rows = new Map<number, number[]>();
    for (const c of cells) {
      const key = Math.round(c.box.t);
      rows.set(key, [...(rows.get(key) || []), c.baseline]);
    }
    for (const [, lines] of rows) {
      if (lines.length > 1 && Math.max(...lines) - Math.min(...lines) > 1.5) {
        problems.push(`values in one row sit on different baselines (${lines.map((x) => x.toFixed(1)).join(', ')})`);
      }
    }
    expect(problems, `at ${width}px with ${r.cellCount} cells`).toEqual([]);
  });
}
