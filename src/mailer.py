"""Outgoing mail over plain SMTP. One message type: a key someone has lost.

Deliberately stdlib `smtplib` and no third-party sending API. This service sends
a handful of messages a month to addresses that already registered, which is
well under the point where a delivery platform earns its dependency, its key,
and its outage.

Two properties matter more than the sending itself:

* **Unconfigured is a state, not a failure.** With no SMTP_HOST there is no
  attempt and `send_api_key` says so, so the dashboard can offer the human
  fallback instead of promising an email that will never arrive. A recovery flow
  that silently swallows the message is worse than one that admits it is off.
* **Slow relays must not be the caller's problem.** Every socket operation has a
  timeout, and the API sends from a background task, so a relay that hangs costs
  one worker thread for ten seconds rather than the request.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

import structlog

from src.config.settings import get_settings

log = structlog.get_logger(__name__)

# A dead relay must not hold a thread open indefinitely.
_TIMEOUT_S = 10


def is_configured() -> bool:
    return bool(get_settings().smtp_host)


def _sender() -> str:
    """Who the message is from.

    Falls back SMTP_FROM -> SMTP_USER -> ADMIN_EMAIL, because most relays reject
    a From: that is not the mailbox that authenticated, and the commonest
    misconfiguration is setting the credentials and forgetting the address.
    """
    s = get_settings()
    return s.smtp_from or s.smtp_user or s.admin_email or "no-reply@localhost"


def _deliver(msg: EmailMessage) -> None:
    s = get_settings()
    # Implicit TLS on 465, STARTTLS on everything else. Which one is decided by
    # the port because that is the convention every relay follows, and guessing
    # wrong fails loudly at connect rather than sending in the clear.
    if s.smtp_port == 465:
        with smtplib.SMTP_SSL(
            s.smtp_host, s.smtp_port, timeout=_TIMEOUT_S,
            context=ssl.create_default_context(),
        ) as server:
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_pass)
            server.send_message(msg)
        return

    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=_TIMEOUT_S) as server:
        server.ehlo()
        try:
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        except smtplib.SMTPNotSupportedError:
            # A relay with no TLS at all is almost always a local sink in dev.
            # Sending anyway is right there and wrong against a public relay,
            # so it is logged loudly enough to notice in production.
            log.warning("smtp_no_starttls", host=s.smtp_host, port=s.smtp_port)
        if s.smtp_user:
            server.login(s.smtp_user, s.smtp_pass)
        server.send_message(msg)


def send_api_key(email: str, api_key: str) -> bool:
    """Mail somebody their own key. True if it was handed to the relay.

    The key goes to the REGISTERED address and nowhere else -- that is the whole
    security model of this feature. Someone who types another person's address
    into the recovery box causes an email to that person's inbox and learns
    nothing themselves, which is why this can exist while the registration
    endpoint still refuses to show a key at all.

    Never raises. The caller is a background task with nobody to report to, and
    a failed send must leave a log line rather than an unhandled exception in a
    worker.
    """
    if not is_configured():
        log.info("smtp_unconfigured", to=email)
        return False

    s = get_settings()
    msg = EmailMessage()
    msg["Subject"] = "Your To Scale API key"
    msg["From"] = _sender()
    msg["To"] = email
    msg.set_content(
        "Someone asked to be reminded of the To Scale API key for this "
        "address.\n\n"
        f"    {api_key}\n\n"
        "Send it as an X-API-Key header:\n\n"
        f"    curl -H \"X-API-Key: {api_key}\" <host>/api/company/JPM\n\n"
        "If this was not you, nothing has changed -- the key is the same one "
        "you already had, and it was sent only to this address. If you would "
        "like it replaced, reply to this message.\n\n"
        f"— To Scale{chr(10) + s.admin_email if s.admin_email else ''}\n"
    )

    try:
        _deliver(msg)
    except Exception as exc:  # noqa: BLE001 - a failed send is a log line, not a crash
        log.warning(
            "smtp_send_failed", to=email, host=s.smtp_host, error=str(exc)[:200]
        )
        return False
    log.info("smtp_key_sent", to=email)
    return True
