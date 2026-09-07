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
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

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
    # Method first, then the claim about accuracy, then the price. Naming a
    # price before saying where the numbers come from is the wrong order for a
    # reader deciding whether to trust a financial dataset at all.
    assert html.index('id="trust"') < html.index('class="acc"')
    assert html.index('class="acc"') < html.index('id="pricing"')


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
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    from src.config.settings import get_settings

    get_settings.cache_clear()

    client.post("/api/auth/register", json={"email": "known@example.com", "accept_terms": True})
    sent = []
    monkeypatch.setattr(
        "src.mailer.send_api_key", lambda e, k: sent.append((e, k)) or True
    )

    a = client.post("/api/auth/resend-key", json={"email": "known@example.com", "accept_terms": True})
    b = client.post("/api/auth/resend-key", json={"email": "nobody@example.com", "accept_terms": True})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    # ...and only the registered one actually produced an email.
    assert [e for e, _ in sent] == ["known@example.com"]


def test_the_recovery_response_never_contains_the_key(client, monkeypatch):
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr("src.mailer.send_api_key", lambda e, k: True)

    key = client.post(
        "/api/auth/register", json={"email": "keeper@example.com", "accept_terms": True}
    ).json()["api_key"]
    r = client.post("/api/auth/resend-key", json={"email": "keeper@example.com", "accept_terms": True})
    assert key not in r.text


def test_recovery_says_so_when_email_is_not_configured(client):
    """Rather than accepting the request and dropping it, which leaves somebody
    waiting on an email that was never going to be sent."""
    client.post("/api/auth/register", json={"email": "lost@example.com", "accept_terms": True})
    r = client.post("/api/auth/resend-key", json={"email": "lost@example.com", "accept_terms": True})
    assert r.status_code == 503
    assert r.json()["sent"] is False
    assert "owner@example.com" in r.json()["detail"]


def test_one_address_cannot_be_mailed_repeatedly(client, monkeypatch):
    """A registered address is somebody else's inbox. Without a cooldown the
    recovery button is an email cannon aimed at them."""
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    sent = []
    monkeypatch.setattr(
        "src.mailer.send_api_key", lambda e, k: sent.append(e) or True
    )

    client.post("/api/auth/register", json={"email": "target@example.com", "accept_terms": True})
    for _ in range(4):
        r = client.post(
            "/api/auth/resend-key", json={"email": "target@example.com", "accept_terms": True}
        )
        assert r.status_code == 200  # the reply never changes
    assert len(sent) == 1


def test_the_mail_body_carries_the_key(monkeypatch):
    """The one place the key is legitimately written down."""
    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "inbox_fixed")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    get_settings.cache_clear()
    mailer.reset_inbox_cache()

    sent = {}

    class FakeMessages:
        def send(self, inbox_id, **kw):
            sent.update(inbox_id=inbox_id, **kw)

    class FakeInboxes:
        messages = FakeMessages()

    class FakeClient:
        inboxes = FakeInboxes()

    monkeypatch.setattr(mailer, "_client", lambda: FakeClient())
    assert mailer.send_api_key("someone@example.com", "SECRET-KEY-123") is True

    assert sent["inbox_id"] == "inbox_fixed"
    assert sent["to"] == "someone@example.com"
    assert sent["subject"] == "Your To Scale API key"
    assert "SECRET-KEY-123" in sent["text"]
    assert sent["reply_to"] == "owner@example.com"
    get_settings.cache_clear()
    mailer.reset_inbox_cache()


def test_a_failing_upstream_is_a_false_return_not_an_exception(monkeypatch):
    """`send_api_key` runs in a background task with nobody to catch it."""
    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "inbox_fixed")
    get_settings.cache_clear()
    mailer.reset_inbox_cache()

    class Boom:
        def send(self, inbox_id, **kw):
            raise RuntimeError("upstream exploded")

    class FakeInboxes:
        messages = Boom()

    class FakeClient:
        inboxes = FakeInboxes()

    monkeypatch.setattr(mailer, "_client", lambda: FakeClient())
    assert mailer.send_api_key("someone@example.com", "k") is False
    get_settings.cache_clear()
    mailer.reset_inbox_cache()


def test_a_missing_sdk_is_off_rather_than_a_crash(monkeypatch):
    """The import is deferred so a container built before requirements.txt
    gained the package has recovery switched off, not a failed boot."""
    import builtins

    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    get_settings.cache_clear()
    mailer.reset_inbox_cache()

    real_import = builtins.__import__

    def no_agentmail(name, *a, **kw):
        if name == "agentmail":
            raise ImportError("no module named agentmail")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_agentmail)
    assert mailer._client() is None
    assert mailer.send_api_key("someone@example.com", "k") is False
    get_settings.cache_clear()


