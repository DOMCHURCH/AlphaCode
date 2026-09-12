"""The four Stripe events that used to be dropped on the floor.

`test_billing.py` covers the money arriving. This covers the money NOT
arriving, which is the half that was unhandled: a card that stops working, a
bank that wants the cardholder to authenticate, a plan changed in the Stripe
dashboard, and a checkout nobody finished.

Every test here asserts a DATABASE consequence rather than the handler's return
value, because a handler that answers `{"status": "paused"}` and changes
nothing is exactly the bug this file exists to catch. Each one also delivers
its event twice and asserts the second delivery changed nothing -- Stripe
retries for three days, and an idempotency table that is never tested is a
table that works until the first real retry.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
WEBHOOK_SECRET = "whsec_test_do_not_use"
PRICE_PRO = "price_test_pro_monthly"
PRICE_PRO_ANNUAL = "price_test_pro_annual"
PRICE_DATASET = "price_test_dataset"
GOOD_SIG = "t=1,v1=this-one-verifies"

BUYER = "buyer@example.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A TestClient over a throwaway on-disk database with Stripe configured.

    On disk rather than `:memory:`: the API opens its own connections through
    `session_scope`, and an in-memory database is private to the connection
    that created it.
    """
    db = tmp_path / "webhooks.db"
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
    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", PRICE_PRO_ANNUAL)
    monkeypatch.setenv("STRIPE_PRICE_DATASET", PRICE_DATASET)

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import _checkout_gate, _register_gate, app
    from src.billing import reset_last_error

    _register_gate.reset()
    _checkout_gate.reset()
    reset_last_error()

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture()
def stripe_calls(monkeypatch):
    """Signature verification accepts exactly one header and nothing else."""
    import stripe

    def _construct(payload, sig_header, secret, tolerance=300, api_key=None):
        assert secret == WEBHOOK_SECRET
        if sig_header != GOOD_SIG:
            raise stripe.SignatureVerificationError("no match", sig_header)
        return json.loads(payload)

    monkeypatch.setattr(stripe.Webhook, "construct_event", staticmethod(_construct))


@pytest.fixture()
def mails(monkeypatch):
    """Capture dunning mail instead of sending it. Returns the list of sends."""
    from src import mailer

    sent: list[dict] = []

    def _fake(email, *, attempt, remaining, invoice_url=""):
        sent.append(
            {
                "email": email, "attempt": attempt,
                "remaining": remaining, "invoice_url": invoice_url,
            }
        )
        return True

    monkeypatch.setattr(mailer, "send_payment_failed", _fake)
    return sent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post_event(client, event, sig=GOOD_SIG):
    return client.post(
        "/api/billing/webhook",
        content=json.dumps(event).encode(),
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )


def buy_pro(client, *, email=BUYER, customer="cus_1", subscription="sub_1"):
    """Put a real paying account in the database, the way a purchase does.

    Through the webhook rather than by writing rows: the events under test find
    their account by the Stripe ids that `checkout.session.completed` stores,
    so a hand-built row would test a lookup that never happens in production.
    """
    event = {
        "id": "evt_setup",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_setup",
                "object": "checkout.session",
                "mode": "subscription",
                "payment_status": "paid",
                "customer": customer,
                "subscription": subscription,
                "customer_details": {"email": email},
                "metadata": {"plan": "pro", "email": email},
            }
        },
    }
    r = post_event(client, event)
    assert r.status_code == 200, r.text
    return r.json()


def account(email=BUYER):
    from src import accounts

    return accounts.by_email(email)


def failed_invoice(
    *, event_id, reason="subscription_cycle", customer="cus_1",
    subscription="sub_1", invoice_url="https://invoice.stripe.com/i/test",
):
    return {
        "id": event_id,
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "id": "in_failed",
                "billing_reason": reason,
                "customer": customer,
                "subscription": subscription,
                "hosted_invoice_url": invoice_url,
            }
        },
    }


def subscription_updated(
    *, event_id, price=PRICE_PRO_ANNUAL, customer="cus_1", subscription="sub_1",
    status="active", days_left=365, cancel_at_period_end=False,
):
    end = int((dt.datetime.now(dt.UTC) + dt.timedelta(days=days_left)).timestamp())
    return {
        "id": event_id,
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": subscription,
                "customer": customer,
                "status": status,
                "cancel_at_period_end": cancel_at_period_end,
                "current_period_end": end,
                "items": {"data": [{"price": {"id": price}}]},
            }
        },
    }


# ---------------------------------------------------------------------------
# invoice.payment_failed
# ---------------------------------------------------------------------------

