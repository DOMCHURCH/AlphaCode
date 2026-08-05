"""Capture the README's three company screenshots from a running instance.

    python3 scripts/screenshots.py                       # against localhost:8000
    python3 scripts/screenshots.py https://your-app.up.railway.app

Writes docs/screenshots/{jpm,msft,wmt}.png plus a side-by-side strip.

This exists as a script rather than committed images because the images have to
come from REAL loaded data. A screenshot taken against a seeded development
database would show shapes that no company actually filed, in a README whose
entire argument is that every figure traces to a filing -- which is the exact
failure this project is about.

Needs Playwright:  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

TICKERS = ("JPM", "MSFT", "WMT")
OUT = pathlib.Path(__file__).resolve().parent.parent / "docs" / "screenshots"
WIDTH = 430          # a phone, which is how the site is actually read
STRIP_HEIGHT = 1500  # tall enough for the header, the drawing and the legend


async def main(base: str) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Playwright is not installed.\n"
              "  pip install playwright && playwright install chromium")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    base = base.rstrip("/")
    # Honour a preinstalled browser (CI images often set this) rather than
    # insisting on Playwright's own download.
    launch: dict[str, object] = {}
    if os.environ.get("CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["CHROMIUM_PATH"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch)
        page = await browser.new_page(
            viewport={"width": WIDTH, "height": STRIP_HEIGHT},
            device_scale_factor=2,
        )
        for ticker in TICKERS:
            url = f"{base}/company/{ticker}"
            resp = await page.goto(url, wait_until="networkidle")
            if resp is None or resp.status != 200:
                print(f"FAILED {url} -> HTTP {resp.status if resp else '?'}")
                await browser.close()
                return 1
            # Confirm the page actually drew something before saving it: a
            # "nothing to draw" page is a 200 too.
            if not await page.query_selector(".bs-cols"):
                print(f"FAILED {url} -> no drawing on the page (no data loaded?)")
                await browser.close()
                return 1
            path = OUT / f"{ticker.lower()}.png"
            await page.screenshot(path=str(path))
            print(f"wrote {path.relative_to(OUT.parent.parent)}")
        await browser.close()

    _strip()
    return 0


def _strip() -> None:
    """Glue the three together, since one image makes the point on its own."""
    try:
        from PIL import Image
    except ImportError:
        print("Pillow not installed; skipping the side-by-side strip.")
        return
    imgs = [Image.open(OUT / f"{t.lower()}.png") for t in TICKERS]
    h = min(i.height for i in imgs)
    gap = 24
    total = sum(i.width for i in imgs) + gap * (len(imgs) - 1)
    canvas = Image.new("RGB", (total, h), (231, 235, 242))
    x = 0
    for i in imgs:
        canvas.paste(i.crop((0, 0, i.width, h)), (x, 0))
        x += i.width + gap
    canvas.save(OUT / "three-sectors.png")
    print(f"wrote docs/screenshots/three-sectors.png ({total}x{h})")


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    raise SystemExit(asyncio.run(main(base_url)))
