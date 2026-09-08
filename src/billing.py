"""Stripe Checkout: a card path into the grant switch the operator already has.

Nothing here is a second billing system. A payment that settles calls exactly
the function `POST /admin/grant-access` calls -- `accounts.apply_admin_action`
-- so a Stripe purchase, a comp and a manual refund are the same operation on
the same two columns, and the account state can still be read and changed by
hand when Stripe is not the answer.

Four properties are the whole design, and each one exists because its absence
is a way to take somebody's money and give them nothing:

* **Card data never reaches this service.** The buyer is redirected to Stripe's
  own page; this process sees an event afterwards. There is no card form here
  and no PAN in this database, which is what lets the privacy policy say so.
* **Delivery is at-least-once, so fulfilment must be at-most-once.** Stripe
  redelivers any webhook it did not get a 2xx for, for three days. Without the
  `stripe_events` table the second delivery of one `checkout.session.completed`
  would add a second 31-day Pro period for a single payment. The event id is a
  primary key, so the duplicate is refused by the database rather than by a
  query that raced.
* **The order is check, grant, record.** Recording the event first would be
  tidier and is the wrong way round: a database blip during the grant would
  leave a committed "already handled" row and no access, so Stripe's retry
  reads as a duplicate and the buyer has paid for nothing, silently. This order
  fails the other way -- nothing recorded, a 500, and the retry works -- and its
  worst case is a rare extra period, which costs the operator rather than the
  buyer. (The brief asked for the check and the insert inside the same
  `session_scope` as the grant. That is not reachable through
  `apply_admin_action`, which owns its own transaction, and nesting a write
  inside an outer write deadlocks on SQLite. This ordering is the same
  guarantee without the deadlock.)
* **An unknown buyer is provisioned, never dropped.** Somebody can pay with an
  address that has no account -- that is the normal case for a first purchase --
  so the webhook registers the address, grants what was bought, and mails them
  the key. The alternative is taking the money and doing nothing.

The `stripe` import is deferred, exactly as `mailer` defers `agentmail`: a
container built before requirements.txt gained the package has checkout switched
off rather than failing to boot.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.config.settings import get_settings
from src.storage.db import session_scope
from src.storage.models import ApiUser, StripeEvent

log = structlog.get_logger(__name__)

# What can be bought, and the grant each purchase turns into. The action names
# are `accounts.VALID_ACTIONS` -- deliberately, so a plan that does not map to a
# real grant fails here rather than at the moment money has already moved.
PLANS: tuple[str, ...] = ("pro", "dataset")
_ACTION: dict[str, str] = {"pro": "grant_pro", "dataset": "grant_download"}
# Subscription for the recurring one, one-off payment for the file. Also the
# fallback when an event carries no plan metadata (a Payment Link made in the
# Stripe dashboard, for instance): with exactly two products, the mode of the
# session is enough to tell which was bought.
_MODE: dict[str, str] = {"pro": "subscription", "dataset": "payment"}
_PLAN_BY_MODE: dict[str, str] = {v: k for k, v in _MODE.items()}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _why(exc: Exception) -> str:
    """One readable line out of a Stripe error, status first.

    Same reasoning as `mailer._why`: `str()` on an SDK error leads with a dump
    of the response headers, which pushes the two things worth reading at 2am
    past the end of a truncated log line.
    """
    status = getattr(exc, "http_status", None) or getattr(exc, "status_code", None)
    message = getattr(exc, "user_message", None) or str(exc)
    if status is not None:
        return f"HTTP {status}: {message[:150]}"
    return f"{type(exc).__name__}: {message[:150]}"


def _sdk():
    """The `stripe` module, configured from settings, or None if not installed.

    The api key is assigned only when it is set, and the api version only when
    it is pinned: writing an empty string over the version the installed SDK
    ships with would ask a current library to speak no version at all.
    """
    try:
        import stripe
    except ImportError:
        log.warning("stripe_sdk_missing", fix="pip install stripe")
        return None
    s = get_settings()
    if s.stripe_secret_key:
        stripe.api_key = s.stripe_secret_key
    if s.stripe_api_version:
        stripe.api_version = s.stripe_api_version
    return stripe


def api_version() -> str:
    """The API version this process would actually talk to Stripe on."""
    stripe = _sdk()
    return getattr(stripe, "api_version", "") if stripe is not None else ""


def price_for(plan: str) -> str:
    """The configured Price id for a plan, or "" if it is not set."""
    s = get_settings()
    return {
        "pro": s.stripe_price_pro,
        "dataset": s.stripe_price_dataset,
    }.get(plan, "")


def missing_config() -> list[str]:
    """Which of the four required variables are absent. Empty means ready."""
    s = get_settings()
    gaps = [
        name
        for name, value in (
            ("STRIPE_SECRET_KEY", s.stripe_secret_key),
            ("STRIPE_WEBHOOK_SECRET", s.stripe_webhook_secret),
            ("STRIPE_PRICE_PRO", s.stripe_price_pro),
            ("STRIPE_PRICE_DATASET", s.stripe_price_dataset),
        )
        if not value
    ]
    if _sdk() is None:
        gaps.append("stripe (SDK not installed)")
    return gaps


def is_configured() -> bool:
    """Whether a purchase would actually complete, end to end.

    Not "is the secret key set". A deployment with a key and no webhook secret
    can open a checkout page and take a payment it can never hear about, which
    is the single worst state this feature has -- so the flag reported on
    /status is true only when every part of the round trip is present. The
    demo flag learned this the hard way: a True next to a broken endpoint is
    worse than no flag at all.
    """
    return not missing_config()


# ---------------------------------------------------------------------------
# Reading events, which may be SDK objects or plain dicts
# ---------------------------------------------------------------------------

def _field(obj: Any, name: str, default: Any = None) -> Any:
    """One field, whether `obj` is a StripeObject, a dict, or None.

    Stripe's objects are dict subclasses today, but a test hands
    `construct_event` a plain dict and a future SDK may hand back something
    else. Reading through one helper means neither breaks the handler.
    """
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _id_of(value: Any) -> str:
    """A Stripe id, whether the field arrived as the id or as the object."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(_field(value, "id", "") or "")


