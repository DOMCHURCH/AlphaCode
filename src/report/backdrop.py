"""The cinematic backdrop: a gradient, then a still, then — sometimes — a video.

Three layers, each of which is a complete answer on its own. That is the whole
design: nothing here is required for the page to look right, so nothing here can
break the page by failing to arrive.

    1. gradient   0 bytes, painted with the first CSS. Dark, with the warm
                  horizon the film has, so a viewer who never gets past this
                  layer sees the intended mood rather than a black hole.
    2. still      ~57 KB WebP (135 KB JPEG for Safari <14). This is what MOST
                  visitors see, and every mobile visitor. It carries the look.
    3. film       ~148 KB WebM / ~254 KB MP4, and only for a desktop that has
                  finished loading, is not on a metered connection, and has not
                  asked for reduced motion.

The source film is 14 MB at 11.4 Mbps. Serving that to a phone before the first
balance sheet appears would be indefensible on a site whose whole argument is
that it respects the reader, so the film is an upgrade that has to earn its
place on every single visit rather than a cost everybody pays.

Not yet wired into any page — the dark redesign it belongs to has not been
written. `render_backdrop()` is ready to drop into a page shell when it is, and
`/static/media/preview.html` shows it working in the meantime.
"""

from __future__ import annotations

from src.report.company_page import asset_version

# One place for the asset paths, so the CSS, the script and the markup cannot
# disagree about what is being loaded.
POSTER_WEBP = "/static/media/backdrop.webp"
POSTER_JPG = "/static/media/backdrop.jpg"
FILM_WEBM = "/static/media/backdrop.webm"
FILM_MP4 = "/static/media/backdrop.mp4"


def render_backdrop() -> str:
    """The markup. No <video> tag: the script adds one only if it should exist.

    A `<video preload="none">` in the HTML would still cost a connection and a
    poster fetch on every page, on every device, and would have to be argued out
    of loading. Nothing is cheaper than an element that is not there.
    """
    return f"""
<div class="backdrop" id="backdrop" aria-hidden="true">
  <div class="backdrop-still"></div>
  <div class="backdrop-veil"></div>
</div>
<script src="/static/backdrop.js?v={asset_version()}" defer></script>"""
