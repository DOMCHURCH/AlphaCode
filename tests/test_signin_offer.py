"""The sign-in panel's memory per address, and the discount on a "no".

Asked for on 2026-09-23: the panel must not come back in every browser on the
same connection once somebody has answered, and a "no" is met once with an
offer on Pro. The code is Stripe's (`FIRSTMONTH50`, monthly Pro only); what is
pinned here is that the site remembers, reports and applies it correctly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'offer.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-the-offer")
    monkeypatch.setenv("SIGNIN_OFFER_CODE", "FIRSTMONTH50")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import app

    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def test_a_no_is_remembered_for_the_address(client):
    before = client.get("/api/signin-prompt").json()
    assert before["answered"] is None and before["after"] is None
    client.post("/api/signin-prompt/answer", json={"answer": "no", "count": 7})
    after = client.get("/api/signin-prompt").json()
    assert after["answered"] == "no"
    assert after["after"] == 7 + after["interval"]


def test_a_yes_is_final_and_a_later_no_does_not_undo_it(client):
    client.post("/api/signin-prompt/answer", json={"answer": "yes", "count": 3})
    client.post("/api/signin-prompt/answer", json={"answer": "no", "count": 9})
    body = client.get("/api/signin-prompt").json()
    assert body["answered"] == "yes"
    assert body["after"] is None


def test_only_yes_or_no_is_accepted(client):
    r = client.post("/api/signin-prompt/answer", json={"answer": "maybe"})
    assert r.status_code == 422


def test_the_offer_is_reported_only_when_configured(client, monkeypatch):
    assert client.get("/api/signin-prompt").json()["offer"]["code"] == "FIRSTMONTH50"
    from src.config.settings import get_settings

    monkeypatch.setenv("SIGNIN_OFFER_CODE", "")
    get_settings.cache_clear()
    assert client.get("/api/signin-prompt").json()["offer"] is None


def test_the_panel_prints_the_discounted_price_from_the_percent(client):
    html = client.get("/").text
    sheet = html.split('id="signin-prompt"', 1)[1]
    assert 'id="sp-deal"' in sheet and 'id="sp-no"' in sheet
    assert "<s>$49</s> <b>$24.50</b>" in sheet


# --- checkout ----------------------------------------------------------------

class _FakeStripe:
    def __init__(self, promo_found: bool = True):
        self.params: dict = {}
        outer = self

        class _Sessions:
            @staticmethod
            def create(**params):
                outer.params = params
                return SimpleNamespace(url="https://checkout.stripe.test/x", id="cs_1")

        class _Promos:
            @staticmethod
            def list(**kw):
                data = [SimpleNamespace(id="promo_123")] if promo_found else []
                return SimpleNamespace(data=data)

        self.checkout = SimpleNamespace(Session=_Sessions)
        self.PromotionCode = _Promos


@pytest.fixture()
def fake_stripe(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_PRO", "price_pro")
    monkeypatch.setenv("STRIPE_PRICE_PRO_ANNUAL", "price_year")
    monkeypatch.setenv("STRIPE_PRICE_DATASET", "price_data")
    from src import billing
    from src.config.settings import get_settings

    get_settings.cache_clear()
    fake = _FakeStripe()
    monkeypatch.setattr(billing, "_sdk", lambda: fake)
    yield fake
    get_settings.cache_clear()


def test_a_claimed_code_is_applied_to_monthly_pro(client, fake_stripe):
    from src import billing

    billing.create_checkout_session(plan="pro", origin="https://x",
                                    email="a@example.com", promo_code="FIRSTMONTH50")
    assert fake_stripe.params["discounts"] == [{"promotion_code": "promo_123"}]
    assert "allow_promotion_codes" not in fake_stripe.params


def test_annual_never_carries_the_monthly_discount(client, fake_stripe):
    from src import billing

    billing.create_checkout_session(plan="pro_annual", origin="https://x",
                                    email="a@example.com", promo_code="FIRSTMONTH50")
    assert "discounts" not in fake_stripe.params
    assert fake_stripe.params["allow_promotion_codes"] is True


def test_without_a_claim_the_buyer_can_still_type_a_code(client, fake_stripe):
    from src import billing

    billing.create_checkout_session(plan="pro", origin="https://x", email="a@example.com")
    assert fake_stripe.params["allow_promotion_codes"] is True
