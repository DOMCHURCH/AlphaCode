"""The sign-in panel's memory per address, and the discount on a "no".

Asked for on 2026-09-23: the panel must not come back in every browser on the
same connection once somebody has answered, and a "no" is met with an offer on
Pro. Later the same day: the offer is shown ONCE per address, ever; it can
only be claimed from the page it was shown on; there is no public code -- a
claim mints a single-use Stripe promotion code at checkout.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'offer.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-the-offer")
    monkeypatch.setenv("SIGNIN_OFFER_COUPON", "coupon_half")

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
    assert client.get("/api/signin-prompt").json()["offer"] == {
        "label": "Half off your first month of Pro"}
    from src.config.settings import get_settings

    monkeypatch.setenv("SIGNIN_OFFER_COUPON", "")
    get_settings.cache_clear()
    assert client.get("/api/signin-prompt").json()["offer"] is None
    assert client.post("/api/signin-offer/show").json() == {"show": False}


def test_the_offer_is_shown_once_per_address_ever(client):
    assert client.post("/api/signin-offer/show").json() == {"show": True}
    assert client.post("/api/signin-offer/show").json() == {"show": False}
    assert client.get("/api/signin-prompt").json()["offer"] is None
    # A fresh browser on the same connection: no cookies, same answer.
    client.cookies.clear()
    assert client.post("/api/signin-offer/show").json() == {"show": False}


def test_it_cannot_be_claimed_without_being_shown(client):
    assert client.post("/api/signin-offer/claim").status_code == 410


def test_a_claim_sets_a_session_cookie_once(client):
    client.post("/api/signin-offer/show")
    r = client.post("/api/signin-offer/claim")
    assert r.status_code == 200 and r.json()["ok"] is True
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("bp_offer=") and "HttpOnly" in cookie
    assert "Max-Age" not in cookie and "expires" not in cookie.lower()
    assert client.post("/api/signin-offer/claim").status_code == 410


def test_leaving_the_page_forfeits_it(client):
    client.post("/api/signin-offer/show")
    client.post("/api/signin-offer/forfeit")
    assert client.post("/api/signin-offer/claim").status_code == 410


def test_a_stale_showing_cannot_be_claimed(client):
    import datetime as dt

    from src.storage.db import session_scope
    from src.storage.models import SigninOffer

    client.post("/api/signin-offer/show")
    with session_scope() as session:
        row = session.query(SigninOffer).one()
        row.shown_at = row.shown_at - dt.timedelta(minutes=31)
    assert client.post("/api/signin-offer/claim").status_code == 410


def test_the_panel_prints_the_discounted_price_from_the_percent(client):
    html = client.get("/").text
    sheet = html.split('id="signin-prompt"', 1)[1]
    assert 'id="sp-deal"' in sheet and 'id="sp-no"' in sheet
    assert "<s>$49</s> <b>$24.50</b>" in sheet


# --- checkout ----------------------------------------------------------------

class _FakeStripe:
    def __init__(self):
        self.params: dict = {}
        self.minted: list[dict] = []
        outer = self

        class _Sessions:
            @staticmethod
            def create(**params):
                outer.params = params
                return SimpleNamespace(url="https://checkout.stripe.test/x", id="cs_1")

        class _Promos:
            @staticmethod
            def create(**kw):
                outer.minted.append(kw)
                return SimpleNamespace(id=f"promo_{len(outer.minted)}")

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


def _promo_for(client, plan="pro"):
    from starlette.requests import Request

    from src import api

    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    scope = {"type": "http", "method": "POST", "path": "/", "scheme": "https",
             "headers": [(b"cookie", cookie.encode())], "client": ("testclient", 1),
             "server": ("testserver", 443), "query_string": b""}
    return api._claimed_offer_promo(Request(scope), plan)


def test_a_claim_mints_one_single_use_code_and_reuses_it(client, fake_stripe):
    client.post("/api/signin-offer/show")
    client.post("/api/signin-offer/claim")
    assert _promo_for(client) == "promo_1"
    assert _promo_for(client) == "promo_1"
    assert len(fake_stripe.minted) == 1
    kw = fake_stripe.minted[0]
    assert kw["promotion"] == {"type": "coupon", "coupon": "coupon_half"}
    assert kw["max_redemptions"] == 1 and "code" not in kw
    assert kw["restrictions"] == {"first_time_transaction": True}


def test_no_claim_or_an_expired_claim_gets_no_discount(client, fake_stripe):
    import datetime as dt

    from src.storage.db import session_scope
    from src.storage.models import SigninOffer

    assert _promo_for(client) is None
    client.post("/api/signin-offer/show")
    assert _promo_for(client) is None  # shown, never claimed
    client.post("/api/signin-offer/claim")
    with session_scope() as session:
        row = session.query(SigninOffer).one()
        row.expires_at = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(seconds=1)
    assert _promo_for(client) is None
    assert fake_stripe.minted == []


def test_annual_never_gets_the_monthly_offer(client, fake_stripe):
    client.post("/api/signin-offer/show")
    client.post("/api/signin-offer/claim")
    assert _promo_for(client, "pro_annual") is None


def test_a_minted_code_is_applied_to_monthly_pro(client, fake_stripe):
    from src import billing

    billing.create_checkout_session(plan="pro", origin="https://x",
                                    email="a@example.com", offer_promo_id="promo_9")
    assert fake_stripe.params["discounts"] == [{"promotion_code": "promo_9"}]
    assert "allow_promotion_codes" not in fake_stripe.params


def test_annual_checkout_never_carries_it(client, fake_stripe):
    from src import billing

    billing.create_checkout_session(plan="pro_annual", origin="https://x",
                                    email="a@example.com", offer_promo_id="promo_9")
    assert "discounts" not in fake_stripe.params
    assert fake_stripe.params["allow_promotion_codes"] is True


def test_without_a_claim_the_buyer_can_still_type_a_code(client, fake_stripe):
    from src import billing

    billing.create_checkout_session(plan="pro", origin="https://x", email="a@example.com")
    assert fake_stripe.params["allow_promotion_codes"] is True
