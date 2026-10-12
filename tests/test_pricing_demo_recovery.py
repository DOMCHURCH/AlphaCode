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
    # Recovery mails a SIGN-IN LINK rather than the key, so it needs logins to
    # be enabled as well as mail. Without this the route 503s for the other
    # reason and every recovery assertion here reads as a broken endpoint.
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-not-for-real-use")
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
    html = client.get("/pricing").text  # the cards live here; / carries a summary
    assert "$29" in html and "$49" in html and "$0" in html
    assert "7 keyed API calls a month" in html
    # The quotas left the cards on 2026-09-23 (owner: "less data full") and
    # live in one allowance line under them -- still read from settings.
    assert "Pro 5,000" in html
    assert "10 API calls" not in html


def test_the_home_page_separates_the_snapshot_from_the_live_api(client):
    """The dataset and the API used to read as one product at two prices, so a
    buyer picked on price and got the wrong one. Each card now says what its
    data DOES, and the words are the differentiator rather than decoration."""
    html = client.get("/pricing").text  # the cards live here; / carries a summary

    # Em-dash retired site-wide 2026-09-22. What is guarded here is the
    # photograph-vs-window distinction, not the punctuation: each card must
    # still say, in words, whether its data moves.
    assert "Static snapshot of all company data as of" in html
    assert "One-time download. No updates." in html
    assert "The snapshot never changes" in html
    assert "Live, up-to-date data" in html
    assert "Data updates daily, so you always get the latest filings" in html


def test_the_home_page_offers_the_annual_plan_with_a_computed_saving(client):
    """$49 x 12 - $490 = $98. Worked out from the two prices rather than typed,
    so changing either cannot leave a saving that is no longer true."""
    html = client.get("/pricing").text  # the cards live here; / carries a summary

    assert 'data-plan="pro_annual"' in html
    assert "$490" in html
    assert "save $98" in html


def test_the_demo_blurb_quotes_the_real_daily_limit(client):
    """The env sets 3. A sentence promising five above a counter that stops at
    three is the small kind of lie that costs a reader their trust in the
    numbers this whole site is about."""
    html = client.get("/").text
    assert "3 companies a day from one address" in html
    assert "Five companies a day" not in html


def test_no_daily_cap_reads_as_generous_not_as_a_zero(client, monkeypatch):
    """0 means the DAILY counter is off, which is still the shipped default.

    It must not render as "0 companies a day". It must also not render as "no
    limit" any more: there is an hourly per-address window in front of the
    demo now, so the page says generous instead of unlimited.
    """
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "0")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    html = client.get("/").text

    assert "plenty to evaluate with" in html
    assert "0 companies a day" not in html
    # The claim that is no longer true anywhere on the page.
    assert "no limit on looking" not in html
    assert "uncapped" not in html.lower()


def test_the_demo_serves_more_than_a_handful(client, monkeypatch):
    """The copy and the endpoint must agree. Ten straight lookups from one
    address, where the old daily default refused after five -- and comfortably
    inside the hourly per-address window, which is a hundred."""
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "0")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    seed_jpm()

    for n in range(10):
        r = client.get("/api/demo/JPM")
        assert r.status_code != 429, f"capped after {n} lookups"


def test_the_demo_still_stops_a_loop(client, monkeypatch):
    """The global window is the backstop behind the per-address one.

    Exercised here on its own -- DEMO_RATE_PER_HOUR of 3 with the per-address
    gate untouched -- so a regression that removed the global ceiling would
    still be caught even though the per-address gate would also stop a loop.
    """
    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "0")
    monkeypatch.setenv("DEMO_RATE_PER_HOUR", "3")
    from src.api import _demo_gate
    from src.config.settings import get_settings

    get_settings.cache_clear()
    _demo_gate.reset()
    seed_jpm()

    codes = [client.get("/api/demo/JPM").status_code for _ in range(6)]
    assert 429 in codes, "an uncapped demo with no global gate is a free proxy"


def test_pricing_sits_under_the_explanation(client):
    html = client.get("/").text
    # Method first, then the price. Naming a price before saying where the
    # numbers come from is the wrong order for a reader deciding whether to
    # trust a financial dataset at all.
    #
    # This used to assert on a separate `id="trust"` grid and a `class="acc"`
    # accuracy banner. Both are gone: they and two further sections each told
    # part of the same story, and the duplication was literal -- the SEC
    # duplicate-tag problem was stated three times on one page. They are now
    # one section, `id="how"`, and the ordering rule is unchanged.
    assert html.index('id="how"') < html.index('id="demo"')
    assert html.index('id="demo"') < html.index('id="pricing"')


