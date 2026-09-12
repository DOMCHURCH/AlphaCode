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

import datetime as dt
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
PLANS: tuple[str, ...] = ("pro", "pro_annual", "dataset")
_ACTION: dict[str, str] = {
    "pro": "grant_pro",
    # The annual plan is the SAME grant. Nothing downstream of the payment
    # knows about billing periods -- there is one Pro tier, and how long it was
    # paid for is Stripe's business. Renewal is what keeps it on: the
    # subscription-lifecycle events already handled here revoke Pro when a
    # subscription ends, whether it was billed monthly or yearly.
    "pro_annual": "grant_pro",
    "dataset": "grant_download",
}
# Subscription for the recurring ones, one-off payment for the file. Also the
# fallback when an event carries no plan metadata (a Payment Link made in the
# Stripe dashboard, for instance).
_MODE: dict[str, str] = {
    "pro": "subscription",
    "pro_annual": "subscription",
    "dataset": "payment",
}
# Written out rather than derived by reversing `_MODE`, which is the bug this
# line exists to not have: two plans now share the `subscription` mode, so a
# `{v: k for k, v in _MODE.items()}` would silently resolve every un-tagged
# subscription to whichever of the two happened to be declared last. The
# fallback only ever has to name a plan whose GRANT is right -- and both
# subscriptions grant Pro -- so "pro" is the safe answer for either one.
_PLAN_BY_MODE: dict[str, str] = {"subscription": "pro", "payment": "dataset"}


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


# Why the last attempt to open a checkout failed, for /status. Stripe's own
# error CODE and PARAM only -- never its message. The code is an enumerated
# value ("resource_missing", "api_key_expired") and the param is a field name,
# so neither can carry a secret, whereas the message is free text that has
# historically quoted parts of the credential back at you. It is the difference
# between a reader learning "the Price id is wrong" and a reader learning
# anything about the key.
_last_error: str = ""


def last_error() -> str:
    """The code for the last failed checkout, or "" if the last one worked."""
    return _last_error


def reset_last_error() -> None:
    """Test helper, and the escape hatch after the configuration is fixed."""
    global _last_error
    _last_error = ""


# The last fulfilment that took money and granted nothing, as one readable
# line. Separate from `_last_error`, which is about CHECKOUT: a checkout that
# fails costs a click, and a fulfilment that fails costs a customer. This one
# is sticky by design -- it is cleared by an operator looking at it, never by
# the next event succeeding, because "the last event worked" says nothing about
# the one before it that did not.
_last_fulfilment_error: str = ""


def last_fulfilment_error() -> str:
    """The last payment this service could not turn into access, or ""."""
    return _last_fulfilment_error


def reset_fulfilment_error() -> None:
    """Clear it. For the operator who has dealt with it, and for tests."""
    global _last_fulfilment_error
    _last_fulfilment_error = ""


def _note_lost(reason: str, **fields: Any) -> None:
    """Money moved and nothing was granted. Make it impossible to miss.

    These outcomes -- an unknown plan, a session with no address, a renewal for
    an account whose Stripe ids were never stored -- are all answered 200 and
    RECORDED as handled, because Stripe retrying them for three days fixes none
    of them. That is the right call for the protocol and a terrible one for the
    operator: it used to mean a paid event vanished into a container log that
    rotates away, with no durable trace anywhere and nothing on /status.

    So the log line stays and a sticky field is set beside it, surfaced as
    `features.fulfilment_error`. It is deliberately not cleared by the next
    success: an operator has to look at it and clear it.
    """
    global _last_fulfilment_error
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if v)
    _last_fulfilment_error = f"{reason} ({detail})" if detail else reason
    log.error("stripe_fulfilment_LOST", reason=reason, **fields)


def _note_failure(exc: Exception) -> None:
    """Record the safe half of a Stripe error and nothing else.

    This exists because the first time these Price ids were wrong, the only
    place that said so was the container log -- the caller got a deliberately
    vague 502 (correct: a stranger must not be told the deployment's
    configuration) and the operator got the same 502 (useless: they are the
    one person who needs to know). Two enumerated fields close that gap
    without opening the other one.
    """
    global _last_error
    code = str(getattr(exc, "code", "") or "")
    param = str(getattr(exc, "param", "") or "")
    if not code:
        _last_error = type(exc).__name__
    else:
        _last_error = f"{code} ({param})" if param else code


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
        "pro_annual": s.stripe_price_pro_annual,
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

