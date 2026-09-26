"""IndexNow: tell Bing (and Yandex, Seznam, Naver) which pages changed.

Google does not take part; Bing does, and Bing's index is what ChatGPT's
search and Copilot read. Crawling a two-week-old domain is slow, and IndexNow
is how a site says "these URLs changed" instead of waiting.

The key is public by design: the protocol proves ownership by serving it at
`/<key>.txt` on the same host. Submissions run after a deploy's warm-up, in
production only, at most once a day (the throttle lives in the shared cache
table, so a day with ten deploys still pings once).
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

KEY = "dfc0c31c31ef1379d643f51717009850"
HOST = "balanceproof.dev"
ENDPOINT = "https://api.indexnow.org/indexnow"
THROTTLE_KEY = "indexnow:last_submit"
THROTTLE_S = 24 * 3600


def key_file_path() -> str:
    return f"/{KEY}.txt"


def core_urls() -> list[str]:
    """The pages worth re-announcing: the ones that change, and the posts."""
    from src.report.blog import POSTS
    from src.report.compare import PAGES

    base = f"https://{HOST}"
    paths = ["/", "/pricing", "/api", "/restatements", "/dataset", "/blog", "/about"]
    paths += [f"/blog/{p.slug}" for p in POSTS]
    paths += [f"/{p.slug}" for p in PAGES]
    return [base + p for p in paths]


def submit(urls: list[str], *, force: bool = False) -> dict[str, Any]:
    """POST the URLs to IndexNow. Throttled to once a day unless forced."""
    import httpx

    from src.storage.cache import cache_get_json, cache_set_json

    if not force and cache_get_json(THROTTLE_KEY):
        return {"submitted": 0, "skipped": "already submitted in the last 24h"}
    body = {
        "host": HOST,
        "key": KEY,
        "keyLocation": f"https://{HOST}{key_file_path()}",
        "urlList": urls[:10000],
    }
    try:
        r = httpx.post(ENDPOINT, json=body, timeout=20.0)
    except httpx.HTTPError as exc:
        log.warning("indexnow_failed", error=str(exc)[:200])
        return {"submitted": 0, "error": str(exc)[:200]}
    # 200 and 202 are both success (202: key validation pending).
    ok = r.status_code in (200, 202)
    if ok:
        cache_set_json(THROTTLE_KEY, {"at": "now", "count": len(body["urlList"])}, THROTTLE_S)
    log.info("indexnow_submitted", status=r.status_code, urls=len(body["urlList"]))
    return {"submitted": len(body["urlList"]) if ok else 0, "status": r.status_code}


def warm() -> None:
    """Boot hook: announce the core pages, in production only."""
    import os

    # Railway sets this on every deploy; local runs and tests have no such
    # variable, so they never ping a real search engine.
    if os.environ.get("RAILWAY_ENVIRONMENT_NAME") != "production":
        return
    submit(core_urls())
