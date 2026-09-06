"""Outgoing mail over AgentMail. One message type: a key someone has lost.

Replaces the earlier `smtplib` implementation. The contract is unchanged --
`is_configured()` and `send_api_key()` are what the API calls, and neither the
routes nor the dashboard know or care what is behind them.

Two properties carried over from the SMTP version, because they are the ones
that matter to the caller rather than to the transport:

* **Unconfigured is a state, not a failure.** With no AGENTMAIL_API_KEY there is
  no attempt and the dashboard offers the human fallback instead of promising an
  email that will never arrive. That now also covers the SDK simply not being
  installed -- the import is deferred for exactly that reason, so a deploy that
  has not yet picked up requirements.txt degrades to "email is off" rather than
  failing to boot.
* **Slow upstreams must not be the caller's problem.** The client carries its own
  timeout and the API sends from a background task, so a hung request costs one
  worker thread rather than the response.

The inbox is resolved once and cached. AGENTMAIL_INBOX_ID pins it explicitly;
with that unset the inbox is created under a stable `client_id`, which makes the
call idempotent -- a restart reuses the same mailbox instead of accumulating a
new one per deploy.
"""

from __future__ import annotations

import structlog

from src.config.settings import get_settings

log = structlog.get_logger(__name__)

# A hung HTTP call must not hold a worker thread open indefinitely.
_TIMEOUT_S = 10.0

# Stable identity for the mailbox this service sends from, so repeated creates
# resolve to one inbox rather than one per boot.
_INBOX_CLIENT_ID = "to-scale-key-recovery"

# Resolved lazily and kept, because resolving costs a round trip and the answer
# does not change while the process lives.
_inbox_id: str | None = None


def is_configured() -> bool:
    return bool(get_settings().agentmail_api_key)


def reset_inbox_cache() -> None:
    """Test helper, and the escape hatch if the inbox is reconfigured live."""
    global _inbox_id
    _inbox_id = None


def _why(exc: Exception) -> str:
    """One readable line out of an SDK error.

    `str(ApiError)` leads with a full dump of the response headers, so the
    status and the message -- the only two things worth reading at 2am -- are
    pushed past the end of a truncated log line. This puts them first: an
    unusable "headers: {'content-type': ..." is exactly the kind of log that
    sent the last debugging session hunting in the wrong place.
    """
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if status is not None:
        detail = body
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or body
        return f"HTTP {status}: {str(detail)[:150]}"
    return f"{type(exc).__name__}: {str(exc)[:150]}"


def _client():
    """An AgentMail client, or None if the SDK or the key is missing.

    The import is inside the function on purpose. A module-level import would
    make `agentmail` a hard requirement of starting the service at all, so a
    container built before requirements.txt gained the package would crash on
    boot instead of simply having key recovery switched off.
    """
    s = get_settings()
    if not s.agentmail_api_key:
        return None
    try:
        from agentmail import AgentMail
    except ImportError:
        log.warning("agentmail_sdk_missing", fix="pip install agentmail")
        return None
    return AgentMail(api_key=s.agentmail_api_key, timeout=_TIMEOUT_S)


def _resolve_inbox(client) -> str | None:
    """The inbox id to send from. Pinned by config, or created and reused.

    Creating with a fixed `client_id` is the documented idempotent path, but a
    server that answers an existing client_id with a conflict rather than the
    existing inbox is an equally reasonable reading of the same API -- so the
    conflict is handled by going and finding it. Getting this wrong in the
    optimistic direction would mint a fresh mailbox on every deploy.
    """
    global _inbox_id
    if _inbox_id:
        return _inbox_id

    s = get_settings()
    if s.agentmail_inbox_id:
        _inbox_id = s.agentmail_inbox_id
        return _inbox_id

    from agentmail.inboxes import CreateInboxRequest

    try:
        inbox = client.inboxes.create(
            request=CreateInboxRequest(
                client_id=_INBOX_CLIENT_ID,
                display_name="To Scale",
            )
        )
        _inbox_id = inbox.inbox_id
        log.info("agentmail_inbox_ready", inbox=inbox.email)
        return _inbox_id
    except Exception as exc:  # noqa: BLE001 - fall through to the lookup below
        log.info("agentmail_inbox_create_declined", error=_why(exc))

    try:
        existing = client.inboxes.list()
        for inbox in existing.inboxes or []:
            if inbox.client_id == _INBOX_CLIENT_ID:
                _inbox_id = inbox.inbox_id
                log.info("agentmail_inbox_found", inbox=inbox.email)
                return _inbox_id
        # No tagged inbox, but the account has one: use it rather than refusing
        # to send over a naming convention.
        if existing.inboxes:
            _inbox_id = existing.inboxes[0].inbox_id
            log.warning(
                "agentmail_inbox_fallback", inbox=existing.inboxes[0].email
            )
            return _inbox_id
    except Exception as exc:  # noqa: BLE001
        log.warning("agentmail_inbox_lookup_failed", error=_why(exc))
    return None