def test_the_dataset_card_counts_the_rows_it_is_selling(client):
    seed_jpm()
    from src.dataset import reset_count_cache

    reset_count_cache()
    html = client.get("/pricing").text  # the cards live here; / carries a summary
    # The row count moved onto the card's bullets when the cards started
    # saying what each product IS. It is still counted, never written down.
    assert "3 rows as one CSV file" in html


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
        "src.mailer.send_recovery_link",
        lambda e, u, t=15: sent.append((e, u)) or True,
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
    monkeypatch.setattr(
        "src.mailer.send_recovery_link", lambda e, u, t=15: True
    )

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
    assert "support@balanceproof.dev" in r.json()["detail"]


def test_one_address_cannot_be_mailed_repeatedly(client, monkeypatch):
    """A registered address is somebody else's inbox. Without a cooldown the
    recovery button is an email cannon aimed at them."""
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    from src.config.settings import get_settings

    get_settings.cache_clear()
    sent = []
    monkeypatch.setattr(
        "src.mailer.send_recovery_link", lambda e, u, t=15: sent.append(e) or True
    )

    client.post("/api/auth/register", json={"email": "target@example.com", "accept_terms": True})
    for _ in range(4):
        r = client.post(
            "/api/auth/resend-key", json={"email": "target@example.com", "accept_terms": True}
        )
        assert r.status_code == 200  # the reply never changes
    assert len(sent) == 1


def test_the_mail_body_carries_a_link_and_not_a_key(monkeypatch):
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
    assert mailer.send_recovery_link(
        "someone@example.com", "https://balanceproof.dev/auth/verify?token=t0ken", 15
    ) is True

    assert sent["inbox_id"] == "inbox_fixed"
    assert sent["to"] == "someone@example.com"
    assert sent["subject"] == "Recover your BalanceProof API key"
    assert "https://balanceproof.dev/auth/verify?token=t0ken" in sent["text"]
    # The mail that used to go out had a live credential in it, in plain text,
    # in an inbox, forever. It cannot now: there is no key to put in it.
    assert "X-API-Key" not in sent["text"]
    assert sent["reply_to"] == "support@balanceproof.dev"  # never the owner's personal inbox
    get_settings.cache_clear()
    mailer.reset_inbox_cache()


def test_a_failing_upstream_is_a_false_return_not_an_exception(monkeypatch):
    """`send_recovery_link` runs in a background task with nobody to catch it."""
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
    assert mailer.send_recovery_link("someone@example.com", "https://x/y") is False
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
    assert mailer.send_recovery_link("someone@example.com", "https://x/y") is False
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
        email = "balanceproof@agentmail.to"
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

    assert mailer.send_recovery_link("a@example.com", "https://x/y") is True
    assert mailer.send_recovery_link("b@example.com", "https://x/y") is True
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
        email = "balanceproof@agentmail.to"
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
    assert mailer.send_recovery_link("a@example.com", "https://x/y") is True
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
        client_id=mailer._INBOX_CLIENT_ID, display_name="BalanceProof"
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
        subject="Your BalanceProof API key",
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


# ---------------------------------------------------------------------------
# 6. The home page's plan summary and its card
# ---------------------------------------------------------------------------
# The four tests above read /pricing: the plan cards, with their checkout
# buttons, live there now. The home page carries one line per plan.


def _home_pricing(client) -> str:
    html = client.get("/").text
    section = html[html.index('id="pricing"'):]
    return section[: section.index("</section>")]


def test_the_home_page_summarises_the_plans_in_cad_from_settings(client):
    from src.config.settings import get_settings, price_label

    section = _home_pricing(client)
    s = get_settings()

    assert "Canadian dollars" in section and "CAD" in section
    assert 'href="/pricing#compare-plans"' in section
    assert price_label(0) in section
    assert price_label(s.pro_price_usd) in section
    assert price_label(s.dataset_price_usd) in section


