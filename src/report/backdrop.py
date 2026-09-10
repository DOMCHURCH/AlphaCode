"""The backdrop: a gradient, then one still image. No video.

Two layers, and the first is a complete answer on its own:

    1. gradient   0 bytes, painted with the first CSS, plus a 24px blurred
                  placeholder inlined as a data URI. Dark, carrying the
                  picture's own twilight palette, so a viewer who never gets
                  past this layer sees the intended mood rather than a hole.
    2. image      79 KB WebP at 2400px, 27 KB at 1200px, in a <picture>.

**The film is gone.** It was three files -- WebM, MP4 and a JPEG poster --
fetched by a script that could not start until `load` had fired, behind checks
for viewport, connection type and tab visibility. All of that machinery existed
to decide whether to spend 190 KB on decoration, and the decision cost more
than the bytes: the largest element on the page could not begin loading until
everything else had finished.

WHY <picture> AND NOT A CSS `image-set()`. The first attempt at this put both
variants in one `background-image: image-set(...)` rule with `2400w` / `1200w`
descriptors, and shipped a backdrop that was **plain black on every browser**
for the life of three deploys. `image-set()` takes *resolution* descriptors
(`1x`, `2x`); the `w` descriptor is a `srcset` feature and is a parse error
there. An invalid value drops the whole declaration -- so the image and the
placeholder stacked behind it in the same declaration both vanished, and the
`@supports not (image-set(...))` fallback never ran because the thing it tested
for was supported. Nothing logged, nothing 404'd, the bytes sat on the CDN
being served with a 200 to nobody.

`<picture>` has none of that failure mode: `w` descriptors are what `srcset` is
for, the preload scanner sees it in the markup, and `fetchpriority="high"` says
out loud what this element is -- the LCP.
"""

from __future__ import annotations

from pathlib import Path

# One place for the asset paths, so the CSS, the markup and the social cards
# cannot disagree about what is being loaded. The film is gone: see above.
_MEDIA = Path(__file__).parent / "static" / "media"

HERO_2400 = "/static/media/backdrop-2400.webp"
HERO_1200 = "/static/media/backdrop-1200.webp"

# The intrinsic size of the desktop variant. Declared on the <img> so the box
# is known before the bytes land -- a fixed full-screen layer cannot shift the
# page, but stating the ratio keeps the intent legible and costs nothing.
HERO_W, HERO_H = 2400, 1339


def media_version() -> str:
    """Cache-busting stamp for the hero files specifically.

    `asset_version()` walks the top level of `static/` and these live one
    directory down in `media/`, so it does not see them change. That matters
    more here than anywhere else on the site: `/static` is served
    `max-age=31536000, immutable`, so a regenerated image behind an unversioned
    URL reaches nobody who has already visited -- which is precisely the set of
    people most likely to notice the backdrop was broken.
    """
    try:
        return str(int(max(f.stat().st_mtime for f in _MEDIA.iterdir() if f.is_file())))
    except (OSError, ValueError):
        return "0"


def render_backdrop() -> str:
    """The markup. One <picture>, one veil, nothing to sequence.

    `alt=""` and `aria-hidden` on the container because this is scenery: it
    carries no information a screen reader should stop for.
    """
    v = media_version()
    return f"""
<div class="backdrop" id="backdrop" aria-hidden="true">
  <picture class="backdrop-still">
    <source type="image/webp" sizes="100vw"
            srcset="{HERO_1200}?v={v} 1200w, {HERO_2400}?v={v} 2400w">
    <img class="backdrop-img" src="{HERO_2400}?v={v}" alt=""
         width="{HERO_W}" height="{HERO_H}"
         loading="eager" fetchpriority="high" decoding="async">
  </picture>
  <div class="backdrop-veil"></div>
</div>"""
