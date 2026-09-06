"""Pricing copy, the public demo, and key recovery by email.

The three features share one property worth testing hard: each of them takes
something that was private (a price, a working key, the existence of an account)
and puts it somewhere more public. So most of what follows checks the edge that
was NOT crossed -- the demo key never reaching the browser, the recovery reply
never distinguishing a registered address from an unregistered one, the prices
never being written down in two places that can disagree.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_SECRET = "test-admin-secret-do-not-use"
DEMO_KEY = "demo-key-for-tests-0123456789abcdef"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "features.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "7")
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "5000")
    monkeypatch.setenv("DATASET_PRICE_USD", "29")
    monkeypatch.setenv("PRO_PRICE_USD", "49")
    monkeypatch.setenv("DEMO_API_KEY", DEMO_KEY)
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "3")
    monkeypatch.setenv("SMTP_HOST", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.accounts import reset_resend_cooldowns
    from src.api import _register_gate, _resend_gate, app
    from src.dataset import reset_count_cache

    _register_gate.reset()
    _resend_gate.reset()
    reset_resend_cooldowns()
    reset_count_cache()

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def seed_jpm():
    """One company, so the demo has something real to return."""
    import datetime as dt

    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker="JPM", name="JPMorgan Chase & Co."
        ))
        for metric, val in [
            ("total_assets", 4.1e12),
            ("total_liabilities", 3.75e12),
            ("total_equity", 3.5e11),
        ]:
            s.add(Fundamental(
                ticker="JPM", metric=metric, value=val,
                period_end=dt.date(2025, 6, 30), fiscal_period="Q2",
                filing_date=dt.date(2025, 8, 1), source="sec",
            ))


# ---------------------------------------------------------------------------
# 1. Pricing
# ---------------------------------------------------------------------------

def test_the_home_page_prices_come_from_settings_not_the_copy(client):
    """The env sets an unusual free tier (7) and Pro ceiling (5,000). If the
    card is hardcoded it will say 10 and 10,000 -- and in production it would
    quote a price the API does not enforce."""
    html = client.get("/").text
    assert "$29" in html and "$49" in html and "$0" in html
    assert "7 API calls per month" in html
    assert "5,000 API calls per month" in html
    assert "10 API calls per month" not in html


def test_the_demo_blurb_quotes_the_real_daily_limit(client):
    """The env sets 3. A sentence promising five above a counter that stops at
    three is the small kind of lie that costs a reader their trust in the
    numbers this whole site is about."""
    html = client.get("/").text
    assert "3 companies a day from one address" in html
    assert "Five companies a day" not in html


def test_pricing_sits_under_the_accuracy_banner(client):
    html = client.get("/").text
    assert html.index('class="acc"') < html.index('id="pricing"')
    assert html.index('id="pricing"') < html.index('class="summary"')


def test_the_dataset_card_counts_the_rows_it_is_selling(client):
    seed_jpm()
    from src.dataset import reset_count_cache

    reset_count_cache()
    html = client.get("/").text
    assert "Download all 3 rows as CSV" in html


# ---------------------------------------------------------------------------
# 2. Key recovery
# ---------------------------------------------------------------------------

def test_recovery_says_the_same_thing_for_known_and_unknown_addresses(
    client, monkeypatch
):
    """Otherwise this is an account-enumeration oracle, and it would be a poor
    trade to refuse the key on /register and then leak who has one here."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    client.post("/api/auth/register", json={"email": "known@example.com"})
    sent = []
    monkeypatch.setattr(
        "src.mailer.send_api_key", lambda e, k: sent.append((e, k)) or True
    )

    a = client.post("/api/auth/resend-key", json={"email": "known@example.com"})
    b = client.post("/api/auth/resend-key", json={"email": "nobody@example.com"})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    # ...and only the registered one actually produced an email.
    assert [e for e, _ in sent] == ["known@example.com"]


