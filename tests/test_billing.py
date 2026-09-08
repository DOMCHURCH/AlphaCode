"""Stripe Checkout, end to end, with Stripe replaced by a fake.

Nothing here talks to Stripe. Two seams are monkeypatched -- the call that opens
a Checkout Session and the call that verifies a webhook signature -- and every
event is a plain dict handed to the real handler, which is the shape the SDK
delivers anyway.

What is asserted is mostly the ways a payment system loses money or gives away
access, because those are the failures nobody notices until somebody complains:

* a redelivered webhook granting a second month for one payment;
* an unsettled payment granting before the funds clear;
* a renewal invoice granting nothing, so month two is free;
* the first invoice of a subscription granting a second time on top of the
  checkout that already did;
* a buyer whose address has no account being taken for money and given nothing;
* an unsigned body reaching the grant switch.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
WEBHOOK_SECRET = "whsec_test_do_not_use"
PRICE_PRO = "price_1UDDNJPlpgbONcUzKiSeEp4U"
PRICE_DATASET = "price_1UDDMePlpgbONcUzFWRiwAhU"
GOOD_SIG = "t=1,v1=this-one-verifies"

BUYER = "buyer@example.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient over a throwaway on-disk database with Stripe configured.

    On disk, not `:memory:`, for the same reason `test_accounts.py` is: the API
    opens its own connections through `session_scope`, and an in-memory
    database is private to the connection that made it.
    """
    db = tmp_path / "billing.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "3")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "5000")
    monkeypatch.setenv("PRO_PERIOD_DAYS", "31")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("STRIPE_PRICE_PRO", PRICE_PRO)
    monkeypatch.setenv("STRIPE_PRICE_DATASET", PRICE_DATASET)

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import _checkout_gate, _register_gate, app
    from src.dataset import reset_count_cache

    _register_gate.reset()
    _checkout_gate.reset()
    reset_count_cache()

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture()
def stripe_calls(monkeypatch):
    """Replace the two calls that leave the process. Returns the sessions made.

    `construct_event` accepts exactly one signature and rejects everything
    else, which is the only property of the real one this code depends on.
    """
    import stripe

    made: list[dict] = []

    def _create(**params):
        made.append(params)
        return {
            "id": f"cs_test_{len(made)}",
            "url": f"https://checkout.stripe.com/c/pay/cs_test_{len(made)}",
        }

    def _construct(payload, sig_header, secret, tolerance=300, api_key=None):
        assert secret == WEBHOOK_SECRET
        if sig_header != GOOD_SIG:
            raise stripe.SignatureVerificationError(
                "No signatures found matching the expected signature",
                sig_header,
            )
        return json.loads(payload)

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_create))
    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(_construct))
    return made


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def register(client, email=BUYER):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def status(client, key):
    r = client.get("/api/user/status", headers={"X-API-Key": key})
    assert r.status_code == 200, r.text
    return r.json()


def post_event(client, event, sig=GOOD_SIG):
    """Deliver one event as Stripe would: a raw body plus a signature header."""
    return client.post(
        "/api/billing/webhook",
        content=json.dumps(event).encode(),
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )


def checkout_event(
    *, event_id="evt_1", plan="pro", email=BUYER, payment_status="paid",
    mode=None, metadata=True, customer="cus_1", subscription="sub_1",
    kind="checkout.session.completed",
):
    session = {
        "id": "cs_test_1",
        "object": "checkout.session",
        "mode": mode or ("subscription" if plan == "pro" else "payment"),
        "payment_status": payment_status,
        "customer": customer,
        "subscription": subscription if plan == "pro" else None,
        "customer_details": {"email": email},
    }
    if metadata:
        session["metadata"] = {"plan": plan, "email": email}
    return {"id": event_id, "type": kind, "data": {"object": session}}


def invoice_event(
    *, event_id="evt_inv", reason="subscription_cycle", customer="cus_1",
    subscription="sub_1",
):
    return {
        "id": event_id,
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_1",
                "billing_reason": reason,
                "customer": customer,
                "subscription": subscription,
            }
        },
    }


def cancel_event(*, event_id="evt_cancel", subscription="sub_1", customer="cus_1"):
    return {
        "id": event_id,
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": subscription, "customer": customer}},
    }


# ---------------------------------------------------------------------------
# Opening a checkout
# ---------------------------------------------------------------------------

def test_checkout_returns_a_url_and_never_grants_anything(client, stripe_calls):
    """The URL is handed back; the purchase is not fulfilled until the webhook.

    Fulfilling on the return URL is the classic hole: it is a plain GET that a
    stranger can visit without paying and a buyer can close before it loads.
    """
    key = register(client)
    r = client.post("/api/billing/checkout", json={"plan": "pro", "email": BUYER})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.com/")

    assert status(client, key)["tier"] == "free"