def create_portal_session(*, email: str, origin: str) -> dict[str, str]:
    """Open Stripe's own billing portal for this account.

    The other half of `create_checkout_session`, and its absence was the single
    largest commercial gap in the audit: a subscriber could start paying in
    thirty seconds and could not stop without emailing the operator. Cancelling
    a subscription should never require a human on the other end -- it is the
    kind of friction that turns one cancellation into a chargeback.

    Stripe hosts the portal, so cancelling, swapping a card, changing a plan and
    downloading invoices all happen on their page and arrive back here as the
    webhook events this service already handles. Nothing about the account is
    written here: this endpoint mints a URL and that is all it does.

    `origin` rather than `BASE_URL` for the return trip, same reasoning as
    checkout -- the browser that opened the portal is the one coming back.

    Raises rather than returning an error shape, because every failure here is
    a state the caller has to tell the reader about in different words: no
    Stripe (503), an account that has never paid (409), Stripe refusing (502).
    """
    stripe = _sdk()
    s = get_settings()
    if stripe is None or not s.stripe_secret_key:
        raise HTTPException(
            status_code=503, detail="Billing is not configured on this deployment."
        )

    customer = _customer_id_of(email)
    if not customer:
        # Never bought anything, so Stripe has no customer to show them. A 409
        # rather than a 404: the account exists, it simply has no billing to
        # manage, and the dashboard uses the distinction to decide whether to
        # show the button at all.
        raise HTTPException(
            status_code=409,
            detail=(
                "This account has no Stripe customer yet — nothing has been "
                "purchased on it."
            ),
        )

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer,
            return_url=f"{origin}/dashboard#billing",
        )
    except Exception as exc:  # noqa: BLE001 - the reader needs a reason, not a 500
        log.warning("stripe_portal_failed", email=email, error=_why(exc))
        raise HTTPException(
            status_code=502,
            detail="Stripe would not open the billing portal. Try again shortly.",
        ) from None

    url = str(_field(session, "url", "") or "")
    if not url:
        log.error("stripe_portal_no_url", email=email)
        raise HTTPException(
            status_code=502, detail="Stripe returned no portal URL."
        )
    log.info("stripe_portal_opened", email=email)
    return {"url": url}


def _customer_id_of(email: str) -> str:
    """The Stripe customer this account is attached to, or "".

    Read from `api_users` rather than searched for in Stripe by address: the id
    was stored when they paid, and a lookup by email would happily return a
    customer belonging to somebody who typed the same address into Stripe's own
    page. The stored id is the one this service actually granted against.
    """
    from src import accounts

    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == accounts.normalise_email(email))
        ).scalar_one_or_none()
        return str(row.stripe_customer_id or "") if row is not None else ""


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
    # Already owned? Then this is a charge for nothing. The buy buttons are
    # disabled once the flag is set, but that is a client-side guard on a
    # public endpoint -- a signed-out repeat buyer, or anyone posting here
    # directly, was charged $79.99 for a boolean that was already true. And it
    # confers literally nothing extra: the download streams the LIVE table, so
    # an existing buyer already receives every future snapshot.
    #
    # Only for the dataset. Pro is a subscription and buying it again is a
    # renewal, which is a legitimate thing to do.
    if wanted == "dataset" and email:
        owner = accounts.by_email(accounts.normalise_email(email))
        if owner is not None and owner.has_paid_download:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This account already owns the dataset — download it from "
                    "your dashboard. It is a one-time purchase and buying it "
                    "again grants nothing: your download always streams the "
                    "current data."
                ),
            )

    price = price_for(wanted)
    stripe = _sdk()
    s = get_settings()
    if stripe is None or not s.stripe_secret_key or not price:
        # `missing_config()` deliberately does not list STRIPE_PRICE_PRO_ANNUAL
        # -- a deployment selling only the monthly plan is a healthy one -- so
        # name it here, where an operator is looking at the one plan that
        # failed rather than at the whole feature.
        gaps = missing_config()
        if not price and f"STRIPE_PRICE_{wanted.upper()}" not in gaps:
            gaps.append(f"STRIPE_PRICE_{wanted.upper()}")
        log.error("stripe_checkout_unconfigured", plan=wanted, missing=gaps)
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
        # just bought actually is -- and lands there SIGNED IN.
        #
        # `{CHECKOUT_SESSION_ID}` is substituted by Stripe. The dashboard route
        # hands it straight back to Stripe to verify, so holding it proves the
        # holder completed that specific paid checkout: it is unguessable, it
        # is checked against the authority that issued it, and it is exchanged
        # for a session immediately and then dropped from the URL.
        #
        # This is what stops a first-time buyer being locked out of what they
        # paid for. Their key previously existed only in an email, and every
        # self-service route to it -- resend, magic link, re-register -- runs
        # through the same relay, while a webhook-created account has no
        # password to log in with. With mail down, the money was taken and the
        # product was unreachable. Now the return trip itself is the way in.
        #
        # Fulfilment still happens on the webhook and nowhere else. This grants
        # nothing; it only proves who is at the door.
        "success_url": (
            f"{origin}/dashboard?checkout=success"
            "&session_id={CHECKOUT_SESSION_ID}"
        ),
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
        _note_failure(exc)
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
    reset_last_error()
    log.info("stripe_checkout_opened", plan=wanted, email=address or "-")
    return {"url": url, "id": str(_field(session, "id", "") or ""), "plan": wanted}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _claim(event_id: str, kind: str) -> bool:
    """Take the event id BEFORE acting on it. True if this delivery won it.

    The insert is the lock. `event_id` is the primary key, so exactly one of
    two concurrent deliveries can insert it and the loser is told so by the
    database rather than by a check it has already raced past.

    This replaced a check-then-insert pair with a real gap between the two
    halves: both deliveries could read "not handled", both could grant, and the
    only trace was one warning after the money had already bought two periods.
    Claiming first closes the gap -- and `_release` below is what keeps the
    other half of the bargain, because an event claimed and then failed must
    become claimable again or Stripe's retry has nothing to retry into.
    """
    try:
        with session_scope() as session:
            session.add(StripeEvent(event_id=event_id, type=kind[:64]))
        return True
    except IntegrityError:
        return False
    except Exception as exc:  # noqa: BLE001
        # The table is unreachable. Act anyway: at-least-once delivery with no
        # idempotency is a risk of granting twice, while refusing to act is a
        # certainty of not granting at all, and only one of those has a buyer
        # on the other end of it.
        log.error("stripe_event_claim_failed", event_id=event_id, error=str(exc)[:200])
        return True