def test_the_recovery_response_never_contains_the_key(client, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("src.mailer.send_api_key", lambda e, k: True)

    key = client.post(
        "/api/auth/register", json={"email": "keeper@example.com"}
    ).json()["api_key"]
    r = client.post("/api/auth/resend-key", json={"email": "keeper@example.com"})
    assert key not in r.text


def test_recovery_says_so_when_smtp_is_not_configured(client):
    """Rather than accepting the request and dropping it, which leaves somebody
    waiting on an email that was never going to be sent."""
    client.post("/api/auth/register", json={"email": "lost@example.com"})
    r = client.post("/api/auth/resend-key", json={"email": "lost@example.com"})
    assert r.status_code == 503
    assert r.json()["sent"] is False
    assert "owner@example.com" in r.json()["detail"]


def test_one_address_cannot_be_mailed_repeatedly(client, monkeypatch):
    """A registered address is somebody else's inbox. Without a cooldown the
    recovery button is an email cannon aimed at them."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    sent = []
    monkeypatch.setattr(
        "src.mailer.send_api_key", lambda e, k: sent.append(e) or True
    )

    client.post("/api/auth/register", json={"email": "target@example.com"})
    for _ in range(4):
        r = client.post(
            "/api/auth/resend-key", json={"email": "target@example.com"}
        )
        assert r.status_code == 200  # the reply never changes
    assert len(sent) == 1


def test_the_mail_body_carries_the_key_and_nothing_alarming(monkeypatch):
    """The one place the key is legitimately written down."""
    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "keys@example.com")
    get_settings.cache_clear()

    captured = {}
    monkeypatch.setattr(mailer, "_deliver", lambda msg: captured.update(msg=msg))
    assert mailer.send_api_key("someone@example.com", "SECRET-KEY-123") is True

    msg = captured["msg"]
    assert msg["To"] == "someone@example.com"
    assert msg["From"] == "keys@example.com"
    assert "SECRET-KEY-123" in msg.get_content()
    get_settings.cache_clear()


def test_a_dead_relay_is_a_false_return_not_an_exception(monkeypatch):
    """`send_api_key` runs in a background task with nobody to catch it."""
    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    get_settings.cache_clear()

    def boom(msg):
        raise OSError("connection refused")

    monkeypatch.setattr(mailer, "_deliver", boom)
    assert mailer.send_api_key("someone@example.com", "k") is False
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 3. The public demo
# ---------------------------------------------------------------------------

def test_the_demo_returns_real_data_with_no_key(client):
    seed_jpm()
    r = client.get("/api/demo/JPM")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "JPM"
    assert body["assets"]["total_assets"]["value"] == 4.1e12
    assert body["demo"]["calls_limit"] == 3


def test_the_demo_key_is_never_sent_to_the_browser(client):
    """The whole reason the demo has its own route. A key in the page is a
    published credential, and the per-address limit it exists to demonstrate
    would be bypassed by lifting it out of the HTML."""
    seed_jpm()
    for path in ("/", "/static/home.js", "/api/demo/JPM"):
        assert DEMO_KEY not in client.get(path).text, path


def test_the_demo_key_does_not_work_on_the_real_api(client):
    """Even if it leaks. /api/company has no per-address ceiling behind it."""
    from src.demo import ensure_demo_user

    seed_jpm()
    ensure_demo_user()
    r = client.get("/api/company/JPM", headers={"X-API-Key": DEMO_KEY})
    assert r.status_code == 403
    assert "demo" in r.json()["detail"].lower()
    assert client.get(
        "/api/user/status", headers={"X-API-Key": DEMO_KEY}
    ).status_code == 403


def test_the_demo_runs_out_after_the_daily_allowance(client):
    seed_jpm()
    for i in range(3):
        assert client.get("/api/demo/JPM").status_code == 200, i
    r = client.get("/api/demo/JPM")
    assert r.status_code == 429
    assert "resets at midnight utc" in r.json()["detail"].lower()


def test_a_wrong_ticker_does_not_spend_a_demo_call(client):
    seed_jpm()
    assert client.get("/api/demo/NOSUCH").status_code == 404
    assert client.get("/api/demo/JPM").json()["demo"]["calls_used_today"] == 1


def test_demo_traffic_stays_out_of_paying_customers_usage(client):
    """Demo calls are counted in `demo_usage`, never in `usage_logs` -- so the
    demo account's own dashboard, and everyone else's, shows only real use."""
    from sqlalchemy import func, select

    from src.storage.db import session_scope
    from src.storage.models import UsageLog

    seed_jpm()
    client.get("/api/demo/JPM")
    with session_scope() as s:
        assert s.execute(select(func.count()).select_from(UsageLog)).scalar_one() == 0


def test_yesterdays_demo_calls_do_not_count_today(client):
    from src.storage.db import session_scope
    from src.storage.models import DemoUsage

    seed_jpm()
    from src.api import _caller_ip_hash  # noqa: F401  (shape check only)

    with session_scope() as s:
        for _ in range(9):
            s.add(DemoUsage(ip_hash="whoever", day="1999-01-01"))
    assert client.get("/api/demo/JPM").status_code == 200


def test_the_demo_is_off_rather_than_broken_when_unconfigured(client, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("DEMO_API_KEY", "")
    get_settings.cache_clear()
    r = client.get("/api/demo/JPM")
    assert r.status_code == 503
    # The home page still renders its demo section; it simply cannot answer.
    assert client.get("/").status_code == 200


def test_the_demo_account_provisions_itself_rather_than_racing_boot(client):
    """`_boot` creates it, but boot is a background task -- the server answers
    before it finishes. A cold container must serve the first visitor, not a
    503, so the lookup provisions on demand."""
    from src.accounts import lookup
    from src.demo import DEMO_EMAIL
    from src.storage.db import session_scope
    from src.storage.models import ApiUser

    # Simulate the window before boot has run.
    with session_scope() as s:
        for row in s.query(ApiUser).filter(ApiUser.email == DEMO_EMAIL).all():
            s.delete(row)
    assert lookup(DEMO_KEY) is None

    from src.demo import account as demo_account

    account = demo_account()
    assert account is not None
    assert account.email == DEMO_EMAIL
    assert account.tier == "pro"
    # Never the dataset: the demo shows one company, not the product.
    assert account.has_paid_download is False
