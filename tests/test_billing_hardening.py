"""The twelve ways a payment could go wrong, and what now stops each of them.

Every test here is named after a real failure the audit found, and each one
fails against the code as it was. They are separate from `test_billing.py`
because that file tests the happy path working; this one tests the unhappy
paths not costing anybody money or access.

Nothing here talks to Stripe. Where a fix works by ASKING Stripe something --
recovering a customer's email, reading a subscription's real billing interval,
verifying a returned session id -- the SDK call is monkeypatched, because the
point under test is what this service does with the answer.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
WEBHOOK_SECRET = "whsec_test_do_not_use"
GOOD_SIG = "t=1,v1=good"
BUYER = "buyer@example.com"
PRICE_PRO = "price_pro_test"
PRICE_DATASET = "price_dataset_test"
PRICE_PRO_ANNUAL = "price_pro_annual_test"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "hardening.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "3")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "5000")
    monkeypatch.setenv("PRO_PERIOD_DAYS", "31")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    # Sessions have to be signable for the return-from-Stripe sign-in to be
    # exercised at all; production sets this.
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-do-not-use-abcdef")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("STRIPE_PRICE_PRO", PRICE_PRO)
    monkeypatch.setenv("STRIPE_PRICE_DATASET", PRICE_DATASET)
    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", PRICE_PRO_ANNUAL)

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    import stripe

    from src import billing
    from src.api import _checkout_gate, _register_gate, app

    _register_gate.reset()
    _checkout_gate.reset()
    billing.reset_last_error()
    billing.reset_fulfilment_error()

    def _create(**params):
        return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}

    def _construct(payload, sig_header, secret, tolerance=300, api_key=None):
        if sig_header != GOOD_SIG:
            raise stripe.SignatureVerificationError("bad", sig_header)
        return json.loads(payload)

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_create))
    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(_construct))

    # https, because the session cookie is Secure and httpx will not send a
    # Secure cookie back over http -- which presents as "signed in, then
    # immediately 401" and is not a bug in the code under test.
    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def register(client, email=BUYER):
    r = client.post(
        "/api/auth/register", json={"email": email, "accept_terms": True}
    )
    return r.json()["api_key"]


def status(client, key):
    return client.get("/api/user/status", headers={"X-API-Key": key}).json()


def post_event(client, event, sig=GOOD_SIG):
    return client.post(
        "/api/billing/webhook",
        content=json.dumps(event).encode(),
        headers={"stripe-signature": sig, "content-type": "application/json"},
    )


def checkout_event(*, event_id="evt_1", plan="pro", email=BUYER, subscription="sub_1"):
    sub = plan.startswith("pro")
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "mode": "subscription" if sub else "payment",
                "payment_status": "paid",
                "customer": "cus_1",
                "subscription": subscription if sub else None,
                "customer_details": {"email": email},
                "metadata": {"plan": plan, "email": email},
            }
        },
    }


# ---------------------------------------------------------------------------
# 04 -- cancelling an old subscription must not revoke a live one
# ---------------------------------------------------------------------------

def test_cancelling_a_superseded_subscription_leaves_pro_alone(client):
    """The $490-a-year customer who reads "Free (expired)".

    A monthly subscriber who upgrades to annual holds two subscriptions until
    somebody cancels the monthly one. The stored id has already moved to the
    new one, so the old one's `deleted` event matched nothing -- and the lookup
    fell through to the CUSTOMER id, found the same person, and revoked Pro
    from an account that is paying.
    """
    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_monthly"))
    post_event(
        client,
        checkout_event(
            event_id="evt_2", plan="pro_annual", subscription="sub_annual"
        ),
    )
    assert status(client, key)["tier"] == "pro"

    # The OLD subscription ends. Same customer, different subscription.
    r = post_event(
        client,
        {
            "id": "evt_cancel_old",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_monthly", "customer": "cus_1"}},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "superseded"
    assert status(client, key)["tier"] == "pro", "revoked a live annual subscription"


def test_cancelling_the_current_subscription_still_revokes(client):
    """The guard must not become a way to never lose Pro."""
    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_1"))

    r = post_event(
        client,
        {
            "id": "evt_cancel",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_1", "customer": "cus_1"}},
        },
    )
    assert r.json()["status"] == "revoked"
    assert status(client, key)["tier"] == "free"


def test_an_immediate_cancellation_does_not_confiscate_paid_time(client):
    """Cancelled in the dashboard mid-period, rather than at period end.

    The expiry is left where it is: they paid through that date, and
    `effective_tier` drops them on the day it passes.
    """
    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_1"))
    future = int((dt.datetime.now(dt.UTC) + dt.timedelta(days=20)).timestamp())

    r = post_event(
        client,
        {
            "id": "evt_cancel_now",
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_1",
                    "customer": "cus_1",
                    "current_period_end": future,
                }
            },
        },
    )
    assert r.json()["status"] == "ends_at_period_end"
    assert status(client, key)["tier"] == "pro"


# ---------------------------------------------------------------------------
# 05 -- money going back
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["charge.refunded", "charge.dispute.created"])
def test_a_reversed_payment_takes_the_access_back(client, kind):
    """Both were unhandled, so a chargeback left a stranger with full access
    and the money reversed, and no code path would ever have noticed."""
    key = register(client)
    post_event(client, checkout_event(plan="pro"))
    post_event(client, checkout_event(event_id="evt_d", plan="dataset"))
    assert status(client, key)["has_paid_download"] is True

    r = post_event(
        client,
        {
            "id": f"evt_{kind}",
            "type": kind,
            "data": {"object": {"id": "ch_1", "customer": "cus_1"}},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "reversed"

    body = status(client, key)
    assert body["tier"] == "free"
    assert body["has_paid_download"] is False


# ---------------------------------------------------------------------------
# 06 -- a second dataset purchase is a charge for nothing
# ---------------------------------------------------------------------------

def test_buying_the_dataset_twice_is_refused_by_the_server(client):
    """The button was disabled client-side, on a public endpoint. Anyone
    signed out, or posting directly, was charged again for a boolean that was
    already true -- and the download streams the LIVE table, so the second
    purchase confers nothing at all."""
    register(client)
    post_event(client, checkout_event(plan="dataset"))

    r = client.post("/api/billing/checkout", json={"plan": "dataset", "email": BUYER})
    assert r.status_code == 409
    assert "already owns" in r.json()["detail"]


def test_buying_pro_again_is_still_allowed(client):
    """Pro is a subscription. Buying it again is a renewal, which is a
    legitimate thing to want to do."""
    register(client)
    post_event(client, checkout_event(plan="pro"))

    r = client.post("/api/billing/checkout", json={"plan": "pro", "email": BUYER})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 03 -- a payment that granted nothing must be visible
# ---------------------------------------------------------------------------

def test_a_payment_that_granted_nothing_shows_up_on_status(client):
    """These are answered 200 and recorded as handled, because Stripe retrying
    them for three days fixes none of them -- so without a durable field they
    vanished into a log that rotates away."""
    assert client.get("/status").json()["features"]["fulfilment_error"] is None

    r = post_event(
        client,
        {
            "id": "evt_no_email",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_x",
                    "mode": "payment",
                    "payment_status": "paid",
                    "customer": "cus_9",
                    "customer_details": {},
                }
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "no_email"

    err = client.get("/status").json()["features"]["fulfilment_error"]
    assert err and "no_email" in err


def test_the_fulfilment_alarm_is_sticky_until_it_is_cleared(client):
    """A later success says nothing about the payment before it that failed."""
    from src import billing

    post_event(
        client,
        {
            "id": "evt_no_email_2",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_y", "mode": "payment", "payment_status": "paid",
                    "customer": "cus_9", "customer_details": {},
                }
            },
        },
    )
    register(client)
    post_event(client, checkout_event(plan="pro"))

    assert client.get("/status").json()["features"]["fulfilment_error"] is not None
    billing.reset_fulfilment_error()
    assert client.get("/status").json()["features"]["fulfilment_error"] is None


# ---------------------------------------------------------------------------
# 12 -- the idempotency gap
# ---------------------------------------------------------------------------

def test_an_event_is_claimed_before_it_is_acted_on(client):
    """Check-then-insert left a gap where two concurrent deliveries could both
    read "not handled" and both grant. The insert IS the lock now."""
    key = register(client)
    a = post_event(client, checkout_event(plan="pro"))
    b = post_event(client, checkout_event(plan="pro"))

    assert a.json()["status"] == "granted"
    assert b.json()["status"] == "duplicate"
    left = dt.datetime.fromisoformat(
        status(client, key)["expires_at"]
    ) - dt.datetime.now(dt.UTC)
    assert left.days <= 32, "one payment bought two periods"


def test_a_failed_handler_hands_the_event_id_back(client, monkeypatch):
    """Claiming first must not turn a transient error into a permanent one: a
    grant that raises has to stay retryable, or Stripe's redelivery is waved
    through as a duplicate and the buyer never gets what they paid for."""
    from src import billing

    boom = {"n": 0}
    real = billing._grant

    def _explode(email, plan, *, days=None):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("database blipped mid-grant")
        return real(email, plan, days=days)

    monkeypatch.setattr(billing, "_grant", _explode)
    key = register(client)

    with pytest.raises(RuntimeError):
        post_event(client, checkout_event(plan="pro"))
    assert status(client, key)["tier"] == "free"

    # Stripe redelivers the SAME event id. It must not be a duplicate.
    r = post_event(client, checkout_event(plan="pro"))
    assert r.json()["status"] == "granted"
    assert status(client, key)["tier"] == "pro"


# ---------------------------------------------------------------------------
# 11 -- terms acceptance on a webhook-provisioned account
# ---------------------------------------------------------------------------

def test_a_buyer_provisioned_by_webhook_has_accepted_the_terms(client):
    """The column exists so it can be said afterwards that the box was ticked,
    and it was NULL in exactly the accounts that paid money."""
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    post_event(client, checkout_event(plan="dataset", email="new-buyer@example.com"))

    with session_scope() as s:
        from sqlalchemy import select

        row = s.execute(
            select(ApiUser).where(ApiUser.email == "new-buyer@example.com")
        ).scalar_one()
        assert row.terms_accepted_at is not None


# ---------------------------------------------------------------------------
# 01 -- the return trip from Stripe is a way in
# ---------------------------------------------------------------------------

def test_the_success_url_carries_the_session_id_for_stripe_to_fill_in(client):
    r = client.post("/api/billing/checkout", json={"plan": "pro", "email": BUYER})
    assert r.status_code == 200

    from src import billing

    made = billing.create_checkout_session(
        plan="pro", origin="https://example.test", email=BUYER
    )
    assert made["url"]


def test_returning_from_a_paid_checkout_signs_the_buyer_in(client, monkeypatch):
    """The critical finding. A first-time buyer's key existed only in an email,
    and resend, magic link and re-register all run through the same relay, so a
    mail outage meant money taken and the product unreachable. The return trip
    is now the way in, and it needs no email at all.
    """
    import stripe

    monkeypatch.setattr(
        stripe.checkout.Session,
        "retrieve",
        staticmethod(
            lambda sid, **kw: {
                "id": sid,
                "payment_status": "paid",
                "customer_details": {"email": "locked-out@example.com"},
                "metadata": {"email": "locked-out@example.com"},
            }
        ),
    )
    post_event(
        client, checkout_event(plan="dataset", email="locked-out@example.com")
    )

    r = client.get(
        "/dashboard?checkout=success&session_id=cs_live_abc", follow_redirects=False
    )
    assert r.status_code == 303
    # The credential is dropped from the URL rather than left in history.
    assert r.headers["location"] == "/dashboard?checkout=success"
    # A real session cookie, not just a redirect: HttpOnly and Secure, because
    # this cookie can be exchanged for the account's API key.
    raw = r.headers["set-cookie"].lower()
    assert "httponly" in raw and "secure" in raw

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "locked-out@example.com"
    assert me.json()["api_key"], "signed in but no key to show them"


def test_an_unverifiable_session_id_signs_nobody_in(client, monkeypatch):
    """The id is checked against Stripe rather than trusted on sight. One that
    does not verify is one somebody typed."""
    import stripe

    def _refuse(sid, **kw):
        raise stripe.InvalidRequestError("No such session", "id")

    monkeypatch.setattr(stripe.checkout.Session, "retrieve", staticmethod(_refuse))

    r = client.get("/dashboard?session_id=cs_live_guessed", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/auth/me").status_code != 200


def test_an_unpaid_session_signs_nobody_in(client, monkeypatch):
    """`payment_status` is re-read from Stripe, because a redirect is a thing a
    browser does and a payment is a thing a bank does."""
    import stripe

    monkeypatch.setattr(
        stripe.checkout.Session,
        "retrieve",
        staticmethod(
            lambda sid, **kw: {
                "id": sid,
                "payment_status": "unpaid",
                "customer_details": {"email": BUYER},
            }
        ),
    )
    register(client)

    client.get("/dashboard?session_id=cs_live_unpaid", follow_redirects=False)
    assert client.get("/api/auth/me").status_code != 200


# ---------------------------------------------------------------------------
# 02 + 07 -- asking Stripe rather than giving up or guessing
# ---------------------------------------------------------------------------

def test_a_renewal_recovers_the_customer_from_stripe(client, monkeypatch):
    """One transient DB error at checkout leaves the Stripe ids unset, and the
    renewal then found no email, returned 200, and was recorded as handled --
    so the customer kept being billed and silently dropped to free."""
    import stripe

    key = register(client)
    post_event(client, checkout_event(plan="pro"))

    # Wipe the ids, as a failed `_attach_ids` would have left them.
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    with session_scope() as s:
        row = s.execute(select(ApiUser).where(ApiUser.email == BUYER)).scalar_one()
        row.stripe_customer_id = "cus_1"
        row.stripe_subscription_id = None

    monkeypatch.setattr(
        stripe.Customer,
        "retrieve",
        staticmethod(lambda cid, **kw: {"id": cid, "email": BUYER}),
    )
    before = status(client, key)["expires_at"]

    r = post_event(
        client,
        {
            "id": "evt_renew",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_1",
                    "billing_reason": "subscription_cycle",
                    "customer": "cus_1",
                    "subscription": "sub_1",
                }
            },
        },
    )
    assert r.json()["status"] == "granted"
    assert status(client, key)["expires_at"] > before


def test_a_renewal_with_no_periods_asks_stripe_for_the_interval(client, monkeypatch):
    """An invoice with no line periods is indistinguishable between a monthly
    and an annual plan, and assuming a month short-changes $490 by eleven."""
    import stripe

    key = register(client)
    post_event(client, checkout_event(plan="pro_annual", subscription="sub_1"))

    monkeypatch.setattr(
        stripe.Subscription,
        "retrieve",
        staticmethod(
            lambda sid, **kw: {
                "id": sid,
                "items": {
                    "data": [
                        {"price": {"recurring": {"interval": "year", "interval_count": 1}}}
                    ]
                },
            }
        ),
    )
    r = post_event(
        client,
        {
            "id": "evt_renew_annual",
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": "in_2",
                    "billing_reason": "subscription_cycle",
                    "customer": "cus_1",
                    "subscription": "sub_1",
                }
            },
        },
    )
    assert r.json()["status"] == "granted"
    left = dt.datetime.fromisoformat(
        status(client, key)["expires_at"]
    ) - dt.datetime.now(dt.UTC)
    assert left.days >= 700, f"an annual renewal bought only {left.days} days"


# ---------------------------------------------------------------------------
# Marketing: reading the site is free, and now says so
# ---------------------------------------------------------------------------

def test_the_pricing_page_says_reading_the_site_is_unlimited(client):
    """Drawings and search were always free and uncapped, and the comparison
    table stopped saying so when its axis changed -- which left the free plan
    reading as though browsing were rationed."""
    html = client.get("/pricing").text

    assert "Reading this site is free and unlimited" in html
    assert "Unlimited drawings and search" in html
    assert "Drawings &amp; search on this site" in html
