"""Helpers for tests that read the sitemap.

/sitemap.xml is an INDEX: one child file per page type (`/sitemap-blog.xml`,
`/sitemap-company-1.xml`, ...). A test that asks "is this URL listed?" wants the
union of the children, so these fetch them the way a crawler would.
"""

from __future__ import annotations

import re

_CHILD = re.compile(r"<loc>https?://[^/<]+(/sitemap-[^<]+\.xml)</loc>")


def sitemap_children(client, headers=None) -> list[str]:
    """The paths of the child files the index lists."""
    index = client.get("/sitemap.xml", headers=headers or {}).text
    return _CHILD.findall(index)


def sitemap_flat(client, headers=None) -> str:
    """Every child file concatenated: all the URLs the sitemap lists, as one string."""
    return "\n".join(
        client.get(path, headers=headers or {}).text
        for path in sitemap_children(client, headers)
    )


def sitemap_locs(sitemap_module, base: str = "https://balanceproof.dev") -> str:
    """The same union, straight from the module (no HTTP), as `<loc>` lines."""
    return "\n".join(f"<loc>{loc}</loc>" for loc, _ in sitemap_module.all_urls(base))
