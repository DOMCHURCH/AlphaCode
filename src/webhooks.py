"""Webhooks: signed POSTs of watchlist alerts to the customer's own URL (Business).

The URL is supplied by a customer and fetched by this server, which is the
textbook SSRF shape. So, on registration AND before every delivery:

- https only, default port or 443/8443, no credentials in the URL;
- every address the host resolves to must be public -- loopback, private
  (RFC 1918 / ULA), link-local (incl. 169.254.169.254 metadata), multicast,
  reserved and unspecified are all refused;
- redirects are not followed; 5-second timeout; the response body is ignored.

Each delivery is signed so the receiver can check it came from here:

    BalanceProof-Signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>

The secret is generated here and shown ONCE, when the endpoint is created.
Three attempts with backoff; an endpoint that fails 10 deliveries in a row is
switched off and says why.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import time
from typing import Any
from urllib.parse import urlsplit

import structlog
from fastapi import HTTPException
from sqlalchemy import select

from src.storage.db import session_scope

log = structlog.get_logger(__name__)

TIMEOUT_S = 5.0
ATTEMPTS = 3
BACKOFF_S = (0.0, 1.0, 3.0)
DISABLE_AFTER = 10
_ALLOWED_PORTS = (None, 443, 8443)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def check_url(url: str) -> str:
    """The URL, normalised, or a 422 saying exactly what is wrong with it."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError as exc:
        raise HTTPException(422, "That is not a URL.") from exc
    if parts.scheme != "https":
        raise HTTPException(422, "Webhook URLs must use https.")
    if parts.username or parts.password:
        raise HTTPException(422, "Webhook URLs cannot carry credentials.")
    if not parts.hostname:
        raise HTTPException(422, "That URL has no host.")
    try:
        port = parts.port
    except ValueError as exc:
        raise HTTPException(422, "That URL has an invalid port.") from exc
    if port not in _ALLOWED_PORTS:
        raise HTTPException(422, "Webhook URLs must use port 443 or 8443.")
    try:
        addresses = _resolve(parts.hostname)
    except OSError as exc:
        raise HTTPException(422, f"{parts.hostname} does not resolve.") from exc
    if not addresses:
        raise HTTPException(422, f"{parts.hostname} does not resolve.")
    for a in addresses:
        ip = ipaddress.ip_address(a.split("%")[0])
        # ::ffff:10.0.0.5 is 10.0.0.5; judge the address it actually reaches.
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast:
            raise HTTPException(
                422, f"{parts.hostname} resolves to a non-public address; "
                     "webhooks can only be sent to the public internet.")
    return parts.geturl()


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


# ---------------------------------------------------------------------------
# Endpoint management
# ---------------------------------------------------------------------------

def _public(row: Any) -> dict[str, Any]:
    return {
        "id": row.id, "url": row.url, "active": bool(row.active),
        "secret_prefix": row.secret[:8],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "consecutive_failures": int(row.consecutive_failures or 0),
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_error": row.last_error,
    }


def list_for(account: Any) -> list[dict[str, Any]]:
    from src.storage.models import WebhookEndpoint

    with session_scope() as session:
        rows = session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.user_id == account.id)
            .order_by(WebhookEndpoint.id)
        ).scalars().all()
        return [_public(r) for r in rows]


def create(account: Any, url: str) -> dict[str, Any]:
    from src import plans
    from src.storage.models import WebhookEndpoint

    limit = plans.require(account.tier, "webhooks")
    clean = check_url(url)
    with session_scope() as session:
        n = len(session.execute(
            select(WebhookEndpoint.id).where(WebhookEndpoint.user_id == account.id)
        ).all())
        if limit is not None and n >= limit:
            raise HTTPException(403, detail={"error": "webhook_limit", "limit": limit})
        secret = "whsec_bp_" + secrets.token_urlsafe(32)
        row = WebhookEndpoint(user_id=account.id, url=clean, secret=secret, active=True)
        session.add(row)
        session.flush()
        out = _public(row)
    out["secret"] = secret
    out["note"] = "Store this secret now; it is not shown again."
    return out


def delete(account: Any, endpoint_id: int) -> bool:
    from src.storage.models import WebhookEndpoint

    with session_scope() as session:
        row = session.get(WebhookEndpoint, endpoint_id)
        if row is None or row.user_id != account.id:
            return False
        session.delete(row)
        return True


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str]:
    import httpx

    try:
        check_url(url)  # re-resolved at send time, not trusted from signup
    except HTTPException as exc:
        return None, str(exc.detail)
    try:
        r = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT_S,
                       follow_redirects=False)
    except httpx.HTTPError as exc:
        return None, type(exc).__name__
    return r.status_code, "" if 200 <= r.status_code < 300 else f"HTTP {r.status_code}"


def _deliver_one(row: Any, event: str, data: Any) -> bool:
    body = json.dumps({"event": event, "created": int(time.time()), "data": data},
                      default=str, separators=(",", ":")).encode()
    ok, error, status = False, "", None
    for wait in BACKOFF_S[:ATTEMPTS]:
        if wait:
            time.sleep(wait)
        status, error = _post(row.url, body, {
            "Content-Type": "application/json",
            "User-Agent": "BalanceProof-Webhooks/1",
            "BalanceProof-Event": event,
            "BalanceProof-Signature": sign(row.secret, body),
        })
        if not error:
            ok = True
            break
    from src.storage.models import WebhookDelivery, WebhookEndpoint

    with session_scope() as session:
        ep = session.get(WebhookEndpoint, row.id)
        if ep is not None:
            if ok:
                ep.consecutive_failures = 0
                ep.last_success_at = _now()
                ep.last_error = None
            else:
                ep.consecutive_failures = int(ep.consecutive_failures or 0) + 1
                ep.last_error = (error or "failed")[:200]
                if ep.consecutive_failures >= DISABLE_AFTER:
                    ep.active = False
                    ep.last_error = f"disabled after {DISABLE_AFTER} failed deliveries: {ep.last_error}"
        session.add(WebhookDelivery(endpoint_id=row.id, event=event, ok=ok,
                                    status_code=status, error=(error or None)))
    log.info("webhook_delivered" if ok else "webhook_failed",
             endpoint=row.id, kind=event, status=status)
    return ok


def _endpoints(user_id: str) -> list[Any]:
    from src.storage.models import WebhookEndpoint

    with session_scope() as session:
        rows = session.execute(
            select(WebhookEndpoint).where(WebhookEndpoint.user_id == user_id,
                                          WebhookEndpoint.active.is_(True))
        ).scalars().all()
        for r in rows:
            session.expunge(r)
        return list(rows)


def deliver_alert(user_id: str, filings: list[dict[str, Any]]) -> int:
    """POST one `watchlist.filed` event to each of the account's endpoints."""
    return sum(_deliver_one(ep, "watchlist.filed", {"filings": filings})
               for ep in _endpoints(user_id))


def send_test(account: Any, endpoint_id: int) -> bool:
    from src.storage.models import WebhookEndpoint

    with session_scope() as session:
        row = session.get(WebhookEndpoint, endpoint_id)
        if row is None or row.user_id != account.id:
            raise HTTPException(404, "No such webhook.")
        session.expunge(row)
    return _deliver_one(row, "ping", {"message": "BalanceProof webhook test"})
