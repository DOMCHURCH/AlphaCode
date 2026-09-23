"""The site's stylesheets, served as one request.

Every page used to link five sheets, each render-blocking, each its own round
trip before first paint: Lighthouse (2026-09-23) put that at 360ms on mobile and
500ms on desktop, the only real performance item it found. The sheets are still
five files on disk and still layered in the same order -- each loaded after the
last and overriding tokens only -- so a layer is still reverted by editing one
line. That line is now in `LAYERS`, once, instead of in three `<head>` blocks
that had to be kept in step by hand.

Minification is deliberately shallow: comments out, whitespace runs collapsed,
spaces around `{ } ; ,` dropped. Nothing touches `:`, because `.a :hover` and
`.a:hover` are different selectors, and a minifier that knows CSS better than
that is a dependency bought for 23 KiB.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

_STATIC = Path(__file__).parent / "static"

# Order is load order is precedence. To return to the ruled look, swap
# "glass.css" for "terminal.css"; nothing else changes.
LAYERS: tuple[str, ...] = (
    "company.css",    # structure and the drawing
    "dashboard.css",  # accuracy banner and dashboard controls
    "backdrop.css",   # the still behind the page, and the section plates
    "dark.css",       # token overrides: the dark ground, drawings included
    "glass.css",      # token overrides: glass surfaces and soft corners
)

_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"\s*([{};,])\s*")

_lock = threading.Lock()
_cache: tuple[str, str] | None = None  # (asset version, css)


def _minify(css: str) -> str:
    css = _COMMENT.sub("", css)
    css = _SPACE.sub(" ", css)
    return _PUNCT.sub(r"\1", css).strip()


def bundle() -> str:
    """The layers, concatenated in order and minified; rebuilt when any changes."""
    global _cache
    from src.report.company_page import asset_version

    version = asset_version()
    with _lock:
        if _cache is None or _cache[0] != version:
            parts = [
                f"/* {name} */\n" + _minify((_STATIC / name).read_text(encoding="utf-8"))
                for name in LAYERS
            ]
            _cache = (version, "\n".join(parts) + "\n")
        return _cache[1]


def stylesheet_link() -> str:
    """The one `<link>` every page shell carries."""
    from src.report.company_page import asset_version

    return f'<link rel="stylesheet" href="/static/site.css?v={asset_version()}">'
