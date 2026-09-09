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
PRICE_DATASET = "price_1UDmJUBLo93QIBx5vBs6mzaU"
PRICE_PRO_ANNUAL = "price_1UDmFsBLo93QIBx5ZCJF3ZPt"
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
    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", PRICE_PRO_ANNUAL)

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import _checkout_gate, _register_gate, app
    from src.billing import reset_last_error
    from src.dataset import reset_count_cache

    _register_gate.reset()
    _checkout_gate.reset()
    reset_count_cache()
    # Why the last checkout failed is module state, so one test's Stripe
    # failure would otherwise be reported on /status by the next.
    reset_last_error()

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
        # Both Pro plans are subscriptions; only the file is a one-off
        # payment. `startswith` rather than `== "pro"` so adding the annual
        # plan did not quietly make it a `payment` with no subscription on it.
        "mode": mode or ("subscription" if plan.startswith("pro") else "payment"),
        "payment_status": payment_status,
        "customer": customer,
        "subscription": subscription if plan.startswith("pro") else None,
        "customer_details": {"email": email},
    }
    if metadata:
        session["metadata"] = {"plan": plan, "email": email}
    return {"id": event_id, "type": kind, "data": {"object": session}}


def invoice_event(
    *, event_id="evt_inv", reason="subscription_cycle", customer="cus_1",
    subscription="sub_1", period_days=None,
):
    obj = {
        "id": "in_1",
        "billing_reason": reason,
        "customer": customer,
        "subscription": subscription,
    }
    if period_days is not None:
        start = int(dt.datetime.now(dt.UTC).timestamp())
        obj["lines"] = {
            "data": [{"period": {"start": start, "end": start + period_days * 86_400}}]
        }
    return {
        "id": event_id,
        "type": "invoice.payment_succeeded",
        "data": {"object": obj},
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


def test_the_annual_plan_is_a_subscription_on_its_own_price(client, stripe_calls):
    """`pro_annual` is a THIRD plan, not a flag on the second one.

    It shares Pro's mode and Pro's grant and nothing else: its own Price id,
    its own metadata, and a checkout that must not quietly bill monthly.
    """
    r = client.post(
        "/api/billing/checkout", json={"plan": "pro_annual", "email": BUYER}
    )
    assert r.status_code == 200, r.text
    assert r.json()["plan"] == "pro_annual"

    (call,) = stripe_calls
    assert call["mode"] == "subscription"
    assert call["line_items"] == [{"price": PRICE_PRO_ANNUAL, "quantity": 1}]
    assert call["metadata"]["plan"] == "pro_annual"
    # Nothing is granted by opening a checkout. Fulfilment is the webhook's.
    assert status(client, register(client))["tier"] == "free"


def test_a_paid_annual_checkout_grants_pro_for_a_YEAR(client, stripe_calls):
    """One Pro tier, but a year of it -- and the year is the whole test.

    The first invoice of a subscription is deliberately not granted on, and the
    next `subscription_cycle` invoice for an annual plan is twelve months away,
    so this checkout grant is the ONLY thing holding the account on Pro until
    then. Granted the monthly period, a $490 buyer reads "Free (expired)" on
    day 32 -- indistinguishable from a lapsed monthly subscriber.
    """
    key = register(client)
    r = post_event(client, checkout_event(plan="pro_annual"))
    assert r.status_code == 200, r.text

    body = status(client, key)
    assert body["tier"] == "pro"
    left = dt.datetime.fromisoformat(body["expires_at"]) - dt.datetime.now(dt.UTC)
    assert left.days >= 360, f"annual bought only {left.days} days"


def test_an_annual_renewal_extends_by_a_year_not_a_month(client, stripe_calls):
    """The renewal reads the period off the invoice rather than assuming one.

    Same code path serves both plans: a monthly renewal carries a ~31-day
    period and buys a month, a yearly one carries a ~365-day period and buys a
    year, and neither the account table nor this handler has to know which plan
    the subscription is on.
    """
    key = register(client)
    post_event(client, checkout_event(plan="pro_annual"))
    monthly_expiry = status(client, key)["expires_at"]

    r = post_event(
        client, invoice_event(event_id="evt_annual_renewal", period_days=365)
    )
    assert r.status_code == 200, r.text

    body = status(client, key)
    left = dt.datetime.fromisoformat(body["expires_at"]) - dt.datetime.now(dt.UTC)
    assert left.days >= 700, f"a year of renewal added only {left.days} days"
    assert body["expires_at"] > monthly_expiry


def test_a_renewal_with_no_line_periods_still_buys_the_ordinary_month(
    client, stripe_calls
):
    """The fallback, which is the old behaviour and is right for monthly.

    "Could not tell" must mean the ordinary period, never a guess: a wrong
    length here is either free access or a customer cut off early.
    """
    key = register(client)
    post_event(client, checkout_event(plan="pro"))
    r = post_event(client, invoice_event(event_id="evt_bare_renewal"))
    assert r.status_code == 200, r.text

    left = dt.datetime.fromisoformat(
        status(client, key)["expires_at"]
    ) - dt.datetime.now(dt.UTC)
    assert 60 <= left.days <= 62, left.days


def test_an_untagged_subscription_event_still_grants_pro(client, stripe_calls):
    """The mode fallback, with two subscription plans instead of one.

    `_PLAN_BY_MODE` used to be `{v: k for k, v in _MODE.items()}`, which with
    two `subscription` plans resolves every un-tagged subscription to whichever
    was declared last. It is written out now, and either answer grants Pro --
    so a Payment Link made by hand in the Stripe dashboard still fulfils.
    """
    key = register(client)
    event = checkout_event(plan="pro_annual", metadata=False)
    r = post_event(client, event)
    assert r.status_code == 200, r.text

    assert status(client, key)["tier"] == "pro"


def test_an_unconfigured_annual_price_is_503_not_a_broken_checkout(
    client, stripe_calls, monkeypatch
):
    """A deployment selling only the monthly plan is a HEALTHY deployment.

    STRIPE_PRICE_PRO_ANNUAL is deliberately absent from `missing_config()`, so
    leaving it unset must not flip the billing flag on /status -- but the plan
    that needs it has to refuse rather than open a checkout with no Price.
    """
    from src.config.settings import get_settings

    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", "")
    get_settings.cache_clear()

    assert client.get("/status").json()["features"]["billing"] is True
    r = client.post("/api/billing/checkout", json={"plan": "pro_annual"})
    assert r.status_code == 503
    assert stripe_calls == []
    # The monthly plan is untouched by the annual one being unset.
    assert client.post("/api/billing/checkout", json={"plan": "pro"}).status_code == 200


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
    """Never take money and no-op. A first purchase has no account yet.

    `send_purchase_key`, not `send_api_key`: somebody who has just paid used to
    receive the key-RECOVERY template, which mentions no purchase and reads
    like a reminder they did not ask for.
    """
    sent: list[tuple[str, str]] = []
    from src import mailer

    monkeypatch.setattr(
        mailer,
        "send_purchase_key",
        lambda email, key: sent.append((email, key)) or True,
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


def test_pricing_is_a_real_page_and_answers_the_dataset_vs_api_question(client):
    """The cancel_url of every checkout, so it is the page somebody lands on at
    the moment they have decided NOT to buy -- which is exactly when "what is
    the difference between these two?" is the question to answer.

    It was a 307 to `/#pricing` and is now a page, which is what the redirect's
    own comment said would happen. The 307 was deliberately never made
    permanent so this change would not have to fight a year of browser cache.
    """
    r = client.get("/pricing", follow_redirects=False)
    assert r.status_code == 200
    html = r.text

    # The comparison, and the four rows that carry the actual differentiation.
    assert "Static (as of download date)" in html
    assert "Live (updates daily)" in html
    assert "One-time analysis" in html
    assert "Ongoing automation" in html
    # And the FAQ, whose first question is the one the page exists for.
    assert "difference between the dataset and the API" in html


def test_every_plan_is_buyable_from_the_pricing_page(client):
    """Four cards, three of them a checkout. A pricing page whose buttons do
    not buy anything is a brochure."""
    html = client.get("/pricing").text

    for plan in ("dataset", "pro", "pro_annual"):
        assert f'data-plan="{plan}"' in html, plan
    # The fallback for a blocked script reaches somewhere a purchase finishes.
    assert 'href="/dashboard#billing" data-plan=' in html


def test_the_dataset_page_states_its_snapshot_date_and_that_it_is_static(client):
    """The one property that decides whether the dataset is the right purchase.

    It used to be nowhere a buyer could read before paying, which is how
    somebody buys a photograph believing they bought a window.
    """
    html = client.get("/dataset").text

    assert "static snapshot" in html.lower()
    assert "will never update" in html
    assert "For live data, use the API" in html
    # Size and rows, so a large download is a decision rather than a surprise.
    assert "File size" in html and "Rows" in html


# ---------------------------------------------------------------------------
# When Stripe refuses
# ---------------------------------------------------------------------------

def test_a_refused_checkout_is_502_and_tells_the_operator_only_the_code(
    client, monkeypatch
):
    """All four variables can be set while the Price ids are the other mode.

    That state reads `billing: true` and fails every purchase, and the first
    time it happened the only place that said so was a container log: the
    buyer got a deliberately vague 502 (right -- a stranger must not be told
    the deployment's configuration) and so did the operator (useless). So the
    code and the field Stripe named are surfaced, and its free-text message,
    which has historically quoted credentials back, is not.
    """
    import stripe

    def _refuse(**params):
        raise stripe.InvalidRequestError(
            "No such price: 'price_1UDDMePlpgbONcUzFWRiwAhU'",
            "line_items[0][price]",
            code="resource_missing",
            http_status=400,
        )

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_refuse))

    r = client.post("/api/billing/checkout", json={"plan": "dataset"})
    assert r.status_code == 502
    # The caller is told nothing about the configuration.
    assert "price" not in r.json()["detail"]

    features = client.get("/status").json()["features"]
    # The flag stays true -- every variable IS set -- and the error says why
    # that is not the same as working.
    assert features["billing"] is True
    assert features["billing_error"] == "resource_missing (line_items[0][price])"
    # Enumerated fields only. Stripe's message never reaches the page.
    assert "No such price" not in features["billing_error"]


