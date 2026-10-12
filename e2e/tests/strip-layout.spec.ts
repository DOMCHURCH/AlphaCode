import { test, expect, type Page } from '@playwright/test';

/**
 * The home page stats strip, at every width a reader is likely to have.
 *
 * Production once grew a seventh cell ("Updated · Nd ago") and at 621-1440px
 * the longest label ran into its neighbour ("REASODATED"). That cell has since
 * been removed, so the strip renders six cells; the seven-cell case is kept by
 * adding the cell back in the browser, so the layout stays safe if it returns.
 * For each case and width this asserts:
 *
 *   - no two cells' boxes overlap
 *   - nothing inside a cell spills out of it (cell scrollWidth <= clientWidth,
 *     and every label and value sits inside its cell's box)
 *   - figures that share a row still share one baseline
 *   - with six cells, no row is left holding a single orphaned cell
 *
 * Read-only: it only loads `/`.
 */

const WIDTHS = [390, 480, 620, 700, 800, 1024, 1280, 1440, 1536, 1920];

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    try { localStorage.setItem('bp.prompt_after', 'yes'); } catch { /* no storage */ }
  });
});

/** The cell as the server used to render it. */
async function addUpdatedCell(page: Page): Promise<void> {
  await page.evaluate(() => {
    const strip = document.querySelector('.strip')!;
    const cell = document.createElement('div');
    cell.className = 'scell txt';
    cell.innerHTML = '<span class="sk">Updated</span><span class="sv"><i class="pulse" aria-hidden="true"></i>2d ago</span>';
    strip.appendChild(cell);
  });
}

for (const cells of [6, 7] as const) {
  for (const width of WIDTHS) {
    test(`${cells} cells: the stats strip never overlaps or spills at ${width}px`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      await page.locator('.strip').scrollIntoViewIfNeeded();
      if (cells === 7) await addUpdatedCell(page);
      // measured after the web fonts land, or a fallback face decides the widths
      await page.evaluate(() => document.fonts.ready);

      const r = await page.evaluate(() => {
        const text = (e: Element) => { const g = document.createRange(); g.selectNodeContents(e); return g.getBoundingClientRect(); };
        return [...document.querySelectorAll<HTMLElement>('.strip .scell')].map((c) => {
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
      });

      expect(r.length, 'cells in the strip').toBe(cells);

      const problems: string[] = [];
      for (let i = 0; i < r.length; i++) {
        const a = r[i];
        if (a.scroll > a.client) problems.push(`"${a.label}" content is ${a.scroll - a.client}px wider than its cell`);
        for (const [part, t] of [['label', a.sk], ['value', a.sv]] as const) {
          if (t.left < a.box.l - 1 || t.right > a.box.r + 1) {
            problems.push(`"${a.label}" ${part} spills ${Math.round(Math.max(a.box.l - t.left, t.right - a.box.r))}px out of its cell`);
          }
        }
        for (let j = i + 1; j < r.length; j++) {
          const b = r[j];
          const w = Math.min(a.box.r, b.box.r) - Math.max(a.box.l, b.box.l);
          const h = Math.min(a.box.b, b.box.b) - Math.max(a.box.t, b.box.t);
          if (w > 0.5 && h > 0.5) problems.push(`"${a.label}" and "${b.label}" overlap by ${Math.round(w)}x${Math.round(h)}px`);
        }
      }
      // Rows are the cells whose tops match.
      const rows = new Map<number, typeof r>();
      for (const c of r) {
        const key = Math.round(c.box.t);
        rows.set(key, [...(rows.get(key) || []), c]);
      }
      for (const [, row] of rows) {
        const lines = row.map((c) => c.baseline);
        if (row.length > 1 && Math.max(...lines) - Math.min(...lines) > 1.5) {
          problems.push(`values in one row sit on different baselines (${lines.map((x) => x.toFixed(1)).join(', ')})`);
        }
      }
      if (cells === 6 && rows.size > 1) {
        const lone = [...rows.values()].filter((row) => row.length === 1);
        if (lone.length) problems.push(`orphaned cell(s) alone on a row: ${lone.map((row) => row[0].label).join(', ')}`);
      }
      expect(problems, `${cells} cells at ${width}px, ${rows.size} row(s)`).toEqual([]);
    });
  }
}
