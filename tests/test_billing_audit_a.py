"""Audit batch A: Stripe events that must not touch the wrong subscription, and
chargebacks that must find the customer the way Stripe really sends them.

Nothing here talks to Stripe. Where the code ASKS Stripe something (a dispute
carries a charge id, not a customer) the SDK call is monkeypatched and the
test checks what this service does with the answer.
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
    db = tmp_path / "audit_a.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "3")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "5000")
    monkeypatch.setenv("PRO_PERIOD_DAYS", "31")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
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

    def _construct(payload, sig_header, secret, tolerance=300, api_key=None):
        if sig_header != GOOD_SIG:
            raise stripe.SignatureVerificationError("bad", sig_header)
        return json.loads(payload)

    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(_construct))

    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def register(client, email=BUYER):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
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


def _ts(days: int) -> int:
    return int((dt.datetime.now(dt.UTC) + dt.timedelta(days=days)).timestamp())


def _on_annual_after_monthly(client):
    """Monthly first, then an annual bought through a second checkout."""
    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_monthly"))
    post_event(
        client,
        checkout_event(event_id="evt_2", plan="pro_annual", subscription="sub_annual"),
    )
    return key


# ---------------------------------------------------------------------------
# F4 -- an older subscription's events must not overwrite the live one
# ---------------------------------------------------------------------------

def test_an_update_for_a_superseded_subscription_changes_nothing(client):
    key = _on_annual_after_monthly(client)
    before = status(client, key)
    assert before["tier"] == "pro"

    r = post_event(
        client,
        {
            "id": "evt_old_updated",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_monthly",
                    "customer": "cus_1",
                    "status": "active",
                    "items": {"data": [{"price": {"id": PRICE_PRO}}]},
                    "current_period_end": _ts(5),
                }
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "superseded"
    assert r.json()["current"] == "sub_annual"
    after = status(client, key)
    assert after["tier"] == "pro"
    assert after["expires_at"] == before["expires_at"], "the old subscription moved the expiry"

    # The stored id is still the annual one, so ITS events still work and its
    # cancellation still revokes.
    from src import billing

    assert billing._subscription_id_of(BUYER) == "sub_annual"


def test_an_update_for_the_current_subscription_still_applies(client):
    """The guard must not become a way to never sync a plan."""
    key = _on_annual_after_monthly(client)
    new_end = _ts(400)

    r = post_event(
        client,
        {
            "id": "evt_cur_updated",
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_annual",
                    "customer": "cus_1",
                    "status": "active",
                    "items": {"data": [{"price": {"id": PRICE_PRO_ANNUAL}}]},
                    "current_period_end": new_end,
                }
            },
        },
    )
    assert r.json()["status"] == "synced"
    expires = dt.datetime.fromisoformat(status(client, key)["expires_at"])
    assert abs(expires.timestamp() - new_end) < 5


def _renewal(event_id, subscription):
    return {
        "id": event_id,
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_" + event_id,
                "billing_reason": "subscription_cycle",
                "customer": "cus_1",
                "subscription": subscription,
                "lines": {
                    "data": [
                        {
                            "period": {
                                "start": _ts(0),
                                "end": _ts(31),
                            }
                        }
                    ]
                },
            }
        },
    }


def test_a_renewal_of_a_superseded_subscription_does_not_touch_the_live_one(client):
    key = _on_annual_after_monthly(client)
    before = status(client, key)

    r = post_event(client, _renewal("evt_old_renew", "sub_monthly"))
    assert r.status_code == 200
    assert r.json()["status"] == "superseded"
    assert r.json()["renewed"] == "sub_monthly"
    after = status(client, key)
    assert after["tier"] == before["tier"]
    assert after["expires_at"] == before["expires_at"], "an old renewal shortened the annual"

    from src import billing

    assert billing._subscription_id_of(BUYER) == "sub_annual"


def test_a_renewal_of_the_current_subscription_still_extends(client):
    key = _on_annual_after_monthly(client)
    before = status(client, key)["expires_at"]

    r = post_event(client, _renewal("evt_cur_renew", "sub_annual"))
    assert r.json()["status"] == "granted"
    assert status(client, key)["expires_at"] > before


# ---------------------------------------------------------------------------
# F3 -- a chargeback must find its customer through the charge
# ---------------------------------------------------------------------------

def dispute_event(*, event_id="evt_dp", charge="ch_1", payment_intent="pi_1"):
    """A `charge.dispute.created` shaped like Stripe's Dispute object.

    A Dispute has `charge` and `payment_intent` and NO `customer`. The test that
    stood here before handed the handler a `customer` field no real dispute
    carries, so it passed while a real chargeback was logged as an unknown
    customer and left the buyer with access.
    """
    return {
        "id": event_id,
        "type": "charge.dispute.created",
        "data": {
            "object": {
                "id": "dp_1",
                "object": "dispute",
                "amount": 49000,
                "currency": "usd",
                "charge": charge,
                "payment_intent": payment_intent,
                "reason": "fraudulent",
                "status": "needs_response",
            }
        },
    }


class FakeLookup:
    """Stands in for the bounded Stripe client `billing._lookup_client()` returns.

    Records what was asked and answers from `charges` / `intents`, or raises
    `error` when set. Nothing here touches the network.
    """

    def __init__(self, charges=None, intents=None, error=None):
        from types import SimpleNamespace

        self.asked: list[tuple[str, str]] = []
        self._charges = charges or {}
        self._intents = intents or {}
        self._error = error
        self.v1 = SimpleNamespace(
            charges=SimpleNamespace(retrieve=self._charge),
            payment_intents=SimpleNamespace(retrieve=self._intent),
        )

    def _charge(self, charge_id, **kw):
        self.asked.append(("charge", charge_id))
        if self._error:
            raise self._error
        return {"id": charge_id, "customer": self._charges.get(charge_id)}

    def _intent(self, intent_id, **kw):
        self.asked.append(("payment_intent", intent_id))
        if self._error:
            raise self._error
        return {"id": intent_id, "customer": self._intents.get(intent_id)}


def use_lookup(monkeypatch, fake):
    from src import billing

    monkeypatch.setattr(billing, "_lookup_client", lambda: fake)
    return fake


def _paid_up(client):
    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_1"))
    post_event(client, checkout_event(event_id="evt_d", plan="dataset"))
    assert status(client, key)["has_paid_download"] is True
    assert status(client, key)["tier"] == "pro"
    return key


def test_the_dispute_fixture_has_no_top_level_customer():
    assert "customer" not in dispute_event()["data"]["object"]


def test_a_dispute_resolves_its_customer_through_the_charge(client, monkeypatch):
    fake = use_lookup(monkeypatch, FakeLookup(charges={"ch_1": "cus_1"}))
    key = _paid_up(client)

    r = post_event(client, dispute_event())
    assert r.status_code == 200
    assert r.json()["status"] == "reversed"
    assert fake.asked == [("charge", "ch_1")]
    body = status(client, key)
    assert body["tier"] == "free"
    assert body["has_paid_download"] is False


def test_a_dispute_with_no_charge_falls_back_to_the_payment_intent(client, monkeypatch):
    fake = use_lookup(monkeypatch, FakeLookup(intents={"pi_1": "cus_1"}))
    key = _paid_up(client)

    r = post_event(client, dispute_event(charge=None))
    assert r.json()["status"] == "reversed"
    assert fake.asked == [("payment_intent", "pi_1")], "no charge id, so no charge lookup"
    assert status(client, key)["has_paid_download"] is False


def test_a_dispute_with_an_expanded_charge_needs_no_lookup(client, monkeypatch):
    fake = use_lookup(monkeypatch, FakeLookup())
    key = _paid_up(client)
    event = dispute_event()
    event["data"]["object"]["charge"] = {"id": "ch_1", "customer": "cus_1"}

    r = post_event(client, event)
    assert r.json()["status"] == "reversed"
    assert fake.asked == [], "the charge was already expanded"
    assert status(client, key)["tier"] == "free"


def test_a_dispute_for_a_stranger_changes_nobody(client, monkeypatch):
    import stripe

    use_lookup(monkeypatch, FakeLookup(charges={"ch_1": "cus_somebody_else"}))
    # Stripe is asked about a customer it knows but this service never saw; the
    # fallback must not invent an email for it.
    monkeypatch.setattr(
        stripe.Customer, "retrieve",
        staticmethod(lambda cid, **kw: {"id": cid, "email": "nobody@example.com"}),
    )
    key = _paid_up(client)

    r = post_event(client, dispute_event())
    assert r.json()["status"] == "unknown_customer"
    assert status(client, key)["tier"] == "pro"


def test_a_stripe_error_on_a_dispute_is_retried_not_swallowed(client, monkeypatch):
    """Answering 200 on a lookup failure would end the retries and leave the
    buyer holding access for money that has been taken back."""
    import stripe

    use_lookup(monkeypatch, FakeLookup(error=stripe.APIConnectionError("stripe unreachable")))
    key = _paid_up(client)

    with pytest.raises(stripe.APIConnectionError):
        post_event(client, dispute_event())
    assert status(client, key)["has_paid_download"] is True

    # Stripe redelivers the SAME event id; it must not be waved through as a
    # duplicate of the attempt that failed.
    use_lookup(monkeypatch, FakeLookup(charges={"ch_1": "cus_1"}))
    r = post_event(client, dispute_event())
    assert r.json()["status"] == "reversed"
    assert status(client, key)["has_paid_download"] is False


def test_a_refund_still_reads_the_customer_off_the_charge(client):
    """`charge.refunded` carries the Charge itself, which has `customer`."""
    key = _paid_up(client)
    r = post_event(
        client,
        {
            "id": "evt_refund",
            "type": "charge.refunded",
            "data": {"object": {"id": "ch_1", "object": "charge", "customer": "cus_1"}},
        },
    )
    assert r.json()["status"] == "reversed"
    assert status(client, key)["tier"] == "free"


# ---------------------------------------------------------------------------
# F3 -- the lookups made while a webhook waits have an explicit timeout
# ---------------------------------------------------------------------------

def test_the_lookup_client_has_an_explicit_timeout_and_no_retries(monkeypatch):
    import stripe

    from src import billing
    from src.config.settings import get_settings

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_API_VERSION", "")
    get_settings.cache_clear()

    captured = {}

    class Recorder:
        def __init__(self, key, **kw):
            captured["key"] = key
            captured.update(kw)

    monkeypatch.setattr(stripe, "StripeClient", Recorder)
    client = billing._lookup_client()

    assert isinstance(client, Recorder)
    assert captured["key"] == "sk_test_do_not_use"
    assert captured["max_network_retries"] == 0
    assert captured["stripe_version"] is None
    http = captured["http_client"]
    assert isinstance(http, stripe.HTTPXClient)
    assert http._timeout == billing._LOOKUP_TIMEOUT_S == 10.0
    get_settings.cache_clear()


def test_the_lookup_client_is_a_real_stripe_client_with_the_charge_and_intent_services(monkeypatch):
    from src import billing
    from src.config.settings import get_settings

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    get_settings.cache_clear()
    client = billing._lookup_client()
    assert callable(client.v1.charges.retrieve)
    assert callable(client.v1.payment_intents.retrieve)
    get_settings.cache_clear()


def test_there_is_no_lookup_client_without_a_key(monkeypatch):
    from src import billing
    from src.config.settings import get_settings

    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    get_settings.cache_clear()
    assert billing._lookup_client() is None
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Which subscription an invoice belongs to: both API shapes
#
# Recent Stripe API versions moved the reference off the invoice's top level and
# under `parent.subscription_details.subscription`. `_renew` feeds the answer to
# the supersession guard, and an empty answer means "no conflict", so a shape the
# extractor does not read would silently switch the guard off.
# ---------------------------------------------------------------------------

SHAPES = ["legacy", "parent"]


def _invoice(subscription, shape):
    invoice = {
        "id": "in_x",
        "billing_reason": "subscription_cycle",
        "customer": "cus_1",
        "lines": {"data": [{"period": {"start": _ts(0), "end": _ts(31)}}]},
    }
    if shape == "legacy":
        invoice["subscription"] = subscription
    else:
        invoice["subscription"] = None       # absent or null in the new shape
        invoice["parent"] = {
            "type": "subscription_details",
            "subscription_details": {"subscription": subscription},
        }
    return invoice


def _renewal_event(event_id, subscription, shape):
    return {"id": event_id, "type": "invoice.payment_succeeded",
            "data": {"object": _invoice(subscription, shape)}}


@pytest.mark.parametrize("shape", SHAPES)
def test_the_subscription_is_read_from_either_invoice_shape(shape):
    from src import billing

    assert billing._subscription_of(_invoice("sub_9", shape)) == "sub_9"


def test_the_extractor_edge_cases():
    from src import billing

    # The legacy field wins when both are present; an expanded object is read by id.
    both = _invoice("sub_legacy", "legacy")
    both["parent"] = {"subscription_details": {"subscription": "sub_parent"}}
    assert billing._subscription_of(both) == "sub_legacy"
    assert billing._subscription_of({"subscription": {"id": "sub_expanded"}}) == "sub_expanded"
    nested = {"parent": {"subscription_details": {"subscription": {"id": "sub_p"}}}}
    assert billing._subscription_of(nested) == "sub_p"
    # A one-off invoice has neither.
    assert billing._subscription_of({"id": "in_1", "parent": None}) == ""
    assert billing._subscription_of({"id": "in_1"}) == ""


@pytest.mark.parametrize("shape", SHAPES)
def test_a_renewal_of_a_superseded_subscription_is_refused_in_either_shape(client, shape):
    key = _on_annual_after_monthly(client)
    before = status(client, key)

    r = post_event(client, _renewal_event("evt_old_renew", "sub_monthly", shape))
    assert r.json()["status"] == "superseded"
    assert r.json()["renewed"] == "sub_monthly"
    assert status(client, key)["expires_at"] == before["expires_at"]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_renewal_of_the_current_subscription_extends_in_either_shape(client, shape):
    key = _on_annual_after_monthly(client)
    before = status(client, key)["expires_at"]

    r = post_event(client, _renewal_event("evt_cur_renew", "sub_annual", shape))
    assert r.json()["status"] == "granted"
    assert status(client, key)["expires_at"] > before


# ---------------------------------------------------------------------------
# Out of order: an upgrade where the new subscription's own events arrive BEFORE
# checkout.session.completed has moved the stored id
#
# Those events answer 200 "superseded" and Stripe will not redeliver them. The
# checkout event then grants the tier and expiry from the SESSION's plan (not from
# the subscription), so the account ends up where the in-order sequence leaves it,
# apart from the expiry: checkout extends from the later of now and the current
# expiry, and only an `updated` event for the new subscription trims it to
# Stripe's exact period end.
# ---------------------------------------------------------------------------

def _updated(event_id, subscription, price, days, customer="cus_1"):
    return {
        "id": event_id,
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": subscription,
                "customer": customer,
                "status": "active",
                "items": {"data": [{"price": {"id": price}}]},
                "current_period_end": _ts(days),
            }
        },
    }


def _expiry_days(client, key):
    expires = dt.datetime.fromisoformat(status(client, key)["expires_at"])
    return (expires - dt.datetime.now(dt.UTC)).total_seconds() / 86400


def test_an_update_for_the_new_subscription_before_its_checkout_loses_nothing(client):
    from src import billing

    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_A"))     # monthly, stored
    assert billing._subscription_id_of(BUYER) == "sub_A"
    monthly_days = _expiry_days(client, key)

    # B's own update arrives first: refused, answered 200, never redelivered.
    early = post_event(client, _updated("evt_B_early", "sub_B", PRICE_PRO_ANNUAL, 365))
    assert early.status_code == 200
    assert early.json()["status"] == "superseded"
    assert early.json()["current"] == "sub_A"
    assert billing._subscription_id_of(BUYER) == "sub_A"
    assert abs(_expiry_days(client, key) - monthly_days) < 0.1, "the dropped update changed the expiry"

    # B's checkout then arrives and does the whole job.
    post_event(
        client,
        checkout_event(event_id="evt_B_checkout", plan="pro_annual", subscription="sub_B"),
    )
    assert billing._subscription_id_of(BUYER) == "sub_B"
    assert status(client, key)["tier"] == "pro"
    assert _expiry_days(client, key) >= 360, "the annual purchase was not granted in full"

    # And B's next update is accepted now, re-syncing to Stripe's period end.
    later = post_event(client, _updated("evt_B_later", "sub_B", PRICE_PRO_ANNUAL, 365))
    assert later.json()["status"] == "synced"
    assert 364 <= _expiry_days(client, key) <= 366


def test_the_out_of_order_sequence_ends_where_the_in_order_one_does(client):
    """The same events in both orders, for two accounts: the same tier, a stored
    id that is the new subscription, and the same expiry once B's update has
    been accepted."""
    from sqlalchemy import select

    from src import billing
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    # In order: checkout A, checkout B, update B.
    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_A"))
    post_event(client, checkout_event(event_id="evt_B_checkout", plan="pro_annual", subscription="sub_B"))
    post_event(client, _updated("evt_B_update", "sub_B", PRICE_PRO_ANNUAL, 365))
    in_order = (status(client, key)["tier"], billing._subscription_id_of(BUYER), _expiry_days(client, key))

    # Out of order, a second account on its own customer id: checkout A, update B,
    # checkout B, update B again.
    other = "other@example.com"
    key2 = register(client, other)
    with session_scope() as s:
        row = s.execute(select(ApiUser).where(ApiUser.email == other)).scalar_one()
        row.stripe_customer_id = "cus_2"
    first = checkout_event(event_id="evt_A2", plan="pro", email=other, subscription="sub_A2")
    first["data"]["object"]["customer"] = "cus_2"
    post_event(client, first)
    assert post_event(
        client, _updated("evt_B2_early", "sub_B2", PRICE_PRO_ANNUAL, 365, customer="cus_2")
    ).json()["status"] == "superseded"
    second = checkout_event(event_id="evt_B2_checkout", plan="pro_annual", email=other, subscription="sub_B2")
    second["data"]["object"]["customer"] = "cus_2"
    post_event(client, second)
    post_event(client, _updated("evt_B2_settle", "sub_B2", PRICE_PRO_ANNUAL, 365, customer="cus_2"))
    out_of_order = (status(client, key2)["tier"], billing._subscription_id_of(other), _expiry_days(client, key2))

    assert in_order[0] == out_of_order[0] == "pro"
    assert in_order[1] == "sub_B" and out_of_order[1] == "sub_B2"
    assert abs(in_order[2] - out_of_order[2]) < 0.1


def test_the_first_invoice_of_the_new_subscription_before_its_checkout_is_ignored_and_nothing_is_lost(client):
    from src import billing

    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_A"))

    first_invoice = _renewal_event("evt_B_invoice", "sub_B", "parent")
    first_invoice["data"]["object"]["billing_reason"] = "subscription_create"
    r = post_event(client, first_invoice)
    assert r.json()["status"] == "ignored"
    assert r.json()["billing_reason"] == "subscription_create"
    assert billing._subscription_id_of(BUYER) == "sub_A"

    post_event(client, checkout_event(event_id="evt_B_checkout", plan="pro_annual", subscription="sub_B"))
    assert billing._subscription_id_of(BUYER) == "sub_B"
    assert _expiry_days(client, key) >= 360


# ---------------------------------------------------------------------------
# One payment, one period: checkout must not add days on top of an update
#
# `customer.subscription.updated` ASSIGNS Stripe's period end; checkout EXTENDS
# from the later of now and the current expiry. When the update arrives first,
# the extension used to stack on the assignment: one annual payment read as
# about 731 days until the next update re-synced it.
# ---------------------------------------------------------------------------

def _link_customer(email=BUYER, customer="cus_1"):
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    with session_scope() as s:
        row = s.execute(select(ApiUser).where(ApiUser.email == email)).scalar_one()
        row.stripe_customer_id = customer


def test_an_annual_purchase_is_about_a_year_when_the_update_arrives_first(client):
    from src import billing

    key = register(client)
    _link_customer()        # so the update can find the account before checkout has

    first = post_event(client, _updated("evt_upd", "sub_B", PRICE_PRO_ANNUAL, 365))
    assert first.json()["status"] == "synced"
    assert 364 <= _expiry_days(client, key) <= 366

    checkout = post_event(
        client, checkout_event(event_id="evt_chk", plan="pro_annual", subscription="sub_B")
    )
    assert checkout.status_code == 200
    assert checkout.json()["status"] == "already_granted"
    assert checkout.json()["tier"] == "pro"
    days = _expiry_days(client, key)
    assert 364 <= days <= 367, f"one annual payment gave {days:.0f} days, not about 365"
    assert billing._subscription_id_of(BUYER) == "sub_B"
    assert status(client, key)["tier"] == "pro"


def test_a_monthly_purchase_is_about_a_month_when_the_update_arrives_first(client):
    key = register(client)
    _link_customer()

    post_event(client, _updated("evt_upd", "sub_M", PRICE_PRO, 30))
    checkout = post_event(client, checkout_event(event_id="evt_chk", plan="pro", subscription="sub_M"))
    assert checkout.json()["status"] == "already_granted"
    assert 29 <= _expiry_days(client, key) <= 32


def test_an_annual_purchase_is_about_a_year_when_checkout_arrives_first(client):
    """The in-order case is unchanged: checkout grants, the update then trims."""
    from src import billing

    key = register(client)
    checkout = post_event(
        client, checkout_event(event_id="evt_chk", plan="pro_annual", subscription="sub_B")
    )
    assert checkout.json()["status"] == "granted"
    after_checkout = _expiry_days(client, key)
    assert 360 <= after_checkout <= 367
    assert billing._subscription_id_of(BUYER) == "sub_B"

    update = post_event(client, _updated("evt_upd", "sub_B", PRICE_PRO_ANNUAL, 365))
    assert update.json()["status"] == "synced"
    assert 364 <= _expiry_days(client, key) <= 366


def test_a_checkout_that_failed_after_attaching_the_id_still_grants_on_redelivery(client, monkeypatch):
    """The reason the rule is not just "the stored id matches": the id is attached
    BEFORE the grant, so a checkout whose grant raised leaves a matching id and an
    ungranted account. Stripe's redelivery must still grant."""
    from src import billing

    key = register(client)
    real_grant = billing._grant

    def failing_grant(*a, **k):
        raise RuntimeError("database blip mid-grant")

    monkeypatch.setattr(billing, "_grant", failing_grant)
    event = checkout_event(event_id="evt_chk", plan="pro_annual", subscription="sub_B")
    with pytest.raises(RuntimeError):
        post_event(client, event)
    assert billing._subscription_id_of(BUYER) == "sub_B"       # attached, not granted
    assert status(client, key)["tier"] == "free"

    monkeypatch.setattr(billing, "_grant", real_grant)
    retry = post_event(client, event)                          # same event id, released
    assert retry.json()["status"] == "granted"
    assert status(client, key)["tier"] == "pro"
    assert _expiry_days(client, key) >= 360