def test_the_inbox_is_resolved_once_and_reused(monkeypatch):
    """Creating a mailbox per send would mint a new address on every restart.
    Pinned by config, or created under a stable client_id and cached."""
    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "")
    get_settings.cache_clear()
    mailer.reset_inbox_cache()

    creates = []

    class FakeInbox:
        inbox_id = "inbox_made"
        email = "to-scale@agentmail.to"
        client_id = mailer._INBOX_CLIENT_ID

    class FakeInboxes:
        def create(self, request=None):
            creates.append(request)
            return FakeInbox()

        class messages:  # noqa: N801 - mirrors the SDK's attribute layout
            @staticmethod
            def send(inbox_id, **kw):
                return None

    class FakeClient:
        inboxes = FakeInboxes()

    client = FakeClient()
    monkeypatch.setattr(mailer, "_client", lambda: client)

    assert mailer.send_api_key("a@example.com", "k") is True
    assert mailer.send_api_key("b@example.com", "k") is True
    assert len(creates) == 1, "the inbox must be created once, not per message"
    assert creates[0].client_id == mailer._INBOX_CLIENT_ID
    get_settings.cache_clear()
    mailer.reset_inbox_cache()


def test_an_existing_inbox_is_found_when_create_conflicts(monkeypatch):
    """A server that answers an existing client_id with a conflict rather than
    the existing inbox is an equally fair reading of the API. Getting this wrong
    optimistically would mint a fresh mailbox on every deploy."""
    import src.mailer as mailer
    from src.config.settings import get_settings

    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("AGENTMAIL_INBOX_ID", "")
    get_settings.cache_clear()
    mailer.reset_inbox_cache()

    class Existing:
        inbox_id = "inbox_existing"
        email = "to-scale@agentmail.to"
        client_id = mailer._INBOX_CLIENT_ID

    class Listing:
        inboxes = [Existing()]

    sent = {}

    class FakeInboxes:
        def create(self, request=None):
            raise RuntimeError("409 conflict: client_id already in use")

        def list(self):
            return Listing()

        class messages:  # noqa: N801
            @staticmethod
            def send(inbox_id, **kw):
                sent["inbox_id"] = inbox_id

    class FakeClient:
        inboxes = FakeInboxes()

    monkeypatch.setattr(mailer, "_client", lambda: FakeClient())
    assert mailer.send_api_key("a@example.com", "k") is True
    assert sent["inbox_id"] == "inbox_existing"
    get_settings.cache_clear()
    mailer.reset_inbox_cache()


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


# ---------------------------------------------------------------------------
# 4. Contract with the real AgentMail SDK
# ---------------------------------------------------------------------------
# Every test above sends through a fake, which proves the mailer's logic and
# nothing about whether the calls it makes exist. These bind the arguments
# `src/mailer.py` actually passes against the INSTALLED SDK's signatures, so the
# next version that renames a parameter fails here rather than in production on
# the one message a customer is waiting for. No network and no key: binding a
# signature does not call anything.

def test_the_create_inbox_request_matches_the_installed_sdk():
    agentmail = pytest.importorskip("agentmail")
    from agentmail.inboxes import CreateInboxRequest

    import src.mailer as mailer

    req = CreateInboxRequest(
        client_id=mailer._INBOX_CLIENT_ID, display_name="To Scale"
    )
    assert req.client_id == mailer._INBOX_CLIENT_ID
    # The mailer reads `.inbox_id` and `.client_id` off what comes back.
    from agentmail.inboxes import Inbox

    assert "inbox_id" in Inbox.model_fields
    assert "client_id" in Inbox.model_fields
    assert agentmail is not None


def test_the_send_call_matches_the_installed_sdk():
    pytest.importorskip("agentmail")
    import inspect

    from agentmail import AgentMail

    client = AgentMail(api_key="not-used-no-network")
    sig = inspect.signature(client.inboxes.messages.send)
    # Exactly the call src/mailer.py makes.
    sig.bind(
        "inbox_id",
        to="someone@example.com",
        subject="Your To Scale API key",
        text="body",
        reply_to=None,
    )


def test_the_client_accepts_a_timeout():
    """A hung upstream must not hold a worker thread open indefinitely."""
    pytest.importorskip("agentmail")
    import inspect

    from agentmail import AgentMail

    assert "timeout" in inspect.signature(AgentMail.__init__).parameters


def test_an_sdk_error_logs_a_readable_cause():
    """`str(ApiError)` leads with a header dump, which pushes the status and
    message past the end of a truncated log line -- the exact shape of log that
    makes an auth failure look like something else."""
    pytest.importorskip("agentmail")
    from agentmail.core.api_error import ApiError

    from src.mailer import _why

    line = _why(ApiError(
        status_code=401,
        headers={"content-type": "application/json", "x-cache": "Error from cloudfront"},
        body={"message": "invalid api key"},
    ))
    assert line.startswith("HTTP 401")
    assert "invalid api key" in line
    assert "content-type" not in line
    # A plain exception still says something useful.
    assert _why(RuntimeError("boom")).startswith("RuntimeError: boom")