def _release(event_id: str) -> None:
    """Give an event id back after the handler failed.

    Without this, claiming first would turn every transient error -- a database
    blip mid-grant -- into a permanent one: the id would be taken, the grant
    would not have happened, and Stripe's redelivery would be waved through as
    a duplicate. The whole point of the ordering is that a failure stays
    retryable.
    """
    try:
        with session_scope() as session:
            row = session.get(StripeEvent, event_id)
            if row is not None:
                session.delete(row)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "stripe_event_release_failed", event_id=event_id, error=str(exc)[:200]
        )


def _stamp_terms(email: str) -> None:
    """Record that a buyer accepted the terms, for an account made by webhook.

    Only the API routes stamped this, so every Stripe-provisioned account held
    a NULL in the one column whose entire purpose is being able to say
    afterwards that the box was ticked -- which is to say, in exactly the
    accounts that paid money. Checkout shows the terms and Stripe records the
    agreement, so the acceptance is real; it was only ever the writing-down
    that was missing.
    """
    from src import auth

    try:
        auth.stamp_terms(email)
    except Exception as exc:  # noqa: BLE001 - the account and grant stand either way
        log.warning("stripe_terms_not_stamped", email=email, error=str(exc)[:120])


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
        account, plaintext = accounts.register(email)
    except accounts.EmailTaken:
        account = accounts.by_email(email)
        if account is None:  # pragma: no cover - taken and absent is impossible
            raise
        return account
    _stamp_terms(account.email)
    # Never raises; an unconfigured or failing relay is a log line. This is no
    # longer the buyer's ONLY way in -- the return trip from Stripe signs them
    # in against a verified session id -- so a dropped email now costs them a
    # convenience rather than the thing they paid for.
    # `plaintext` and not a lookup: this is the last point at which the key
    # exists anywhere, and it is the only reason the buyer's email can carry
    # one at all. An existing account (the branch above) gets no mail, because
    # there is nothing left to send.
    mailer.send_purchase_key(account.email, plaintext)
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
        if row is not None:
            return row.email
    # Nothing stored locally. Before giving up -- and a renewal that gives up is
    # a customer who keeps being billed and silently drops to free -- ASK
    # STRIPE who this customer is. The ids are written by `_attach_ids`, which
    # is deliberately non-fatal, so one transient database error at checkout
    # time is enough to leave them unset forever. This is the repair path for
    # exactly that, and it is also how a subscription created before this code
    # existed keeps renewing.
    return _email_from_stripe(customer=customer)


def _email_from_stripe(*, customer: str = "") -> str:
    """The email Stripe holds for a customer id, or "".

    Only ever a fallback, and it re-registers nothing: the address is matched
    against an account that already exists. Never raises -- an upstream that
    will not answer must not turn a renewal into a 500 that Stripe then retries
    for three days.
    """
    from src import accounts

    stripe = _sdk()
    s = get_settings()
    if stripe is None or not s.stripe_secret_key or not customer:
        return ""
    try:
        obj = stripe.Customer.retrieve(customer)
    except Exception as exc:  # noqa: BLE001 - a lookup that fails is just no answer
        log.warning("stripe_customer_lookup_failed", error=_why(exc))
        return ""
    address = accounts.normalise_email(str(_field(obj, "email", "") or ""))
    if not address or accounts.by_email(address) is None:
        return ""
    log.info("stripe_customer_recovered_from_upstream", customer=customer)
    return address


def period_days(plan: str) -> int | None:
    """How long one payment for `plan` buys, or None for "one ordinary period".

    The whole reason this function exists: the first invoice of a subscription
    is deliberately NOT granted on -- `checkout.session.completed` already
    fulfilled it, and granting on both buys one payment two periods -- so the
    checkout grant is the ONLY thing holding an annual subscriber on Pro until
    their next `subscription_cycle` invoice, which is twelve months away. An
    annual purchase granted the monthly period expires on day 32 having been
    paid for a year, and the account looks exactly like a lapsed monthly one.
    """
    if plan != "pro_annual":
        return None
    return get_settings().pro_annual_period_days