def test_a_working_checkout_clears_the_last_error(client, stripe_calls, monkeypatch):
    """A stale complaint next to a working endpoint is worse than none."""
    from src import billing

    monkeypatch.setattr(billing, "_last_error", "resource_missing (price)")
    assert client.get("/status").json()["features"]["billing_error"] is not None

    r = client.post("/api/billing/checkout", json={"plan": "pro"})
    assert r.status_code == 200
    assert client.get("/status").json()["features"]["billing_error"] is None


def test_the_index_lists_the_checkout_endpoint(client):
    """/api.json is written by hand, so a new route is only there if it is added."""
    listed = " ".join(client.get("/api.json").json()["endpoints"])
    assert "POST /api/billing/checkout" in listed
    assert "/pricing" in listed
    assert "/dataset" in listed


# ---------------------------------------------------------------------------
# The buy buttons, which used to go to the login page
# ---------------------------------------------------------------------------

def _navigates_to_login(js: str) -> bool:
    """Does this script ever NAVIGATE to /login?

    A bare `"/login" in js` is the wrong test and fails honestly: the word
    appears in prose comments and in a perfectly good "Sign in" link on the
    Account tab. What must not exist is the browser being SENT there -- which
    is always some form of assignment to `location`.
    """
    import re

    return bool(re.search(r"location(?:\.href|\.assign\(|\s*=)[^;\n]*/login", js))


