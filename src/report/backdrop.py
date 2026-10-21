"""The backdrop: one fixed picture behind every page, and nothing else.

A single image of vertical glowing panels with curved light, near-black on the
left and colour on the right. It is ONE layer, `position: fixed`, behind every
section of every page, rather than a hero image plus veils, glows and grids per
section. The picture does the work the veils used to do: its left ~40% is
already near-black, and the CSS fades its left edge into #05060A.

THE FILES. `backdrop-glass-2000.webp` is the preferred source and
`backdrop-glass-2000.jpg` the fallback, both 2000px wide and each well under
200 KB, in static/media/. They are named in an inline `<style>` and not in
`glass.css` for one reason: `/static` is served `max-age=31536000, immutable`,
so an unversioned URL would never reach anybody who had already visited, and
the version stamp (`media_version`) is only known here.

WHY `image-set()` HERE AND NOT `srcset`. The first attempt at a backdrop on this
site put `w` descriptors in `image-set()`, which takes `1x`/`2x` and not `w`; the
whole declaration was dropped and the site shipped plain black for three
deploys. This one uses `type()`, which is what `image-set()` is for, and it
follows a plain `url()` declaration of the JPG, so a browser that does not
understand `type()` ignores the second line and keeps the first. Nothing here
can fail to black.

The old ribbon pictures (`backdrop-1200/2400/portrait`) and their veil layer are
gone. `variant` is still accepted so callers need not change, and is ignored.
"""

from __future__ import annotations

from pathlib import Path

# One place for the asset paths, so the CSS, the markup and the social cards
# cannot disagree about what is being loaded.
_MEDIA = Path(__file__).parent / "static" / "media"

BACKDROP_WEBP = "/static/media/backdrop-glass-2000.webp"
BACKDROP_JPG = "/static/media/backdrop-glass-2000.jpg"


def media_version() -> str:
    """Cache-busting stamp for everything in static/media/.

    `asset_version()` walks the top level of `static/` and these live one
    directory down, so it does not see them change. That matters more here than
    anywhere else on the site: `/static` is served `max-age=31536000,
    immutable`, so a regenerated image behind an unversioned URL reaches nobody
    who has already visited -- which is precisely the set of people most likely
    to notice the backdrop was broken.
    """
    try:
        # rglob, not iterdir: the home hero's layers live in media/hero/.
        return str(int(max(f.stat().st_mtime for f in _MEDIA.rglob("*") if f.is_file())))
    except (OSError, ValueError):
        return "0"


def render_backdrop(variant: str = "") -> str:
    """The markup: one fixed container holding one picture.

    `alt`-less and `aria-hidden` because this is scenery: it carries no
    information a screen reader should stop for. The sizing, the right-anchored
    60% width, the left-edge fade and the phone dimming live in glass.css
    (`.backdrop`, `.backdrop-art`); this function only says which files.
    """
    v = media_version()
    return f"""
<style>.backdrop-art{{background-image:url("{BACKDROP_JPG}?v={v}");background-image:image-set(url("{BACKDROP_WEBP}?v={v}") type("image/webp"),url("{BACKDROP_JPG}?v={v}") type("image/jpeg"))}}</style>
<div class="backdrop" id="backdrop" aria-hidden="true"><div class="backdrop-art"></div></div>"""
