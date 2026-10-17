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
