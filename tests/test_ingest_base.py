"""Shared HTTP client behaviours."""

from __future__ import annotations

import asyncio
import json

from src.ingest.base import APIClient, PermanentAPIError


def test_403_error_includes_the_response_body(monkeypatch):
    """A 403 must carry the provider's body so an unactivated/misconfigured key
    ("Unknown API Key") is distinguishable from a plan limit ("NOT_AUTHORIZED").
    Polygon returned a bare 403 before this, which read as a tier problem when it
    may be a bad key."""

    class _Resp:
        status_code = 403
        text = '{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data"}'

        def json(self):
            return json.loads(self.text)

    async def run() -> str:
        async with APIClient("polygon", "https://example.invalid") as c:
            async def _get(url, params=None):
                return _Resp()

            c._client.get = _get  # type: ignore[assignment]
            try:
                await c.get_json("/v2/x", use_cache=False)
            except PermanentAPIError as exc:
                return str(exc)
            return "no error raised"

    msg = asyncio.run(run())
    assert "NOT_AUTHORIZED" in msg
    assert "not entitled" in msg