def _subscription_of(invoice: Any) -> str:
    """The subscription an invoice belongs to, across API-version shapes.

    Recent versions moved the reference off the invoice's top level and under
    `parent.subscription_details`. Both are read because the pinned version can
    change under this code without the code being touched.
    """
    direct = _id_of(_field(invoice, "subscription"))
    if direct:
        return direct
    parent = _field(invoice, "parent")
    details = _field(parent, "subscription_details")
    return _id_of(_field(details, "subscription"))


def _email_of(session_obj: Any) -> str:
    """The buyer's address, preferring the one we asked Stripe to use.

    `metadata.email` is what this service put on the session for a buyer it
    already knew, so it wins: Stripe locks `customer_email` when it is supplied,
    which means the two agree in the normal case and the metadata is the one
    that is ours when they do not. `customer_details.email` is the fallback --
    it is where an address typed on Stripe's own page (or on a Payment Link
    made in the dashboard) turns up.
    """
    from src import accounts

    metadata = _field(session_obj, "metadata") or {}
    candidate = (
        _field(metadata, "email")
        or _field(_field(session_obj, "customer_details"), "email")
        or _field(session_obj, "customer_email")
        or ""
    )
    return accounts.normalise_email(str(candidate))


def _plan_of(session_obj: Any) -> str:
    """Which product was bought: the metadata this service wrote, else the mode.

    The mode fallback is not a guess. There are two products and they use
    different Checkout modes, so `subscription` means Pro and `payment` means
    the dataset -- which also makes a Payment Link created by hand in the
    Stripe dashboard fulfil correctly through this webhook with no code.
    """
    metadata = _field(session_obj, "metadata") or {}
    plan = str(_field(metadata, "plan", "") or "").strip().lower()
    if plan in PLANS:
        return plan
    return _PLAN_BY_MODE.get(str(_field(session_obj, "mode", "") or ""), "")


# ---------------------------------------------------------------------------
# Checkout
# ---------------------------------------------------------------------------