def test_the_home_plan_cards_are_the_pricing_pages_cards_with_working_buttons(client, monkeypatch):
    """The same `plan_cards()` block as /pricing, so every Start button carries
    the `data-plan` home.js turns into a checkout, the billing toggle and the
    status note are there, and the dataset and Enterprise actions exist."""
    from src import billing

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    section = _home_pricing(client)

    for plan in ("starter", "pro", "business", "dataset"):
        assert f'class="plan-cta" href="/dashboard#billing" data-plan="{plan}"' in section, plan
    assert 'class="bill-lead"' in section and 'class="bill-opt' in section
    assert 'data-plan="pro_annual"' in section
    assert 'id="plan-note"' in section
    assert "mailto:" in section and "Enterprise%20enquiry" in section
    assert ">Get a free API key<" in section


def test_the_home_cards_sell_only_what_checkout_can_sell(client, monkeypatch):
    from src import billing

    monkeypatch.setattr(billing, "price_for",
                        lambda plan: "" if plan.startswith(("starter", "business")) else "price_test")
    section = _home_pricing(client)
    assert 'data-plan="starter"' not in section and 'data-plan="business"' not in section
    assert ">Enterprise<" in section, "a conversation, always offered"


def _cards(section: str) -> dict[str, str]:
    import re

    out = {}
    for m in re.finditer(r'<span class="plan-name">([A-Za-z ]+?)(?:<span class="badge">[^<]*</span>)?</span>', section):
        name = m.group(1).strip()
        rest = section[m.end():]
        out[name] = rest.split('<span class="plan-name">', 1)[0]
    return out


def test_the_plan_cards_show_a_per_call_price_worked_out_from_settings(client, monkeypatch):
    from src import billing
    from src.accounts import tier_limit
    from src.config.settings import get_settings

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    cards = _cards(_home_pricing(client))
    s = get_settings()

    for name, price in (("Starter", s.starter_price), ("Pro", s.pro_price_usd),
                        ("Business", s.business_price)):
        expected = f"about CA${price / tier_limit(name.lower()):.4f} a call"
        assert expected in cards[name], expected
    assert "a call," not in cards["Free"], "nothing to price on the free tier"


def test_one_line_says_why_pro_costs_more_per_call_than_starter(client, monkeypatch):
    from src import billing

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    section = _home_pricing(client)
    note = section.split('class="psum-note">', 1)[1].split("</p>", 1)[0]

    assert "more per call than Starter" in note
    for feature in ("point-in-time", "failed checks", "alerts on 50 companies"):
        assert feature in note, feature

    monkeypatch.setattr(billing, "price_for", lambda plan: "")
    note = _home_pricing(client).split('class="psum-note">', 1)[1].split("</p>", 1)[0]
    assert "more per call than Starter" not in note, "no comparison with a plan that is not shown"


def test_each_plan_card_lists_its_features_from_the_plan_tables(client, monkeypatch):
    """Two or three bullets a card on the home page, and the numbers in them are
    the matrix's own, so a card cannot promise what the server refuses."""
    import re

    from src import billing, plans

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    cards = _cards(_home_pricing(client))

    assert list(cards) == ["Free", "Starter", "Pro", "Business", "Full dataset", "Enterprise"]
    for name, body in cards.items():
        bullets = re.findall(r"<li>([^<]+)</li>", body.split('<ul class="plan-bullets">', 1)[1].split("</ul>", 1)[0])
        assert 2 <= len(bullets) <= 3, (name, bullets)
    assert f"last {plans.allowance('pro', 'exceptions_feed')} days" in cards["Pro"]
    assert f"Check {plans.allowance('pro', 'bulk_verify')} companies" in cards["Pro"]
    assert f"({plans.allowance('business', 'webhooks')} endpoints)" in cards["Business"]
    assert f"Alerts on {plans.allowance('starter', 'watchlist')} companies" in cards["Starter"]


def test_the_most_popular_badge_is_on_pro_alone(client, monkeypatch):
    from src import billing

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    section = _home_pricing(client)
    assert section.count("Most popular") == 1
    assert '<span class="plan-name">Pro<span class="badge">Most popular</span></span>' in section


def test_pricing_page_cards_are_unchanged_by_the_home_options(client, monkeypatch):
    """/pricing calls plan_cards() with the defaults: no badge, no per-call line,
    and its own longer bullets."""
    from src import billing

    monkeypatch.setattr(billing, "price_for", lambda plan: "price_test")
    page = client.get("/pricing").text
    assert "Most popular" not in page and "plan-pc" not in page
    assert "Data updates daily, so you always get the latest filings" in page