def test_an_upgrade_whose_grant_failed_after_attaching_still_grants_on_redelivery(client, monkeypatch):
    """Same hazard when the account already holds a (shorter) subscription: the
    old tier and a month of expiry must not be mistaken for the new purchase."""
    from src import billing

    key = register(client)
    post_event(client, checkout_event(plan="pro", subscription="sub_A"))
    assert _expiry_days(client, key) < 40
    real_grant = billing._grant

    monkeypatch.setattr(billing, "_grant", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blip")))
    event = checkout_event(event_id="evt_B_chk", plan="pro_annual", subscription="sub_B")
    with pytest.raises(RuntimeError):
        post_event(client, event)
    assert billing._subscription_id_of(BUYER) == "sub_B"

    monkeypatch.setattr(billing, "_grant", real_grant)
    retry = post_event(client, event)
    assert retry.json()["status"] == "granted"
    assert _expiry_days(client, key) >= 360


def test_the_dataset_is_never_treated_as_already_granted(client):
    key = register(client)
    r = post_event(client, checkout_event(plan="dataset"))
    assert r.json()["status"] == "granted"
    assert status(client, key)["has_paid_download"] is True


# ---------------------------------------------------------------------------
# A default timeout for every Stripe call that does not bring its own client
# ---------------------------------------------------------------------------

def test_the_sdk_gets_a_30_second_default_timeout_and_retries_are_untouched(monkeypatch):
    import stripe

    from src import billing

    monkeypatch.setattr(stripe, "default_http_client", None)
    retries_before = stripe.max_network_retries

    assert billing._sdk() is stripe
    assert billing._STRIPE_TIMEOUT_S == 30.0
    assert stripe.default_http_client._timeout == 30.0
    assert stripe.max_network_retries == retries_before == 2, "retries must be left as the SDK has them"


def test_the_default_client_keeps_the_sdks_choice_of_http_library(monkeypatch):
    import stripe

    from src import billing

    monkeypatch.setattr(stripe, "default_http_client", None)
    chosen = type(stripe.new_default_http_client()).__name__
    billing._sdk()
    assert type(stripe.default_http_client).__name__ == chosen


def test_applying_the_timeout_twice_keeps_the_same_client(monkeypatch):
    import stripe

    from src import billing

    monkeypatch.setattr(stripe, "default_http_client", None)
    billing._sdk()
    first = stripe.default_http_client
    billing._sdk()
    assert stripe.default_http_client is first


def test_a_library_that_cannot_take_a_timeout_falls_back_to_httpx(monkeypatch):
    import stripe

    from src import billing

    def no_timeout(*args, **kwargs):
        raise TypeError("__init__() got an unexpected keyword argument 'timeout'")

    monkeypatch.setattr(stripe, "default_http_client", None)
    monkeypatch.setattr(stripe, "new_default_http_client", no_timeout)
    billing._sdk()
    assert isinstance(stripe.default_http_client, stripe.HTTPXClient)
    assert stripe.default_http_client._timeout == 30.0


def test_the_dispute_lookup_keeps_its_tighter_explicit_timeout():
    """The 30 second default is the ceiling for everything else; the lookup made
    while a webhook waits stays at 10 seconds with no retries."""
    from src import billing

    assert billing._LOOKUP_TIMEOUT_S == 10.0 < billing._STRIPE_TIMEOUT_S == 30.0