def test_the_session_carries_the_right_mode_price_and_return_urls(client, stripe_calls):
    client.post("/api/billing/checkout", json={"plan": "pro", "email": BUYER})
    client.post("/api/billing/checkout", json={"plan": "dataset", "email": BUYER})

    pro, dataset = stripe_calls
    assert pro["mode"] == "subscription"
    assert pro["line_items"] == [{"price": PRICE_PRO, "quantity": 1}]
    # A subscription bought as a one-off payment charges once and never renews;
    # a file sold as a subscription bills every month for one download.
    assert dataset["mode"] == "payment"
    assert dataset["line_items"] == [{"price": PRICE_DATASET, "quantity": 1}]

    # Read back by the webhook, so the plan is explicit rather than inferred.
    assert pro["metadata"]["plan"] == "pro"
    assert dataset["metadata"]["plan"] == "dataset"
    # The buyer's own origin, not settings.BASE_URL: paying on a preview
    # deployment must not land you back on production.
    assert pro["success_url"].startswith("http://testserver/dashboard")
    assert pro["cancel_url"] == "http://testserver/pricing"
    # Locks the address, so the account granted is the account that started it.
    assert pro["customer_email"] == BUYER


def test_an_unknown_plan_is_422_and_reaches_stripe_not_at_all(client, stripe_calls):
    r = client.post("/api/billing/checkout", json={"plan": "enterprise"})
    assert r.status_code == 422
    assert "enterprise" in r.json()["detail"]
    assert stripe_calls == []