def test_the_paid_pricing_cards_start_a_checkout_not_a_login(client):
    """"Buy the data" linked to /login, signed in or not.

    It lost the click, told the reader nothing, and looked exactly like being
    signed out when they were not -- the report that started this. The paid
    cards now carry the plan, and their href is a fallback that still reaches a
    place a purchase can be finished.
    """
    home = client.get("/").text

    assert 'data-plan="pro"' in home
    assert 'data-plan="dataset"' in home
    # The fallback for a blocked script is the billing panel, NOT /login: the
    # login page is the dead end being fixed, so it must not be the fallback.
    assert '<a class="plan-cta" href="/dashboard#billing" data-plan=' in home
    # The free card is a signup and correctly still goes to sign-in.
    assert '<a class="plan-cta" href="/login">Get a key</a>' in home


def test_the_home_script_posts_a_checkout_and_never_redirects_to_login(client):
    js = client.get("/static/home.js").text

    assert '"/api/billing/checkout"' in js
    assert 'credentials: "same-origin"' in js
    assert not _navigates_to_login(js), "a failure path still sends the reader to /login"


def test_the_dashboard_buttons_open_a_checkout_and_report_failure_in_place(client):
    js = client.get("/static/dashboard.js").text

    assert 'startCheckout("pro"' in js
    assert 'startCheckout("dataset"' in js
    assert '"/api/billing/checkout"' in js
    # A 429 from the global gate must read as "wait", not as a dead button.
    assert "r.status === 429" in js
    assert not _navigates_to_login(js), "a failure path still sends the reader to /login"


def test_the_dashboard_opens_the_billing_tab_when_asked(client):
    """/dashboard#billing is the no-script fallback, so the hash must win over
    whichever tab localStorage remembers from a previous visit."""
    js = client.get("/static/dashboard.js").text
    assert "location.hash" in js
    assert 'TABS.indexOf(hash) !== -1' in js


