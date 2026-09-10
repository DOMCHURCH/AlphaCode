"""Rebuild the hero backdrop variants from the tracked source image.

    python scripts/build_backdrop.py

Reads `src/report/static/background-source.png` and writes the three files the
page actually loads:

    media/backdrop-2400.webp   desktop, quality 82
    media/backdrop-1200.webp   phone, quality 80
    media/backdrop-lqip.webp   24px, kept only so the placeholder is auditable

and prints the 24px placeholder as a base64 data URI for `backdrop.css`.

Why a script rather than three one-off commands: the numbers below (widths,
qualities, the crop the CSS assumes) are part of the design, and a design that
lives in somebody's shell history gets re-derived wrongly the next time the
picture changes.

NOTE ON UPSCALING. The source is 1928px wide and the desktop variant is 2400px,
so that variant is a ~1.25x enlargement -- it carries no detail the source did
not have. It exists because a 2x phone and a wide desktop both ask for more than
1200px and the alternative is the browser scaling the 1200 up, which looks worse
than Lanczos doing it once, offline.
"""

from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image, ImageFilter

STATIC = Path(__file__).resolve().parent.parent / "src" / "report" / "static"
SOURCE = STATIC / "background-source.png"
MEDIA = STATIC / "media"

# (filename, width, quality). Heights follow the source aspect ratio, so the
# CSS `object-fit: cover` never has two different shapes to reconcile.
VARIANTS = (
    ("backdrop-2400.webp", 2400, 82),
    ("backdrop-1200.webp", 1200, 80),
)

# The placeholder is deliberately tiny: 24px wide, blurred, so it is a wash of
# the right colours rather than a recognisable thumbnail. Anything larger stops
# being free and starts competing with the real image for the first paint.
LQIP_WIDTH = 24


def _resize(src: Image.Image, width: int) -> Image.Image:
    height = round(width * src.height / src.width)
    return src.resize((width, height), Image.LANCZOS)


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"missing source image: {SOURCE}")

    MEDIA.mkdir(parents=True, exist_ok=True)
    src = Image.open(SOURCE).convert("RGB")
    print(f"source {SOURCE.name}  {src.width}x{src.height}")

    for name, width, quality in VARIANTS:
        out = MEDIA / name
        _resize(src, width).save(out, "WEBP", quality=quality, method=6)
        kb = out.stat().st_size / 1024
        note = "  (upscaled from source)" if width > src.width else ""
        print(f"  {name:<22} {width}px  q{quality}  {kb:6.1f} KB{note}")

    lqip = _resize(src, LQIP_WIDTH).filter(ImageFilter.GaussianBlur(1.2))
    lqip_path = MEDIA / "backdrop-lqip.webp"
    lqip.save(lqip_path, "WEBP", quality=60, method=6)
    encoded = base64.b64encode(lqip_path.read_bytes()).decode("ascii")
    print(f"  {'backdrop-lqip.webp':<22} {LQIP_WIDTH}px  {len(encoded)} base64 chars")
    print("\nPaste into backdrop.css as the .backdrop-still background-image:\n")
    print(f'url("data:image/webp;base64,{encoded}")')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