def test_checkout_is_503_when_stripe_is_not_configured(client, stripe_calls, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    get_settings.cache_clear()
    r = client.post("/api/billing/checkout", json={"plan": "pro"})
    assert r.status_code == 503
    assert stripe_calls == []


# ---------------------------------------------------------------------------
# Fulfilment
# ---------------------------------------------------------------------------

def test_a_paid_dataset_checkout_unlocks_the_download(client, stripe_calls):
    key = register(client)
    assert status(client, key)["has_paid_download"] is False

    r = post_event(client, checkout_event(plan="dataset", subscription=None))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "granted"

    body = status(client, key)
    assert body["has_paid_download"] is True
    # A file purchase must not also hand over the monthly allowance.
    assert body["tier"] == "free"


def test_a_paid_pro_checkout_grants_pro_with_a_period_on_it(client, stripe_calls):
    key = register(client)
    r = post_event(client, checkout_event(plan="pro"))
    assert r.status_code == 200, r.text

    body = status(client, key)
    assert body["tier"] == "pro"
    assert body["expires_at"] is not None
    # 31 days, per PRO_PERIOD_DAYS. Never NULL: a NULL expiry reads as "comped,
    # never expires", so a subscription written that way is free access
    # forever.
    left = dt.datetime.fromisoformat(body["expires_at"]) - dt.datetime.now(dt.UTC)
    assert 29 <= left.days <= 31


def test_a_redelivered_event_does_not_buy_a_second_period(client, stripe_calls):
    """Stripe delivers at least once. One payment must be one period."""
    key = register(client)
    event = checkout_event(plan="pro")

    first = post_event(client, event)
    assert first.json()["status"] == "granted"
    expiry = status(client, key)["expires_at"]

    second = post_event(client, event)
    # 200, not an error: a duplicate is the expected traffic, and anything else
    # puts a real payment into a three-day retry loop.
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert status(client, key)["expires_at"] == expiry


def test_an_unsettled_checkout_grants_nothing_until_the_money_clears(
    client, stripe_calls
):
    key = register(client)
    r = post_event(
        client, checkout_event(plan="pro", payment_status="unpaid")
    )
    assert r.status_code == 200
    assert r.json()["status"] == "unsettled"
    assert status(client, key)["tier"] == "free"

    # The delayed method settles: a different event id, and now it grants.
    later = post_event(
        client,
        checkout_event(
            event_id="evt_async",
            plan="pro",
            kind="checkout.session.async_payment_succeeded",
        ),
    )
    assert later.json()["status"] == "granted"
    assert status(client, key)["tier"] == "pro"


def test_a_payment_link_with_no_metadata_still_fulfils(client, stripe_calls):
    """A link made by hand in the Stripe dashboard carries no metadata of ours.

    With two products on two Checkout modes, the mode is enough to say which
    was bought -- so the operator can sell from a Payment Link today without
    this service being changed.
    """
    key = register(client)
    r = post_event(
        client,
        checkout_event(plan="dataset", metadata=False, subscription=None),
    )
    assert r.json()["status"] == "granted"
    assert status(client, key)["has_paid_download"] is True


def test_paying_from_an_unknown_address_creates_the_account_and_mails_the_key(
    client, stripe_calls, monkeypatch
):
    """Never take money and no-op. A first purchase has no account yet."""
    sent: list[tuple[str, str]] = []
    from src import mailer

    monkeypatch.setattr(
        mailer, "send_api_key", lambda email, key: sent.append((email, key)) or True
    )

    r = post_event(client, checkout_event(plan="pro", email="stranger@example.com"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "granted"

    from src import accounts

    account = accounts.by_email("stranger@example.com")
    assert account is not None
    assert account.tier == "pro"
    # The key is mailed, because the buyer has no other way to learn it.
    assert sent == [("stranger@example.com", account.api_key)]


def test_a_second_purchase_from_a_new_address_does_not_fail_on_the_account(
    client, stripe_calls
):
    """`register` raises on an address it already has; that must not be fatal."""
    first = post_event(
        client, checkout_event(event_id="evt_a", plan="dataset", email="new@example.com",
                               subscription=None)
    )
    assert first.json()["status"] == "granted"
    second = post_event(
        client, checkout_event(event_id="evt_b", plan="pro", email="new@example.com")
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "granted"

    from src import accounts

    account = accounts.by_email("new@example.com")
    assert account.tier == "pro" and account.has_paid_download is True


# ---------------------------------------------------------------------------
# Renewals and cancellation
# ---------------------------------------------------------------------------

def test_a_renewal_invoice_extends_pro(client, stripe_calls):
    """Month two is a payment too. Without this it buys nothing."""
    key = register(client)
    post_event(client, checkout_event(plan="pro"))
    first = status(client, key)["expires_at"]

    r = post_event(client, invoice_event())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "granted"
    assert status(client, key)["expires_at"] > first


def test_the_first_invoice_of_a_subscription_does_not_grant_twice(client, stripe_calls):
    """`subscription_create` arrives alongside the checkout that already paid."""
    key = register(client)
    post_event(client, checkout_event(plan="pro"))
    expiry = status(client, key)["expires_at"]

    r = post_event(client, invoice_event(reason="subscription_create"))
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert status(client, key)["expires_at"] == expiry


def test_a_cancelled_subscription_returns_the_account_to_free(client, stripe_calls):
    key = register(client)
    post_event(client, checkout_event(plan="pro"))
    assert status(client, key)["tier"] == "pro"

    r = post_event(client, cancel_event())
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"

    body = status(client, key)
    assert body["tier"] == "free"
    # Backdated, never nulled: NULL means "never expires", which would UPGRADE
    # the account being cancelled.
    assert body["expires_at"] is not None


def test_cancelling_pro_leaves_a_paid_dataset_alone(client, stripe_calls):
    """Two purchases, two switches. Ending one must not undo the other."""
    key = register(client)
    post_event(
        client, checkout_event(event_id="evt_d", plan="dataset", subscription=None)
    )
    post_event(client, checkout_event(event_id="evt_p", plan="pro"))
    post_event(client, cancel_event())

    body = status(client, key)
    assert body["tier"] == "free"
    assert body["has_paid_download"] is True


def test_an_event_about_a_customer_we_have_never_seen_is_answered_200(
    client, stripe_calls
):
    """Somebody else's charge on the same Stripe account. A retry cannot fix it."""
    register(client)
    r = post_event(client, cancel_event(subscription="sub_other", customer="cus_other"))
    assert r.status_code == 200
    assert r.json()["status"] == "unknown_customer"

    renewal = post_event(
        client,
        invoice_event(event_id="evt_x", customer="cus_other", subscription="sub_other"),
    )
    assert renewal.status_code == 200
    assert renewal.json()["status"] == "unknown_customer"


# ---------------------------------------------------------------------------
# The webhook as a door
# ---------------------------------------------------------------------------

def test_a_bad_signature_is_400_and_grants_nothing(client, stripe_calls):
    """The only unauthenticated write in the app, and the signature is the lock."""
    key = register(client)
    r = post_event(client, checkout_event(plan="pro"), sig="t=1,v1=forged")
    assert r.status_code == 400
    assert status(client, key)["tier"] == "free"


def test_an_event_kind_we_do_not_handle_is_200(client, stripe_calls):
    """Stripe sends a great many. A 4xx on one would start a three-day retry."""
    r = post_event(
        client,
        {"id": "evt_ping", "type": "payment_intent.created", "data": {"object": {}}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_the_webhook_fails_closed_without_a_signing_secret(
    client, stripe_calls, monkeypatch
):
    """Unset means dead, not open -- an unverified body is an open grant switch."""
    from src.config.settings import get_settings

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    key = register(client)
    r = post_event(client, checkout_event(plan="pro"))
    assert r.status_code == 503
    assert status(client, key)["tier"] == "free"


# ---------------------------------------------------------------------------
# What the operator can see
# ---------------------------------------------------------------------------

def test_status_reports_billing_only_when_the_whole_round_trip_is_configured(
    client, monkeypatch
):
    """A key with no webhook secret can take a payment it never hears about.

    That is the one state worth telling apart from "off", so the flag is false
    for it -- the same lesson the demo flag taught.
    """
    from src.config.settings import get_settings

    assert client.get("/status").json()["features"]["billing"] is True

    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    assert client.get("/status").json()["features"]["billing"] is False


def test_the_admin_config_report_names_each_stripe_variable(client):
    r = client.get("/admin.json", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r.status_code == 200, r.text
    names = {row["name"] for row in r.json()["config"]}
    assert {
        "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PRICE_PRO", "STRIPE_PRICE_DATASET",
    } <= names


def test_pricing_redirects_to_the_plans_on_the_home_page(client):
    """The cancel_url of every checkout. It must not be a 404."""
    r = client.get("/pricing", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/#pricing"