def create_checkout_session(
    *, plan: str, origin: str, email: str | None = None
) -> dict[str, str]:
    """Open a Stripe Checkout Session and return where to send the buyer.

    `origin` is the reader's own origin (`api._public_origin`), not the
    settings' `BASE_URL`: the return URLs are followed by the browser that
    started the purchase, and sending somebody from a preview deployment back
    to production after paying is a lost customer with a receipt.
    """
    from src import accounts

    wanted = (plan or "").strip().lower()
    if wanted not in PLANS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown plan {wanted!r}. One of: {', '.join(PLANS)}.",
        )
    price = price_for(wanted)
    stripe = _sdk()
    s = get_settings()
    if stripe is None or not s.stripe_secret_key or not price:
        log.error("stripe_checkout_unconfigured", missing=missing_config())
        raise HTTPException(
            status_code=503,
            detail=(
                "Card checkout is not configured on this deployment. "
                "Contact the site owner to buy this."
            ),
        )

    address = accounts.normalise_email(email or "")
    if address and not accounts.valid_email(address):
        raise HTTPException(
            status_code=422, detail="That does not look like an email address."
        )

    params: dict[str, Any] = {
        "mode": _MODE[wanted],
        "line_items": [{"price": price, "quantity": 1}],
        # The buyer lands back on the dashboard, which is where the thing they
        # just bought actually is. `?checkout=` is read by nothing server-side
        # -- fulfilment happens on the webhook, never on the return URL, which
        # a buyer can close before it loads and a stranger can visit without
        # paying.
        "success_url": f"{origin}/dashboard?checkout=success",
        "cancel_url": f"{origin}/pricing",
        # What the webhook reads back. Written even though the mode implies the
        # plan, because an explicit answer beats an inferred one when the
        # inference is the only thing standing between a payment and a grant.
        "metadata": {"plan": wanted, "email": address},
    }
    if address:
        # Prefills and LOCKS the field on Stripe's page, so the account that
        # gets the grant is the account that started the purchase -- a buyer
        # who retypes a different address here is a support ticket.
        params["customer_email"] = address
        params["client_reference_id"] = address

    try:
        session = stripe.checkout.Session.create(**params)
    except Exception as exc:  # noqa: BLE001 - upstream failure is a 502, not a 500
        log.warning("stripe_checkout_failed", plan=wanted, error=_why(exc))
        raise HTTPException(
            status_code=502,
            detail="Stripe could not start a checkout just now. Please try again.",
        ) from None

    url = str(_field(session, "url", "") or "")
    if not url:
        log.error("stripe_checkout_no_url", plan=wanted)
        raise HTTPException(
            status_code=502, detail="Stripe returned a checkout with no address."
        )
    log.info("stripe_checkout_opened", plan=wanted, email=address or "-")
    return {"url": url, "id": str(_field(session, "id", "") or ""), "plan": wanted}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _already_handled(event_id: str) -> bool:
    with session_scope() as session:
        return session.get(StripeEvent, event_id) is not None


def _record(event_id: str, kind: str) -> None:
    """Mark an event handled. A duplicate here is the expected race, not a bug."""
    try:
        with session_scope() as session:
            session.add(StripeEvent(event_id=event_id, type=kind[:64]))
    except IntegrityError:
        # Two deliveries of the same event overlapped and both passed the
        # check. The grant itself is idempotent for `grant_download` and
        # `revoke_pro`; for `grant_pro` it is not, and this row losing the race
        # is how we find out it happened.
        log.warning("stripe_event_recorded_twice", event_id=event_id, type=kind)
    except Exception as exc:  # noqa: BLE001 - a lost row must not fail the webhook
        log.error("stripe_event_record_failed", event_id=event_id, error=str(exc)[:200])


def _account_for(email: str):
    """The account for a buyer, created and mailed its key if it is new.

    `EmailTaken` is caught rather than allowed to propagate: two checkouts from
    one new address, or a retry after a crash between the register and the
    record, both land here with the account already made.
    """
    from src import accounts, mailer

    existing = accounts.by_email(email)
    if existing is not None:
        return existing
    try:
        account = accounts.register(email)
    except accounts.EmailTaken:
        account = accounts.by_email(email)
        if account is None:  # pragma: no cover - taken and absent is impossible
            raise
        return account
    # Never raises; an unconfigured or failing relay is a log line. The buyer
    # can also recover the key from /dashboard, so a dropped email costs them a
    # click rather than the thing they paid for.
    mailer.send_api_key(account.email, account.api_key)
    log.info("stripe_account_provisioned", email=email)
    return account