def test_a_checkout_started_from_the_home_page_needs_no_account(client, stripe_calls):
    """Most people who click "Go Pro" have never registered.

    Stripe collects the address on its own page and the webhook provisions it,
    so an anonymous checkout must be allowed to open rather than demanding a
    signup first.
    """
    r = client.post("/api/billing/checkout", json={"plan": "pro"})
    assert r.status_code == 200, r.text
    # Nothing is prefilled, so Stripe asks -- and must not be sent an empty
    # customer_email, which it rejects.
    assert "customer_email" not in stripe_calls[0]
    assert stripe_calls[0]["metadata"]["plan"] == "pro"


# ---------------------------------------------------------------------------
# The simulator: a purchase's aftermath, without a purchase
# ---------------------------------------------------------------------------

SIM = "/admin/simulate-purchase"
SIM_HEAD = {"X-Admin-Secret": ADMIN_SECRET}


def test_simulating_a_purchase_grants_through_the_real_handler(client):
    """The point of the endpoint is that it is NOT a second implementation.

    A simulator with its own grant logic tests the simulator. This one builds
    the event Stripe would have sent and puts it through the same dispatch a
    signed webhook reaches, so what it proves about fulfilment is true of a
    real payment too.
    """
    key = register(client)
    r = client.post(SIM, headers=SIM_HEAD, json={"email": BUYER, "plan": "pro_annual"})
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["simulated"] is True
    assert body["fulfilment"]["status"] == "granted"
    assert body["fulfilment"]["event"].startswith("evt_simulated_")

    # The account really is Pro, for a real year.
    s = status(client, key)
    assert s["tier"] == "pro"
    left = dt.datetime.fromisoformat(s["expires_at"]) - dt.datetime.now(dt.UTC)
    assert left.days >= 360


def test_a_simulated_purchase_reaches_stripe_not_at_all(client, stripe_calls):
    """No money, no Price, no Session. If this ever calls Stripe it is not a
    simulation, it is a purchase somebody did not expect to make."""
    client.post(SIM, headers=SIM_HEAD, json={"email": BUYER, "plan": "dataset"})
    assert stripe_calls == []


def test_the_simulator_is_the_grant_switch_and_is_gated_like_it(client):
    """It hands out real access, so an unauthenticated caller must not reach
    it -- this is /admin/grant-access wearing a different shape."""
    r = client.post(SIM, json={"email": BUYER, "plan": "pro"})
    assert r.status_code == 403
    assert status(client, register(client))["tier"] == "free"


def test_a_simulated_event_cannot_collide_with_a_real_delivery(client):
    """Two simulations of the same plan for the same person both apply.

    They carry different ids by construction -- a fixed id would be seen as a
    duplicate the second time and silently do nothing, which would make the
    endpoint look broken the moment somebody pressed it twice.
    """
    key = register(client)
    a = client.post(SIM, headers=SIM_HEAD, json={"email": BUYER, "plan": "pro"})
    b = client.post(SIM, headers=SIM_HEAD, json={"email": BUYER, "plan": "pro"})

    assert a.json()["fulfilment"]["event"] != b.json()["fulfilment"]["event"]
    assert b.json()["fulfilment"]["status"] == "granted"
    # Two periods, because two grants -- the same as two real payments.
    left = dt.datetime.fromisoformat(
        status(client, key)["expires_at"]
    ) - dt.datetime.now(dt.UTC)
    assert left.days >= 60


def test_pro_and_the_dataset_do_not_overwrite_each_other(client):
    """The question a buyer asks: if I have Pro and then buy the CSV, or the
    other way round, does one cancel the other? Two columns, never one.
    """
    key = register(client)
    client.post(SIM, headers=SIM_HEAD, json={"email": BUYER, "plan": "pro_annual"})
    client.post(SIM, headers=SIM_HEAD, json={"email": BUYER, "plan": "dataset"})

    s = status(client, key)
    assert s["tier"] == "pro" and s["has_paid_download"] is True

    # And losing Pro does not take away a file they paid for.
    client.post(
        "/admin/grant-access",
        headers=SIM_HEAD,
        json={"email": BUYER, "action": "revoke_pro"},
    )
    s = status(client, key)
    assert s["tier"] == "free"
    assert s["has_paid_download"] is True, "a cancelled subscription ate a paid CSV"


def test_the_simulator_says_when_a_buyer_would_be_locked_out(client):
    """The finding this endpoint exists to make visible.

    A first-time buyer's key exists only in an email, and every self-service
    route to it runs through the same relay -- so with mail unconfigured, a
    successful grant and an unreachable product look identical in the status
    payload. The endpoint says which one happened, in words.
    """
    r = client.post(
        SIM, headers=SIM_HEAD, json={"email": "brand-new@example.com", "plan": "pro"}
    )
    body = r.json()

    assert body["account_existed_before"] is False
    assert body["email_configured"] is False
    assert "locked out" in body["buyer_can_reach_it"]