def test_one_failed_payment_warns_and_leaves_access_alone(client, stripe_calls, mails):
    """The first decline is a message, not a punishment.

    A card declined once is usually a bank being cautious, and cutting access
    on it would cost a paying customer their afternoon over something that
    fixes itself overnight.
    """
    buy_pro(client)

    r = post_event(client, failed_invoice(event_id="evt_f1"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "warned"

    acct = account()
    assert acct.payment_failure_count == 1
    assert acct.api_access_paused is False
    assert acct.tier == "pro", "one decline must not touch the tier"

    assert len(mails) == 1
    assert mails[0]["attempt"] == 1
    assert mails[0]["remaining"] == 2
    assert mails[0]["invoice_url"] == "https://invoice.stripe.com/i/test"


def test_three_failed_payments_pause_access_and_drop_the_tier(
    client, stripe_calls, mails
):
    """The third consecutive failure is where the money has clearly stopped."""
    buy_pro(client)

    for n in (1, 2):
        assert post_event(client, failed_invoice(event_id=f"evt_f{n}")).status_code == 200
        assert account().api_access_paused is False

    r = post_event(client, failed_invoice(event_id="evt_f3"))
    assert r.json()["status"] == "paused"

    acct = account()
    assert acct.payment_failure_count == 3
    assert acct.api_access_paused is True
    assert acct.tier == "free"
    assert mails[-1]["remaining"] == 0


def test_a_paused_account_is_refused_with_402_not_429(client, stripe_calls, mails):
    """The flag has to actually stop a call, or it is decoration.

    402 rather than 429 on purpose: nothing here resets on the 1st, and telling
    somebody to wait for a quota that is not the problem sends them to the
    wrong page.
    """
    key = buy_pro(client)
    api_key = key.get("api_key") or _key_for(client)

    for n in (1, 2, 3):
        post_event(client, failed_invoice(event_id=f"evt_f{n}"))

    r = client.get("/api/company/AAPL", headers={"X-API-Key": api_key})
    assert r.status_code == 402, r.text
    assert "paused" in r.json()["detail"].lower()


def test_a_successful_payment_clears_the_run_and_lifts_the_pause(
    client, stripe_calls, mails
):
    """A card that works again is a paying customer, not a suspended one."""
    buy_pro(client)
    for n in (1, 2, 3):
        post_event(client, failed_invoice(event_id=f"evt_f{n}"))
    assert account().api_access_paused is True

    paid = {
        "id": "evt_paid",
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "id": "in_ok", "billing_reason": "subscription_cycle",
                "customer": "cus_1", "subscription": "sub_1",
            }
        },
    }
    assert post_event(client, paid).status_code == 200

    acct = account()
    assert acct.payment_failure_count == 0
    assert acct.api_access_paused is False
    assert acct.tier == "pro"


def test_a_redelivered_failure_does_not_count_twice(client, stripe_calls, mails):
    """Stripe retries for three days. One decline must count once."""
    buy_pro(client)
    event = failed_invoice(event_id="evt_same")

    assert post_event(client, event).status_code == 200
    first = post_event(client, event)

    assert first.json()["status"] == "duplicate"
    assert account().payment_failure_count == 1
    assert len(mails) == 1, "the second delivery must not mail them again"


def test_a_first_invoice_failure_is_ignored(client, stripe_calls, mails):
    """`subscription_create` failing means the checkout never completed.

    There is nothing to withdraw, and counting it would start a dunning run
    against an account that was never granted anything.
    """
    buy_pro(client)
    r = post_event(
        client, failed_invoice(event_id="evt_first", reason="subscription_create")
    )
    assert r.json()["status"] == "ignored"
    assert account().payment_failure_count == 0
    assert mails == []


# ---------------------------------------------------------------------------
# customer.subscription.updated
# ---------------------------------------------------------------------------

def test_an_upgrade_moves_the_plan_and_the_expiry(client, stripe_calls):
    """Monthly to annual in Stripe has to reach the expiry stored here.

    This is the bug the handler exists for: without it the account kept a
    31-day expiry after buying a year, and lapsed eleven months early.
    """
    buy_pro(client)
    before = account()
    assert before.pro_plan == ""

    r = post_event(client, subscription_updated(event_id="evt_u1", days_left=365))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "synced"
    assert body["plan"] == "annual"

    acct = account()
    assert acct.pro_plan == "annual"
    assert acct.pro_expires_at is not None
    days = (acct.pro_expires_at - dt.datetime.now()).days
    assert 360 <= days <= 366, f"expiry should be about a year out, got {days}d"


def test_a_downgrade_moves_the_expiry_backwards(client, stripe_calls):
    """Assignment, not extension. Stripe owns the period end."""
    buy_pro(client)
    post_event(client, subscription_updated(event_id="evt_up", days_left=365))
    assert account().pro_plan == "annual"

    post_event(
        client,
        subscription_updated(event_id="evt_down", price=PRICE_PRO, days_left=30),
    )
    acct = account()
    assert acct.pro_plan == "monthly"
    days = (acct.pro_expires_at - dt.datetime.now()).days
    assert days <= 31, f"a downgrade must shorten the period, got {days}d"