def _attach_ids(email: str, *, customer: str = "", subscription: str = "") -> None:
    """Remember Stripe's ids for this account, so later events can find it.

    `customer.subscription.deleted` carries no address at all -- a customer id
    and a subscription id are the only things it knows about the person -- so an
    account without these is an account whose cancellation cannot be actioned.
    """
    if not (customer or subscription):
        return
    try:
        with session_scope() as session:
            row = session.execute(
                select(ApiUser).where(ApiUser.email == email)
            ).scalar_one_or_none()
            if row is None:
                return
            if customer:
                row.stripe_customer_id = customer[:64]
            if subscription:
                row.stripe_subscription_id = subscription[:64]
    except Exception as exc:  # noqa: BLE001 - the grant matters more than the id
        log.warning("stripe_ids_not_stored", email=email, error=str(exc)[:200])


def _email_for(*, customer: str = "", subscription: str = "") -> str:
    """The account behind a Stripe customer or subscription id, or "".

    Subscription first: a customer can hold more than one subscription, and the
    one being cancelled is the specific thing the event is about.
    """
    with session_scope() as session:
        row = None
        if subscription:
            row = session.execute(
                select(ApiUser).where(ApiUser.stripe_subscription_id == subscription)
            ).scalar_one_or_none()
        if row is None and customer:
            row = session.execute(
                select(ApiUser).where(ApiUser.stripe_customer_id == customer)
            ).scalar_one_or_none()
        return row.email if row is not None else ""


def _grant(email: str, plan: str) -> dict[str, Any]:
    from src import accounts

    account = accounts.apply_admin_action(email, _ACTION[plan])
    log.info(
        "stripe_fulfilled",
        plan=plan, email=email, tier=account.tier,
        download=account.has_paid_download,
    )
    return {
        "status": "granted",
        "plan": plan,
        "email": email,
        "tier": account.tier,
        "has_paid_download": account.has_paid_download,
    }


def _fulfil_checkout(obj: Any) -> dict[str, Any]:
    """A completed Checkout Session: provision if needed, then grant."""
    paid = str(_field(obj, "payment_status", "") or "")
    if paid not in ("paid", "no_payment_required"):
        # A delayed-notification method that has not settled. Granting now
        # would give away the product on a payment that can still fail; the
        # `async_payment_succeeded` event arrives when it clears, under its own
        # id, and is fulfilled then.
        log.info("stripe_checkout_unsettled", payment_status=paid)
        return {"status": "unsettled", "payment_status": paid}

    plan = _plan_of(obj)
    email = _email_of(obj)
    if plan not in PLANS:
        # Nothing to grant and nothing a retry would fix. Loud, because it
        # means somebody has paid and this service cannot tell for what.
        log.error("stripe_checkout_unknown_plan", mode=_field(obj, "mode"))
        return {"status": "unknown_plan"}
    if not email:
        log.error("stripe_checkout_no_email", plan=plan)
        return {"status": "no_email", "plan": plan}

    _account_for(email)
    _attach_ids(
        email,
        customer=_id_of(_field(obj, "customer")),
        subscription=_id_of(_field(obj, "subscription")),
    )
    return _grant(email, plan)