def test_the_home_card_response_is_what_the_demo_endpoint_answers(client):
    """The card's response is built by the call the endpoint makes, and this is
    what stops the two drifting: every value shown must equal the endpoint's."""
    import json
    from html import unescape

    seed_jpm()
    api = client.get("/api/demo/JPM").json()
    html = client.get("/").text

    card = html.split('class="codecard"', 1)[1]
    response = card.split('<pre class="code json"', 1)[1].split("<code>", 1)[1]
    shown = json.loads(unescape(response.split("</code>", 1)[0]))

    assert shown["ticker"] == "JPM"
    for key in ("ticker", "company_name", "period_end", "filing_date",
                "data_quality_issues", "source"):
        assert shown[key] == api[key], key
    for section in ("assets", "liabilities", "equity"):
        for concept, cell in shown[section].items():
            assert cell == api[section][concept], (section, concept)
    # Trimmed, and says so: the caption counts what was left out.
    assert "line items, the ones the check uses" in " ".join(html.split())

# --------------------------------------------------------------- fix pass 3
def test_the_pricing_section_says_what_happens_at_the_call_limit(client):
    """Checked against `enforce_monthly_limit`: a 429 that resets on the 1st
    (UTC), and no code anywhere bills beyond the plan price."""
    from pathlib import Path

    import src.accounts as accounts

    section = _home_pricing(client)
    line = section.split('class="psum-limit">', 1)[1].split("</p>", 1)[0]
    assert "429" in line and "1st (UTC)" in line and "no overage billing" in line

    source = Path(accounts.__file__).read_text(encoding="utf-8")
    assert "status_code=429" in source and "It resets on the 1st (UTC)" in source
    assert "overage" not in source.lower()


def test_a_short_plan_faq_answers_the_four_questions(client):
    html = client.get("/").text
    faq = html.split('id="plan-faq"', 1)[1].split("</section>", 1)[0]
    flat = " ".join(faq.split())

    assert flat.count("<details") == 4
    for question in ("Do I need a card for the Free plan?", "Can I cancel any time?",
                     "What counts as a call?", "What happens when I go over the limit?"):
        assert question in flat, question
    assert "no card and no password" in flat
    assert "Manage subscription" in flat and "14-day refund window" in flat
    assert "HTTP 429" in flat and "no overage billing" in flat
    # ordered after the plans and before the founder note
    assert html.index('id="pricing"') < html.index('id="plan-faq"') < html.index('id="why"')


def test_the_header_is_solid_and_the_phone_menu_is_its_own_opaque_sheet():
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    block = css.split("Fix pass: opaque header", 1)[1]
    head = block.split("---- the strip", 1)[0]
    assert "background: #06070e;" in head and "backdrop-filter: none;" in head
    assert "rgba" not in head.split(".nav-js .navlinks", 1)[0].split("nav,", 1)[1]
    assert "background: #0b0c14;" in head


def test_every_strip_figure_is_one_size_on_one_baseline():
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    block = css.split("Fix pass: opaque header", 1)[1].split("loose text over the glow", 1)[0]
    assert 'body[data-bd="fluted"] .scell.key .sv { font-size: clamp(34px, 3.6vw, 48px); }' in block
    # the subgrid rows that put every value on one baseline are still in force
    assert "align-self: baseline" in css


def test_long_prose_over_the_glow_is_held_by_a_plate_or_a_width(client):
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    block = css.split("loose text over the glow sits on a plate", 1)[1]
    assert ".company-intro::before" in block and "rgba(5, 6, 10, .78)" in block
    assert ".company-intro { max-width: 56rem; }" in block
    assert "backdrop-filter" not in block.split("the code card", 1)[0], "a plate, not a blur"


def test_the_code_card_wraps_at_spaces_and_scrolls_rather_than_split_a_url(client):
    from pathlib import Path

    import src.report.home_page as hp

    css = (Path(hp.__file__).parent / "static" / "glass.css").read_text(encoding="utf-8")
    block = css.split("the code card: break only where a person would", 1)[1]
    assert "overflow-wrap: normal;" in block and "word-break: normal;" in block
    assert "overflow-x: auto;" in block