def test_cancel_at_period_end_is_recorded_but_takes_nothing_away(
    client, stripe_calls
):
    """They paid for the time. `customer.subscription.deleted` ends it."""
    buy_pro(client)
    r = post_event(
        client,
        subscription_updated(event_id="evt_cae", cancel_at_period_end=True),
    )
    assert r.json()["cancel_at_period_end"] is True
    assert account().tier == "pro", "access must survive a scheduled cancellation"


def test_an_update_for_an_unknown_customer_changes_nothing(client, stripe_calls):
    """Somebody else's subscription on the same Stripe account."""
    buy_pro(client)
    r = post_event(
        client, subscription_updated(event_id="evt_other", customer="cus_stranger",
                                     subscription="sub_stranger")
    )
    assert r.json()["status"] == "unknown_customer"
    assert account().pro_plan == ""


def test_a_redelivered_update_is_ignored(client, stripe_calls):
    buy_pro(client)
    event = subscription_updated(event_id="evt_dup_u")
    assert post_event(client, event).json()["status"] == "synced"
    assert post_event(client, event).json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# invoice.payment_action_required
# ---------------------------------------------------------------------------

def test_action_required_mails_the_link_and_touches_nothing_else(
    client, stripe_calls, mails
):
    """SCA is a person to ask, not a failure to count."""
    buy_pro(client)
    event = {
        "id": "evt_sca",
        "type": "invoice.payment_action_required",
        "data": {
            "object": {
                "id": "in_sca", "customer": "cus_1", "subscription": "sub_1",
                "hosted_invoice_url": "https://invoice.stripe.com/i/sca",
            }
        },
    }
    r = post_event(client, event)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "action_required"
    assert body["invoice_url"] == "https://invoice.stripe.com/i/sca"

    acct = account()
    assert acct.tier == "pro"
    assert acct.payment_failure_count == 0, "authentication pending is not a failure"
    assert mails[-1]["invoice_url"] == "https://invoice.stripe.com/i/sca"


def test_a_redelivered_action_required_does_not_mail_twice(
    client, stripe_calls, mails
):
    buy_pro(client)
    event = {
        "id": "evt_sca_dup",
        "type": "invoice.payment_action_required",
        "data": {"object": {"id": "in_s", "customer": "cus_1", "subscription": "sub_1"}},
    }
    post_event(client, event)
    assert post_event(client, event).json()["status"] == "duplicate"
    assert len(mails) == 1


# ---------------------------------------------------------------------------
# checkout.session.expired
# ---------------------------------------------------------------------------

def test_an_expired_checkout_is_recorded_and_grants_nothing(client, stripe_calls):
    """An abandoned cart. No account, no grant, but not silence either."""
    event = {
        "id": "evt_exp",
        "type": "checkout.session.expired",
        "data": {
            "object": {
                "id": "cs_abandoned",
                "object": "checkout.session",
                "metadata": {"plan": "pro", "email": "window.shopper@example.com"},
                "customer_details": {"email": "window.shopper@example.com"},
            }
        },
    }
    r = post_event(client, event)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "expired"
    assert body["plan"] == "pro"
    assert account("window.shopper@example.com") is None, "no account may be created"


def test_a_redelivered_expiry_is_ignored(client, stripe_calls):
    event = {
        "id": "evt_exp_dup",
        "type": "checkout.session.expired",
        "data": {"object": {"id": "cs_x", "metadata": {"plan": "pro"}}},
    }
    assert post_event(client, event).json()["status"] == "expired"
    assert post_event(client, event).json()["status"] == "duplicate"


# ---------------------------------------------------------------------------
# The table itself
# ---------------------------------------------------------------------------

def test_every_advertised_event_has_a_handler():
    """The eight events this service tells Stripe it can act on.

    A list rather than a count, so adding a ninth without a handler fails here
    rather than in production three weeks later when somebody's card expires.
    """
    from src.billing import _HANDLERS

    for kind in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.expired",
        "customer.subscription.deleted",
        "customer.subscription.updated",
        "invoice.payment_succeeded",
        "invoice.payment_failed",
        "invoice.payment_action_required",
    ):
        assert kind in _HANDLERS, f"{kind} would be silently dropped"


def test_an_unsigned_body_never_reaches_a_handler(client, stripe_calls, mails):
    """The signature is the only thing standing in front of these handlers."""
    buy_pro(client)
    r = post_event(client, failed_invoice(event_id="evt_forged"), sig="t=1,v1=forged")
    assert r.status_code == 400
    assert account().payment_failure_count == 0
    assert mails == []


def _key_for(client, email=BUYER):
    """A usable API key for an account created by a webhook.

    Rotated rather than read. Keys are stored as SHA-256 digests, so there is
    no "read it back" any more -- and issuing a fresh one is exactly what the
    account holder would do, so the test exercises a real path instead of a
    back door that only tests have.
    """
    from src import accounts, auth

    acct = accounts.by_email(email)
    assert acct is not None
    return auth.regenerate_key(email)