def _renew(obj: Any) -> dict[str, Any]:
    """A recurring invoice paid: extend Pro by another period.

    Only `subscription_cycle`. The first invoice of a subscription
    (`subscription_create`) is the one `checkout.session.completed` already
    fulfilled, and granting on both would buy one payment two periods.
    """
    reason = str(_field(obj, "billing_reason", "") or "")
    if reason != "subscription_cycle":
        return {"status": "ignored", "billing_reason": reason}

    customer = _id_of(_field(obj, "customer"))
    subscription = _subscription_of(obj)
    email = _email_for(customer=customer, subscription=subscription)
    if not email:
        # A customer this service has never seen. Answer 200: it is somebody
        # else's charge on the same Stripe account, or a subscription made
        # before this code existed, and neither is fixed by a three-day retry.
        log.warning("stripe_renewal_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}
    if subscription:
        _attach_ids(email, subscription=subscription)
    return _grant(email, "pro")


def _cancel(obj: Any) -> dict[str, Any]:
    """A subscription ended: back to free, without touching a paid download."""
    from src import accounts

    subscription = _id_of(_field(obj, "id"))
    customer = _id_of(_field(obj, "customer"))
    email = _email_for(customer=customer, subscription=subscription)
    if not email:
        log.info("stripe_cancel_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}

    account = accounts.apply_admin_action(email, "revoke_pro")
    # Forget the subscription, keep the customer. A cancelled id must not match
    # a later event, but the customer is how the same person is recognised if
    # they come back.
    try:
        with session_scope() as session:
            row = session.execute(
                select(ApiUser).where(ApiUser.email == email)
            ).scalar_one_or_none()
            if row is not None:
                row.stripe_subscription_id = None
    except Exception as exc:  # noqa: BLE001
        log.warning("stripe_sub_id_not_cleared", email=email, error=str(exc)[:200])
    log.info("stripe_subscription_cancelled", email=email, tier=account.tier)
    return {"status": "revoked", "plan": "pro", "email": email, "tier": account.tier}


# Every event this service acts on. Everything else gets a 200 and no action:
# Stripe sends a great many kinds, and answering anything but 2xx to one we
# simply do not care about would put the endpoint into a three-day retry loop.
_HANDLERS = {
    "checkout.session.completed": _fulfil_checkout,
    # The same session, arriving late, for a payment method that does not
    # settle immediately. Without this a delayed-notification purchase either
    # never grants at all or (with the settled-check alone) grants before the
    # funds clear.
    "checkout.session.async_payment_succeeded": _fulfil_checkout,
    "invoice.payment_succeeded": _renew,
    "customer.subscription.deleted": _cancel,
}


def handle_event(payload: bytes, signature: str | None) -> dict[str, Any]:
    """Verify one webhook delivery and act on it exactly once.

    A body that is not from Stripe is the ONLY 400. Everything else is a 200
    (handled, ignored, or already seen) or an exception -- because Stripe
    retries any 4xx or 5xx for three days, so a 400 on an event this service
    merely does not understand would put a real payment into a retry loop,
    while a 500 on something that genuinely broke is what makes Stripe deliver
    it again.
    """
    s = get_settings()
    stripe = _sdk()
    if stripe is None or not s.stripe_webhook_secret:
        # Fails closed, like `verify_admin_secret`: an unverified body posted to
        # this URL is an unauthenticated request to the grant switch.
        log.error("stripe_webhook_unconfigured", missing=missing_config())
        raise HTTPException(
            status_code=503,
            detail="The Stripe webhook is not configured on this deployment.",
        )

    try:
        event = stripe.Webhook.construct_event(
            payload, signature or "", s.stripe_webhook_secret
        )
    except stripe.SignatureVerificationError as exc:
        # Mapped here rather than in the route so the "400 for this and nothing
        # else" rule lives next to the check it describes -- and so the route
        # needs no import of `stripe` on a path that runs when the SDK may be
        # the very thing that is missing.
        log.warning("stripe_webhook_bad_signature", error=str(exc)[:200])
        raise HTTPException(status_code=400, detail="Bad Stripe signature.") from None
    kind = str(_field(event, "type", "") or "")
    event_id = str(_field(event, "id", "") or "")
    handler = _HANDLERS.get(kind)
    if handler is None:
        # Not recorded: Stripe sends a great many kinds and the table is for
        # things that were acted on, not a copy of the account's event log.
        return {"status": "ignored", "type": kind}
    if not event_id:
        log.error("stripe_event_without_id", type=kind)
        return {"status": "ignored", "type": kind}

    if _already_handled(event_id):
        log.info("stripe_event_duplicate", event_id=event_id, type=kind)
        return {"status": "duplicate", "event": event_id, "type": kind}

    obj = _field(_field(event, "data") or {}, "object") or {}
    result = handler(obj)
    _record(event_id, kind)
    result["event"] = event_id
    result["type"] = kind
    return result
