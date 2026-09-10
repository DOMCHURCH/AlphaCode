"""The backdrop: a gradient, then one still image. No video.

Two layers, and the first is a complete answer on its own:

    1. gradient   0 bytes, painted with the first CSS. Dark, with the warm
                  horizon the image has, so a viewer who never gets past this
                  layer sees the intended mood rather than a black hole.
    2. image      108 KB WebP at 2400px, 44 KB at 1200px, plus a 144-byte
                  inline placeholder that paints with the stylesheet.

**The film is gone.** It was three files -- WebM, MP4 and a JPEG poster --
fetched by a script that could not start until `load` had fired, behind checks
for viewport, connection type and tab visibility. All of that machinery existed
to decide whether to spend 190 KB on decoration, and the decision cost more
than the bytes: the largest element on the page could not begin loading until
everything else had finished.

A still image needs none of it. It is in the first stylesheet, the browser
starts it during preload scan, and there is no state to get wrong. The
`prefers-reduced-motion` gate went with it, because a static image has no
motion to reduce -- which also settles the Windows "Show animations" problem
that made Chrome report `reduce` for people who never asked for it.
"""

from __future__ import annotations

# One place for the asset paths, so the CSS, the script and the markup cannot
# disagree about what is being loaded.
# One place for the asset paths, so the CSS and the markup cannot disagree.
# The film is gone: see the module docstring.
HERO_2400 = "/static/media/backdrop-2400.webp"
HERO_1200 = "/static/media/backdrop-1200.webp"


def render_backdrop() -> str:
    """The markup. No <video> tag: the script adds one only if it should exist.

    A `<video preload="none">` in the HTML would still cost a connection and a
    poster fetch on every page, on every device, and would have to be argued out
    of loading. Nothing is cheaper than an element that is not there.
    """
    return """
<div class="backdrop" id="backdrop" aria-hidden="true">
  <div class="backdrop-still"></div>
  <div class="backdrop-veil"></div>
</div>"""