def _grant(email: str, plan: str, *, days: int | None = None) -> dict[str, Any]:
    from src import accounts

    account = accounts.apply_admin_action(
        email, _ACTION[plan], days=days if days is not None else period_days(plan)
    )
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
        _note_lost("unknown_plan", mode=_field(obj, "mode"), email=email)
        return {"status": "unknown_plan"}
    if not email:
        _note_lost("no_email", plan=plan)
        return {"status": "no_email", "plan": plan}

    _account_for(email)
    _attach_ids(
        email,
        customer=_id_of(_field(obj, "customer")),
        subscription=_id_of(_field(obj, "subscription")),
    )
    return _grant(email, plan)


def _invoice_period_days(invoice: Any) -> int | None:
    """The length in days of the period an invoice covers, or None.

    Read from `lines.data[0].period`, which Stripe sends as unix timestamps.
    None means "could not tell", and every caller treats that as the ordinary
    monthly period rather than guessing -- a wrong length here is either free
    access or a customer cut off early, and both are worse than the default.

    Clamped to a year and a bit at the top: a malformed or hostile period must
    not be able to grant a decade. The floor of 1 keeps a same-day period from
    granting nothing at all.
    """
    lines = _field(invoice, "lines") or {}
    data = _field(lines, "data") or []
    if not data:
        return None
    period = _field(data[0], "period") or {}
    start, end = _field(period, "start"), _field(period, "end")
    try:
        span = (int(end) - int(start)) // 86_400
    except (TypeError, ValueError):
        return None
    if span < 1:
        return None
    return min(span, 400)


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
        _note_lost("renewal_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}
    if subscription:
        _attach_ids(email, subscription=subscription)
    # Extended by the length of the period that was just paid for, read off the
    # invoice, so a yearly renewal buys a year and a monthly one buys a month
    # without this code -- or the account table -- knowing which plan it is.
    #
    # When the invoice carries no line periods, ASK Stripe about the
    # subscription rather than assuming a month. The bare fallback is correct
    # for the monthly plan and short-changes an annual one by eleven months,
    # and the two are indistinguishable from an invoice with no periods on it.
    # Money arrived, so whatever run of failures was building is over and any
    # pause it caused is lifted. Done here rather than on a schedule because a
    # settled payment is the only event that truthfully ends a dunning run.
    from src import accounts as _accounts

    _accounts.clear_payment_failures(email)

    days = _invoice_period_days(obj) or _subscription_period_days(subscription)
    if days is None:
        # Both readings failed. A month is the safe direction to be wrong in:
        # too little access is an email to an operator with a grant switch,
        # too much is a year given away. Loud, because it should not happen.
        log.warning("stripe_renewal_period_unknown", subscription=subscription or "-")
    return _grant(email, "pro", days=days)


def _subscription_id_of(email: str) -> str:
    """The Stripe subscription this account is currently on, or ""."""
    with session_scope() as session:
        row = session.execute(
            select(ApiUser).where(ApiUser.email == email)
        ).scalar_one_or_none()
        return str(row.stripe_subscription_id or "") if row is not None else ""


def _ends_in_the_future(subscription_obj: Any) -> bool:
    """Whether a cancelled subscription still has paid-for time on it.

    True only when Stripe says the period runs past now. A subscription
    cancelled the ordinary way -- at period end -- arrives here with that
    boundary already reached, so this is False and the revoke proceeds.
    """
    end = _field(subscription_obj, "current_period_end")
    try:
        when = dt.datetime.fromtimestamp(int(end), dt.UTC)
    except (TypeError, ValueError):
        return False
    return when > dt.datetime.now(dt.UTC)


def _forget_subscription(email: str) -> None:
    """Drop the stored subscription id, keep the customer id.

    A cancelled id must not match a later event; the customer is how the same
    person is recognised if they come back.
    """
    try:
        with session_scope() as session:
            row = session.execute(
                select(ApiUser).where(ApiUser.email == email)
            ).scalar_one_or_none()
            if row is not None:
                row.stripe_subscription_id = None
    except Exception as exc:  # noqa: BLE001 - the revoke already happened
        log.warning("stripe_sub_id_not_cleared", email=email, error=str(exc)[:200])


def _subscription_period_days(subscription: str) -> int | None:
    """The billing interval of a subscription, in days, straight from Stripe.

    The second opinion behind `_invoice_period_days`. Reads
    `items.data[0].price.recurring`, which is where the plan's own cadence
    lives, so a yearly subscription answers ~365 whatever its invoice looked
    like. Returns None rather than guessing, and never raises.
    """
    stripe = _sdk()
    s = get_settings()
    if stripe is None or not s.stripe_secret_key or not subscription:
        return None
    try:
        sub = stripe.Subscription.retrieve(subscription)
    except Exception as exc:  # noqa: BLE001 - no answer is not a wrong answer
        log.warning("stripe_subscription_lookup_failed", error=_why(exc))
        return None
    items = _field(_field(sub, "items") or {}, "data") or []
    if not items:
        return None
    recurring = _field(_field(items[0], "price") or {}, "recurring") or {}
    interval = str(_field(recurring, "interval", "") or "")
    try:
        count = int(_field(recurring, "interval_count", 1) or 1)
    except (TypeError, ValueError):
        count = 1
    per = {"day": 1, "week": 7, "month": 31, "year": 366}.get(interval)
    if per is None:
        return None
    return min(per * max(count, 1), 400)


def _cancel(obj: Any) -> dict[str, Any]:
    """A subscription ended: back to free, without touching a paid download.

    Refuses to revoke when the ended subscription is not the one this account
    is currently on. That case is not hypothetical: a Pro subscriber who buys
    the annual plan holds two subscriptions for as long as it takes somebody to
    cancel the monthly one, and `_attach_ids` has already moved the stored id
    to the new one. Cancelling the old subscription then delivers a `deleted`
    event whose subscription id nobody holds -- and the lookup used to fall
    through to the CUSTOMER id, find the same person, and revoke Pro from an
    account paying $490 a year. Matching on the id is the whole fix.
    """
    from src import accounts

    subscription = _id_of(_field(obj, "id"))
    customer = _id_of(_field(obj, "customer"))
    email = _email_for(customer=customer, subscription=subscription)
    if not email:
        log.info("stripe_cancel_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}

    current = _subscription_id_of(email)
    if subscription and current and current != subscription:
        # They are on a different, live subscription. This event is the tail of
        # an old one and must not touch anything.
        log.info(
            "stripe_cancel_superseded",
            email=email, ended=subscription, current=current,
        )
        return {
            "status": "superseded",
            "email": email,
            "ended": subscription,
            "current": current,
        }

    if _ends_in_the_future(obj):
        # Stripe cancelled at once rather than at period end, and there is time
        # left that was paid for. Revoking now confiscates it. Leave the expiry
        # where it is -- `effective_tier` drops them to free on the day it
        # passes, which is the day their money stops covering.
        log.info("stripe_cancel_leaves_paid_time", email=email)
        account = accounts.by_email(email)
        _forget_subscription(email)
        return {
            "status": "ends_at_period_end",
            "email": email,
            "tier": account.tier if account else "unknown",
        }

    account = accounts.apply_admin_action(email, "revoke_pro")
    _forget_subscription(email)
    log.info("stripe_subscription_cancelled", email=email, tier=account.tier)
    return {"status": "revoked", "plan": "pro", "email": email, "tier": account.tier}


# Every event this service acts on. Everything else gets a 200 and no action:
# Stripe sends a great many kinds, and answering anything but 2xx to one we
# simply do not care about would put the endpoint into a three-day retry loop.
def _reverse(obj: Any) -> dict[str, Any]:
    """Money went back: a refund, or a dispute opened. Take the access back.

    Previously unhandled, which meant a refund issued in the Stripe dashboard
    left `has_paid_download` true and a chargeback left a customer with full
    access and the money reversed -- with no code path anywhere that would ever
    notice. Both now revoke, and both are loud.

    Deliberately blunt: it revokes BOTH the subscription and the download
    rather than trying to work out which product the reversed charge was for.
    A charge object does not reliably carry the plan, and the failure modes are
    not symmetric -- wrongly leaving access after a chargeback is a stranger
    using a product they did not pay for, while wrongly removing it is one
    email to an operator who has a grant switch. A dispute WON can be put back
    with `/admin/grant-access`.
    """
    from src import accounts

    customer = _id_of(_field(obj, "customer"))
    email = _email_for(customer=customer)
    if not email:
        log.info("stripe_reversal_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}

    accounts.apply_admin_action(email, "revoke_pro")
    account = accounts.apply_admin_action(email, "revoke_download")
    _forget_subscription(email)
    # Not `_note_lost`: nothing was lost. This is money going back on purpose,
    # and the operator needs to know it happened, not to be alarmed by it.
    log.warning(
        "stripe_payment_REVERSED",
        email=email, customer=customer or "-",
        reason=str(_field(obj, "reason", "") or "refund"),
    )
    return {
        "status": "reversed",
        "email": email,
        "tier": account.tier,
        "has_paid_download": account.has_paid_download,
    }


# How many consecutive failed invoices before access stops.
#
# Three, because Stripe's own default retry schedule makes roughly that many
# attempts over about three weeks. Cutting off at one would punish a card that
# was declined for a bank's own reasons and worked the next morning; waiting
# for Stripe to give up and fire `customer.subscription.deleted` means three
# weeks of unpaid Pro access with the customer never told anything was wrong.
DUNNING_LIMIT = 3


def _price_id_of(subscription_obj: Any) -> str:
    """The Price this subscription is billed on, off the event object itself.

    No API call: `customer.subscription.updated` carries the full subscription,
    items included, so the price is already in hand. Reading it back from
    Stripe would be a round trip to learn what the payload just said.
    """
    items = _field(_field(subscription_obj, "items") or {}, "data") or []
    if not items:
        return ""
    return _id_of(_field(items[0], "price"))


def _plan_of_price(price_id: str) -> str:
    """"monthly" | "annual" | "" for a configured Price id.

    NOT `_plan_of`, which is a different question about a different object:
    that one reads the plan a checkout SESSION was for ("pro", "dataset"), and
    this one names the billing cadence behind a subscription's Price. Two
    functions with one name silently broke checkout fulfilment once already.

    Compared against the ids this deployment is configured with rather than
    inferred from the price's interval, so a price that is not ours answers ""
    instead of being filed under a plan we do not sell.
    """
    s = get_settings()
    if price_id and price_id == s.stripe_price_pro_annual:
        return "annual"
    if price_id and price_id == s.stripe_price_pro:
        return "monthly"
    return ""


def _period_end_of(subscription_obj: Any) -> dt.datetime | None:
    """`current_period_end` as a datetime, or None if it is unreadable.

    Recent API versions moved the field onto the subscription ITEM as well as
    the subscription, and some payloads carry it in only one of the two, so
    both are read before giving up.
    """
    end = _field(subscription_obj, "current_period_end")
    if end is None:
        items = _field(_field(subscription_obj, "items") or {}, "data") or []
        if items:
            end = _field(items[0], "current_period_end")
    try:
        return dt.datetime.fromtimestamp(int(end), dt.UTC).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _payment_failed(obj: Any) -> dict[str, Any]:
    """A renewal invoice did not get paid. Warn them, and eventually stop them.

    This was unhandled, and the gap was expensive in both directions: a
    customer whose card expired kept full Pro access for however long Stripe
    spends retrying -- about three weeks -- and was never told, so the first
    they knew of it was the subscription vanishing. Neither half is acceptable.

    Only `subscription_cycle` invoices count. The first invoice of a
    subscription failing means the checkout never completed, so there is
    nothing to withdraw and nobody has been granted anything.
    """
    from src import accounts, mailer

    reason = str(_field(obj, "billing_reason", "") or "")
    if reason not in ("subscription_cycle", "subscription_update"):
        return {"status": "ignored", "billing_reason": reason}

    customer = _id_of(_field(obj, "customer"))
    subscription = _subscription_of(obj)
    email = _email_for(customer=customer, subscription=subscription)
    if not email:
        _note_lost("payment_failed_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}

    attempt = accounts.record_payment_failure(email)
    remaining = max(DUNNING_LIMIT - attempt, 0)
    invoice_url = str(_field(obj, "hosted_invoice_url", "") or "")

    paused = False
    if attempt >= DUNNING_LIMIT:
        # The allowance drops to free AND the account is marked stopped. Two
        # separate writes because they answer different questions: what can
        # this key do now, and why.
        accounts.apply_admin_action(email, "revoke_pro")
        accounts.pause_api_access(email)
        paused = True
        log.warning(
            "stripe_dunning_exhausted",
            email=email, attempt=attempt, subscription=subscription or "-",
        )
    else:
        log.info("stripe_payment_failed", email=email, attempt=attempt)

    # Mailed last, and its result is not allowed to change the outcome: the
    # account state above is the part that must be right, and a relay that is
    # down must not make Stripe redeliver an event already acted on.
    sent = mailer.send_payment_failed(
        email, attempt=attempt, remaining=remaining, invoice_url=invoice_url
    )
    if not sent:
        log.warning("stripe_dunning_mail_unsent", email=email, attempt=attempt)

    return {
        "status": "paused" if paused else "warned",
        "email": email,
        "attempt": attempt,
        "remaining": remaining,
        "notified": sent,
    }


def _subscription_updated(obj: Any) -> dict[str, Any]:
    """The subscription changed in Stripe. Make this side match it.

    Upgrades, downgrades and a `cancel_at_period_end` flag all arrive here and
    all used to be dropped, so an account that moved from monthly to annual in
    Stripe kept a monthly expiry date here and lapsed eleven months early.

    Stripe is the authority on both facts this writes -- which price they are
    on, and when the paid-for period ends -- so both are ASSIGNED from the
    event rather than extended from what is already stored.

    A cancellation scheduled for period end is recorded and nothing else: the
    time has been paid for, `customer.subscription.deleted` fires when it runs
    out, and revoking now would confiscate days somebody bought.
    """
    from src import accounts

    subscription = _id_of(_field(obj, "id"))
    customer = _id_of(_field(obj, "customer"))
    email = _email_for(customer=customer, subscription=subscription)
    if not email:
        log.info("stripe_update_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}

    status = str(_field(obj, "status", "") or "")
    if status in ("incomplete_expired", "canceled"):
        # `_cancel` owns the ending of a subscription. Acting here as well
        # would revoke twice and race with it.
        return {"status": "ignored", "subscription_status": status}

    plan = _plan_of_price(_price_id_of(obj))
    period_end = _period_end_of(obj)
    changed: list[str] = []

    if subscription:
        _attach_ids(email, customer=customer, subscription=subscription)
    if plan:
        accounts.set_pro_plan(email, plan)
        changed.append("plan")
    if period_end is not None and accounts.set_pro_expiry(email, period_end):
        changed.append("expiry")

    cancel_at_end = bool(_field(obj, "cancel_at_period_end", False))
    log.info(
        "stripe_subscription_updated",
        email=email, plan=plan or "-", status=status or "-",
        cancel_at_period_end=cancel_at_end, changed=",".join(changed) or "-",
    )
    return {
        "status": "synced",
        "email": email,
        "plan": plan,
        "subscription_status": status,
        "cancel_at_period_end": cancel_at_end,
        "expires_at": period_end.isoformat() if period_end else None,
        "changed": changed,
    }


def _payment_action_required(obj: Any) -> dict[str, Any]:
    """The bank wants the cardholder to authenticate before it will pay.

    Nothing changes here: the money has not failed, it is waiting on a person.
    What this fixes is that the person was never told. A European customer
    whose bank demands 3-D Secure on a renewal gets a link they can act on
    rather than silence followed, three weeks later, by a cancelled
    subscription.

    Recorded with the invoice URL because that URL is the whole remedy, and
    without it in a log line there is no way to help somebody who writes in.
    """
    from src import mailer

    customer = _id_of(_field(obj, "customer"))
    subscription = _subscription_of(obj)
    email = _email_for(customer=customer, subscription=subscription)
    invoice_url = str(_field(obj, "hosted_invoice_url", "") or "")
    if not email:
        _note_lost("action_required_unknown_customer", customer=customer or "-")
        return {"status": "unknown_customer"}

    log.warning(
        "stripe_payment_action_required",
        email=email, subscription=subscription or "-", invoice_url=invoice_url or "-",
    )
    # Reuses the dunning mail with a zero-length run: no failure has been
    # counted, so `attempt` is 0 and the copy says nothing about attempts
    # remaining -- only that the payment needs them.
    sent = mailer.send_payment_failed(
        email, attempt=0, remaining=DUNNING_LIMIT, invoice_url=invoice_url
    )
    return {
        "status": "action_required",
        "email": email,
        "invoice_url": invoice_url,
        "notified": sent,
    }


def _checkout_expired(obj: Any) -> dict[str, Any]:
    """A Checkout Session was opened and never completed.

    No account exists yet and nothing was granted, so there is nothing to
    change -- this is here so an abandoned checkout leaves a trace instead of
    being indistinguishable from a session that was never opened. The plan and
    the address are logged where they are known, which together are the only
    two things worth knowing about an abandonment.
    """
    plan = str(_field(_field(obj, "metadata") or {}, "plan", "") or "")
    email = _email_of(obj)
    log.info(
        "stripe_checkout_expired",
        plan=plan or "-", email=email or "-", session=_id_of(_field(obj, "id")) or "-",
    )
    return {"status": "expired", "plan": plan, "email": email}


_HANDLERS = {
    "checkout.session.completed": _fulfil_checkout,
    # The same session, arriving late, for a payment method that does not
    # settle immediately. Without this a delayed-notification purchase either
    # never grants at all or (with the settled-check alone) grants before the
    # funds clear.
    "checkout.session.async_payment_succeeded": _fulfil_checkout,
    "invoice.payment_succeeded": _renew,
    "customer.subscription.deleted": _cancel,
    # A renewal that did not get paid. Counted, mailed, and after
    # `DUNNING_LIMIT` of them in a row the account stops -- see `_payment_failed`
    # for why silence for Stripe's whole three-week retry window was the wrong
    # answer in both directions.
    "invoice.payment_failed": _payment_failed,
    # The bank wants the cardholder to authenticate. Not a failure yet, and the
    # only useful thing to do is put the URL in front of the person.
    "invoice.payment_action_required": _payment_action_required,
    # Upgrades, downgrades, and a cancellation scheduled for period end. Stripe
    # is the authority on the plan and the period, so both are assigned from
    # the event rather than extended from what is stored here.
    "customer.subscription.updated": _subscription_updated,
    # Nothing to grant and nothing to take back; recorded so an abandoned
    # checkout is distinguishable from one that never happened.
    "checkout.session.expired": _checkout_expired,
    # Money going back. A refund is the operator's own doing and a dispute is
    # the buyer's, but both end with the charge reversed and access that should
    # not still be there. `dispute.created` rather than `dispute.closed`:
    # access should stop while the money is held, and a dispute won is one
    # `/admin/grant-access` call to put back.
    "charge.refunded": _reverse,
    "charge.dispute.created": _reverse,
}


def claim_checkout(session_id: str) -> str:
    """The address behind a completed Checkout Session, verified with Stripe.

    Returns "" for anything that is not a settled session this deployment can
    confirm -- a bad id, an unpaid one, an SDK that is not installed, or a
    Stripe that will not answer. Never raises: this runs on the buyer's return
    trip, and a page that 500s on the way back from a payment is worse than one
    that simply does not sign them in.

    The check is the whole security model. The id is unguessable, and it is
    handed back to the authority that minted it rather than trusted on sight,
    so the only way to hold one that verifies is to have completed that
    checkout. `payment_status` is re-read here rather than assumed from the
    redirect, because a redirect is a thing a browser does and a payment is a
    thing a bank does.
    """
    stripe = _sdk()
    s = get_settings()
    if stripe is None or not s.stripe_secret_key or not session_id:
        return ""
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception as exc:  # noqa: BLE001 - an unverifiable id is simply not one
        log.warning("stripe_claim_failed", error=_why(exc))
        return ""
    paid = str(_field(session, "payment_status", "") or "")
    if paid not in ("paid", "no_payment_required"):
        log.info("stripe_claim_unsettled", payment_status=paid)
        return ""
    email = _email_of(session)
    if email:
        log.info("stripe_claim_ok", email=email)
    return email


def _dispatch(event: Any) -> dict[str, Any]:
    """Act on one already-authenticated event, exactly once.

    Split out of `handle_event` so the simulator can reach it. Everything after
    the signature check lives here -- handler lookup, the idempotency check,
    the grant, and the record -- so a simulated purchase runs the SAME code a
    real one does rather than a second implementation that can drift from it.
    A simulator that does not exercise the real path tests the simulator.
    """
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

    # Claim, then act. A handler that raises hands the id back so Stripe's
    # redelivery can try again; one that returns -- for any outcome, including
    # the ones that could not grant -- keeps it, because none of those are
    # fixed by being sent the same event for three days.
    if not _claim(event_id, kind):
        log.info("stripe_event_duplicate", event_id=event_id, type=kind)
        return {"status": "duplicate", "event": event_id, "type": kind}

    obj = _field(_field(event, "data") or {}, "object") or {}
    try:
        result = handler(obj)
    except Exception:
        _release(event_id)
        raise
    result["event"] = event_id
    result["type"] = kind
    return result


# The event id prefix every simulated purchase carries. Deliberately unlike
# anything Stripe mints, so a row in `stripe_events` is always attributable to
# a person pressing the button rather than to money having moved.
SIMULATED_PREFIX = "evt_simulated_"


def simulate_checkout(*, email: str, plan: str) -> dict[str, Any]:
    """Run a purchase's AFTERMATH without a card, a Price, or a cent.

    Stripe rejects test cards against live keys -- that is by design and no
    amount of code changes it -- so the only honest way to watch what happens
    to a buyer on the live deployment is to synthesise the event Stripe would
    have sent and put it through the real handler. That is exactly what this
    does: it builds a `checkout.session.completed` in the shape Stripe sends
    and hands it to `_dispatch`, so the account is provisioned, the ids are
    attached, the grant is applied and the key email goes out through the same
    code a paid checkout uses.

    What it does NOT do is take money, so it also proves nothing about Stripe's
    half: signature verification, the Price amount, the receipt and the payout
    are all untested by this. It answers "what does my customer experience
    after the payment clears", and only that.

    Every simulated event carries `SIMULATED_PREFIX` and a fresh UUID, so it is
    both attributable in `stripe_events` and impossible to collide with a real
    delivery. The customer and subscription ids are prefixed the same way,
    which matters: attaching a plausible-looking `cus_...` to an account would
    make a later real webhook for that customer resolve to the wrong person.
    """
    import uuid

    from src import accounts

    wanted = (plan or "").strip().lower()
    if wanted not in PLANS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown plan {wanted!r}. One of: {', '.join(PLANS)}.",
        )
    address = accounts.normalise_email(email or "")
    if not address or not accounts.valid_email(address):
        raise HTTPException(
            status_code=422, detail="That does not look like an email address."
        )

    tag = uuid.uuid4().hex[:16]
    event = {
        "id": f"{SIMULATED_PREFIX}{tag}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_simulated_{tag}",
                "object": "checkout.session",
                "mode": _MODE[wanted],
                "payment_status": "paid",
                "customer": f"cus_simulated_{tag}",
                "subscription": (
                    f"sub_simulated_{tag}"
                    if _MODE[wanted] == "subscription"
                    else None
                ),
                "customer_details": {"email": address},
                "metadata": {"plan": wanted, "email": address},
            }
        },
    }
    # Loud on purpose. This grants real access on a live deployment, and the
    # log line is the only thing separating "we tested the flow" from "why does
    # this account have Pro".
    log.warning("stripe_purchase_SIMULATED", plan=wanted, email=address)
    result = _dispatch(event)
    result["simulated"] = True
    return result


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
    return _dispatch(event)
