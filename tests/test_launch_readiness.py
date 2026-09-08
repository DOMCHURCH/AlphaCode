"""What has to be true before the site is pointed at the public.

Three groups, and each of them is a bug that actually happened rather than a
box being ticked:

* **A signed-in reader can buy.** The pricing buttons used to link to /login
  unconditionally, so pressing "Buy the data" while signed in read as being
  signed out at the exact moment somebody was trying to pay. Every assertion
  here is about the session surviving from the page render to the Stripe
  redirect -- and about a lapsed session saying so instead of bouncing.
* **Every page says what it is.** One shell, so a canonical tag, a social card
  and a robots directive cannot be on the home page and missing from the rest.
  The private pages carry `noindex`, and that is asserted per page rather than
  once, because the failure mode is one page quietly not going through the
  shell.
* **Every page has the backdrop.** It was on the home page and nowhere else.

They share one client because the interesting cases need a session AND Stripe
configured at the same time -- which is exactly the combination the bug lived
in.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

SESSION_SECRET = "test-session-secret-do-not-use"
WEBHOOK_SECRET = "whsec_test_do_not_use"
PRICE_PRO = "price_test_pro"
PRICE_DATASET = "price_test_dataset"
SITE = "https://toscale.pro"
USER = "reader@example.com"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "launch.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("BASE_URL", "https://example.test")
    monkeypatch.setenv("SITE_URL", SITE)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "am-test-key")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_do_not_use")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("STRIPE_PRICE_PRO", PRICE_PRO)
    monkeypatch.setenv("STRIPE_PRICE_DATASET", PRICE_DATASET)
    monkeypatch.setenv("DEMO_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src import auth
    from src.api import _checkout_gate, _magic_link_gate, _register_gate, app

    _register_gate.reset()
    _magic_link_gate.reset()
    _checkout_gate.reset()
    auth.reset_cooldowns()
    monkeypatch.setattr("src.mailer.send_magic_link", lambda *a, **k: True)

    # https, because the session cookie is Secure on any deployment whose
    # BASE_URL is https -- and a TestClient on http would silently drop it.
    with TestClient(app, base_url="https://testserver") as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


@pytest.fixture()
def stripe_calls(monkeypatch):
    """The one call that leaves the process, replaced. Returns what was sent."""
    import stripe

    made: list[dict] = []

    def _create(**params):
        made.append(params)
        return {
            "id": f"cs_test_{len(made)}",
            "url": f"https://checkout.stripe.com/c/pay/cs_test_{len(made)}",
        }

    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(_create))
    return made


def sign_in(client, email: str = USER) -> None:
    """Take a real magic link all the way through, so the cookie is a real one."""
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import MagicLink

    r = client.post(
        "/api/auth/magic-link", json={"email": email, "accept_terms": True}
    )
    assert r.status_code == 200, r.text
    with session_scope() as s:
        token = s.execute(
            select(MagicLink)
            .where(MagicLink.email == email)
            .order_by(MagicLink.created_at.desc())
        ).scalars().first().token
    r = client.post("/api/auth/verify", json={"token": token})
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# The session, from the page to Stripe
# ---------------------------------------------------------------------------

def test_me_returns_the_account_while_signed_in(client):
    """The dashboard reads its own key off this. If it 401s, the page falls
    back to "paste a key" and everything downstream looks logged out."""
    sign_in(client)

    r = client.get("/api/auth/me")

    assert r.status_code == 200, r.text
    assert r.json()["email"] == USER
    assert r.json()["signed_in"] is True
    assert r.json()["api_key"]


def test_a_signed_in_reader_gets_buy_buttons_not_a_login_link(client):
    """The bug, at the place it started: the pricing cards.

    Signed out, "Buy the data" has to ask for an account first -- a purchase
    lands on one. Signed in, it must not, because being sent to a login form
    while signed in is indistinguishable from having been logged out.
    """
    out = client.get("/").text
    assert 'action="/checkout"' not in out
    assert out.count('class="plan-cta" href="/login"') == 3

    sign_in(client)
    page = client.get("/").text

    # Both paid plans post to the checkout route; the free tier still asks for
    # an account, because there is nothing to buy.
    assert page.count('action="/checkout"') == 2
    assert 'value="dataset"' in page
    assert 'value="pro"' in page
    assert page.count('class="plan-cta" href="/login"') == 1


def test_the_checkout_form_sends_a_signed_in_reader_to_stripe(client, stripe_calls):
    sign_in(client)

    r = client.post(
        "/checkout", content="plan=dataset",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"].startswith("https://checkout.stripe.com/")
    # The address came from the cookie, not from anything posted.
    assert stripe_calls[0]["customer_email"] == USER
    assert stripe_calls[0]["metadata"]["plan"] == "dataset"


def test_a_lapsed_session_is_an_error_page_never_a_redirect_to_login(
    client, stripe_calls
):
    """The other half of the same bug.

    A missing cookie here means the session lapsed between the page render and
    the click. Bouncing to /login would say "you are not signed in" and nothing
    about the purchase; the reader is left unable to tell that from a broken
    button. So: a page, a 401, and a sentence.
    """
    r = client.post(
        "/checkout", content="plan=pro",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert r.status_code == 401
    assert "session has expired" in r.text
    assert 'href="/login"' in r.text          # offered, not forced
    assert stripe_calls == []                 # and nothing was opened


def test_the_json_checkout_prefers_the_session_over_whatever_was_posted(
    client, stripe_calls
):
    """The cookie is proof of the address; the body is a claim about one."""
    sign_in(client)

    r = client.post(
        "/api/billing/checkout",
        json={"plan": "pro", "email": "someone.else@example.com"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    assert stripe_calls[0]["customer_email"] == USER


def test_session_required_turns_a_missing_cookie_into_a_plain_401(
    client, stripe_calls
):
    """What the dashboard's buttons send. Without it the checkout would open
    anonymously and Stripe would ask a signed-in reader who they are."""
    r = client.post(
        "/api/billing/checkout", json={"plan": "pro", "session_required": True}
    )

    assert r.status_code == 401
    assert "session has expired" in r.json()["detail"]
    assert stripe_calls == []


def test_an_anonymous_checkout_still_works(client, stripe_calls):
    """Unchanged, and deliberately: somebody can buy before they have an
    account, and the webhook makes one when the payment settles."""
    r = client.post(
        "/api/billing/checkout", json={"plan": "dataset", "email": "new@example.com"}
    )

    assert r.status_code == 200, r.text
    assert stripe_calls[0]["customer_email"] == "new@example.com"


# ---------------------------------------------------------------------------
# What every page has to say about itself
# ---------------------------------------------------------------------------

PUBLIC_PAGES = ["/", "/api", "/terms", "/privacy"]
PRIVATE_PAGES = ["/login", "/dashboard"]
ALL_PAGES = PUBLIC_PAGES + PRIVATE_PAGES


@pytest.mark.parametrize("path", ALL_PAGES)
def test_every_page_carries_the_full_head(client, path):
    body = client.get(path).text

    assert '<meta charset="utf-8">' in body
    assert 'name="viewport" content="width=device-width, initial-scale=1.0"' in body
    assert re.search(r"<title>.+</title>", body)
    assert 'name="description"' in body
    assert 'name="keywords"' in body
    assert 'name="theme-color" content="#0a0a0a"' in body
    assert f'<link rel="canonical" href="{SITE}{path}">' in body
    assert 'rel="apple-touch-icon"' in body


@pytest.mark.parametrize("path", ALL_PAGES)
def test_every_page_carries_a_social_card(client, path):
    body = client.get(path).text

    assert 'property="og:title"' in body
    assert 'property="og:description"' in body
    assert 'property="og:type"' in body
    assert f'property="og:url" content="{SITE}{path}"' in body
    # Absolute, because a relative og:image is dropped by every scraper.
    assert f'property="og:image" content="{SITE}/static/og-image.png"' in body
    assert 'name="twitter:card" content="summary_large_image"' in body
    assert 'name="twitter:title"' in body
    assert 'name="twitter:image"' in body


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_are_indexable(client, path):
    assert 'name="robots" content="index, follow"' in client.get(path).text


@pytest.mark.parametrize("path", PRIVATE_PAGES)
def test_private_pages_are_not_indexable(client, path):
    """A sign-in form or somebody's account page in a search result is a result
    nobody wanted, and it competes with the page that should have ranked."""
    assert 'name="robots" content="noindex, nofollow"' in client.get(path).text


def test_the_home_page_carries_its_structured_data(client):
    body = client.get("/").text

    blocks = [
        json.loads(m)
        for m in re.findall(
            r'<script type="application/ld\+json">\s*(.+?)\s*</script>',
            body,
            re.S,
        )
    ]
    by_type = {b["@type"]: b for b in blocks}
    assert set(by_type) == {"WebApplication", "Organization", "Product"}

    app_block = by_type["WebApplication"]
    assert app_block["url"] == f"{SITE}/"
    assert app_block["applicationCategory"] == "FinancialApplication"
    assert app_block["offers"]["price"] == "0"

    assert by_type["Organization"]["founder"]["name"] == "Dominique Church"
    assert by_type["Organization"]["logo"] == f"{SITE}/static/logo.png"

    # The price is read from settings, never written into the copy: a rich
    # result quoting a price Stripe does not charge is a claim Google will
    # happily show for months.
    from src.config.settings import get_settings

    offer = by_type["Product"]["offers"]
    assert offer["price"] == str(get_settings().pro_price_usd)
    assert offer["priceCurrency"] == "USD"


def test_structured_data_cannot_close_its_own_script_tag(client):
    """`</script>` inside a JSON string would end the element and turn the rest
    of the document into text."""
    body = client.get("/").text
    for block in re.findall(
        r'<script type="application/ld\+json">(.+?)</script>', body, re.S
    ):
        assert "<" not in block


# ---------------------------------------------------------------------------
# The backdrop
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_PAGES + ["/company/NOPE"])
def test_every_page_has_the_backdrop(client, path):
    """It used to be the home page and nowhere else, which made the site look
    like two sites."""
    body = client.get(path).text

    assert '<div class="backdrop" id="backdrop"' in body
    assert 'class="backdrop-still"' in body
    assert 'class="backdrop-veil"' in body
    assert "/static/backdrop.js" in body
    assert "/static/backdrop.css" in body


@pytest.mark.parametrize(
    "path,film",
    [
        ("/", "hero"),          # glanced at, so it can afford a sunset
        ("/login", "hero"),
        ("/api", "still"),
        ("/dashboard", "calm"),  # figures read line by line
        ("/company/NOPE", "calm"),
        ("/terms", "quiet"),     # a thousand words somebody actually reads
        ("/privacy", "quiet"),
    ],
)
def test_the_veil_is_chosen_per_page(client, path, film):
    """How much backdrop gets through is a readability question, and the answer
    genuinely differs per page. The weight itself lives in backdrop.css; this
    is the attribute it keys off."""
    assert f'<body data-film="{film}">' in client.get(path).text


# ---------------------------------------------------------------------------
# The rest of the launch checklist
# ---------------------------------------------------------------------------

def test_the_marks_are_all_served(client):
    for path, kind in (
        ("/favicon.ico", "image/svg+xml"),
        ("/apple-touch-icon.png", "image/png"),
        ("/static/favicon.ico", None),
        ("/static/og-image.png", "image/png"),
        ("/static/apple-touch-icon.png", "image/png"),
        ("/static/logo.png", "image/png"),
    ):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.content, path
        if kind:
            assert r.headers["content-type"].startswith(kind), path


def test_humans_txt_names_a_person_and_the_stack(client):
    r = client.get("/humans.txt")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "Dominique Church" in r.text
    assert "FastAPI" in r.text


def test_static_assets_are_cached_and_a_versioned_one_is_cached_hard(client):
    """A stamped URL names one build of one file, so it can be kept for a year.
    An unstamped one will mean something else after the next deploy."""
    plain = client.get("/static/company.css")
    stamped = client.get("/static/company.css?v=12345")

    assert plain.headers["cache-control"] == "public, max-age=3600"
    assert stamped.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_a_browser_gets_a_404_page_and_a_client_gets_json(client):
    """The switch is an explicit `text/html`, never the `*/*` that curl and
    every API client send -- answering a machine with markup because it did not
    object is how a JSON client ends up parsing a stylesheet link."""
    page = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert page.status_code == 404
    assert "There is nothing at this address" in page.text
    assert 'name="robots" content="noindex, nofollow"' in page.text

    machine = client.get("/no-such-page", headers={"Accept": "*/*"})
    assert machine.status_code == 404
    assert machine.json()["detail"]


def test_robots_keeps_crawlers_off_the_private_surfaces(client):
    """`noindex` keeps a fetched page out of the index; this stops the fetch.
    Both, because they answer different halves of the question."""
    body = client.get("/robots.txt").text

    for path in ("/dashboard", "/login", "/auth/", "/search", "/checkout"):
        assert f"Disallow: {path}" in body
    assert "Sitemap:" in body


def test_the_api_reference_examples_are_https(client):
    """They are read now and pasted later. `http://` in one is a command that
    answers with a redirect instead of with JSON."""
    body = client.get("/api").text

    assert "https://testserver/api/company/JPM" in body
    assert "http://testserver" not in body