def _body(api_key: str, admin_email: str) -> str:
    signature = f"\n{admin_email}" if admin_email else ""
    return (
        "Someone asked to be reminded of the To Scale API key for this "
        "address.\n\n"
        f"    {api_key}\n\n"
        "Send it as an X-API-Key header:\n\n"
        f'    curl -H "X-API-Key: {api_key}" <host>/api/company/JPM\n\n'
        "If this was not you, nothing has changed -- the key is the same one "
        "you already had, and it was sent only to this address. If you would "
        "like it replaced, reply to this message.\n\n"
        f"— To Scale{signature}\n"
    )


def send_api_key(email: str, api_key: str) -> bool:
    """Mail somebody their own key. True if AgentMail accepted it.

    The key goes to the REGISTERED address and nowhere else -- that is the whole
    security model of this feature. Someone who types another person's address
    into the recovery box causes an email to that person's inbox and learns
    nothing themselves, which is why this can exist while registration still
    refuses to show a key at all.

    Never raises. The caller is a background task with nobody to report to, and
    a failed send must leave a log line rather than an unhandled exception in a
    worker thread.
    """
    client = _client()
    if client is None:
        log.info("agentmail_unconfigured", to=email)
        return False

    inbox_id = _resolve_inbox(client)
    if inbox_id is None:
        log.warning("agentmail_no_inbox", to=email)
        return False

    s = get_settings()
    try:
        client.inboxes.messages.send(
            inbox_id,
            to=email,
            subject="Your To Scale API key",
            text=_body(api_key, s.admin_email),
            reply_to=s.admin_email or None,
        )
    except Exception as exc:  # noqa: BLE001 - a failed send is a log line, not a crash
        log.warning("agentmail_send_failed", to=email, error=_why(exc))
        return False
    log.info("agentmail_key_sent", to=email)
    return True


def send_magic_link(email: str, url: str, ttl_minutes: int = 15) -> bool:
    """Mail a login link. True if AgentMail accepted it.

    Same never-raises contract as `send_api_key`: the caller is a background
    task with nobody to report an exception to.

    The body says what to do if it was not you, because this is the one message
    the service sends to addresses that have never registered -- somebody typing
    a stranger's address into the login box causes mail to that stranger, and
    they are owed an explanation rather than a bare link.
    """
    client = _client()
    if client is None:
        log.info("agentmail_unconfigured", to=email)
        return False
    inbox_id = _resolve_inbox(client)
    if inbox_id is None:
        log.warning("agentmail_no_inbox", to=email)
        return False

    body = (
        "Click here to log in to To Scale:\n\n"
        f"    {url}\n\n"
        f"This link expires in {ttl_minutes} minutes and can be used once.\n\n"
        "If you didn't request this, you can safely ignore this email -- no "
        "account was created and nothing has changed.\n"
    )
    try:
        client.inboxes.messages.send(
            inbox_id,
            to=email,
            subject="Log in to To Scale",
            text=body,
            reply_to=get_settings().admin_email or None,
        )
    except Exception as exc:  # noqa: BLE001 - a failed send is a log line
        log.warning("agentmail_link_failed", to=email, error=_why(exc))
        return False
    log.info("agentmail_link_sent", to=email)
    return True
