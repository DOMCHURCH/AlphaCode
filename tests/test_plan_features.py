"""Stage A of the paid tiers (2026-09-23): history depth and "what changed".

The plan matrix lives in `src/plans.py`; these tests pin that the gates read
it -- Free gets one year and no changes, Starter gets five years and changes
-- and that a restatement already sitting in `fundamentals` is reported.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

TODAY = dt.date.today()


def _q(years_ago: float) -> dt.date:
    return TODAY - dt.timedelta(days=int(years_ago * 365.25))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    monkeypatch.setenv("SESSION_SECRET", "test-secret-plans")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CALLS", "100")
    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()
    _seed()
    from src.api import _register_gate, app

    _register_gate.reset()
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    get_settings.cache_clear()
    reset_engine_cache()


def _seed():
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    def fact(metric, value, period, filed):
        return Fundamental(ticker="TST", metric=metric, value=value,
                           period_end=period, fiscal_period="Q",
                           filing_date=filed, source="sec")

    with session_scope() as s:
        s.add(UniverseSnapshot(as_of_date=TODAY, ticker="TST", name="Test Co"))
        # Four periods: 0.2, 0.7, 2.5 and 4.5 years ago. A = L + E on each.
        for years, assets in ((0.2, 1000.0), (0.7, 900.0), (2.5, 700.0), (4.5, 500.0)):
            p = _q(years)
            filed = p + dt.timedelta(days=40)
            s.add(fact("total_assets", assets, p, filed))
            s.add(fact("total_liabilities", assets * 0.6, p, filed))
            s.add(fact("total_equity", assets * 0.4, p, filed))
        # The 0.7-year period's liabilities were later RESTATED in the newest
        # filing: 540 originally, 560 revised (equity 360 -> 340).
        p, newest = _q(0.7), _q(0.2) + dt.timedelta(days=40)
        s.add(fact("total_liabilities", 560.0, p, newest))
        s.add(fact("total_equity", 340.0, p, newest))


def _key(client, email, tier=None):
    r = client.post("/api/auth/register", json={"email": email, "accept_terms": True})
    assert r.status_code == 201, r.text
    if tier:
        from src import accounts

        accounts.apply_admin_action(email, f"grant_{tier}")
    return {"X-API-Key": r.json()["api_key"]}


def test_the_plan_list_is_public(client):
    body = client.get("/api/plans").json()
    features = {f["feature"]: f for f in body["features"]}
    assert features["history_years"]["free"] == 1
    assert features["history_years"]["starter"] == 5
    assert features["changes"]["free"] is False
    assert body["monthly_calls"]["free"] == 100


def test_free_history_is_one_year_whatever_is_asked(client):
    h = _key(client, "free@example.com")
    body = client.get("/api/company/TST/history?years=10", headers=h).json()
    assert body["years_allowed"] == 1
    assert body["periods"] == 2  # 0.2 and 0.7 years ago
    assert body["available_from"] == _q(4.5).isoformat()


def test_starter_history_reaches_five_years(client):
    h = _key(client, "starter@example.com", "starter")
    body = client.get("/api/company/TST/history", headers=h).json()
    assert body["years_allowed"] == 5 and body["periods"] == 4
    # Newest first, and each period is the reconciled latest-filing figure.
    ends = [b["period_end"] for b in body["balance_sheets"]]
    assert ends == sorted(ends, reverse=True)


def test_changes_are_refused_on_free_with_the_plan_that_unlocks_them(client):
    h = _key(client, "free2@example.com")
    r = client.get("/api/company/TST/changes", headers=h)
    assert r.status_code == 403
    detail = r.json()["detail"]
    assert detail["error"] == "plan_required" and detail["required_plan"] == "Starter"


def test_changes_report_the_restatement_and_the_period_move(client):
    h = _key(client, "starter2@example.com", "starter")
    body = client.get("/api/company/TST/changes", headers=h).json()
    move = body["since_previous_period"]["total_assets"]
    assert move["previous"] == 900.0 and move["current"] == 1000.0
    assert move["change_pct"] == pytest.approx(11.11, abs=0.01)
    restated = {(r["metric"], r["period_end"]): r for r in body["restatements"]}
    liab = restated[("total_liabilities", _q(0.7).isoformat())]
    assert liab["previous"] == 540.0 and liab["current"] == 560.0
    # Equal comparatives are not restatements.
    assert ("total_assets", _q(0.7).isoformat()) not in restated


def test_the_new_endpoints_spend_one_call_each(client):
    h = _key(client, "meter@example.com", "starter")
    before = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    client.get("/api/company/TST/history", headers=h)
    client.get("/api/company/TST/changes", headers=h)
    after = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    assert after - before == 2


def test_mcp_lists_the_new_tools(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"get_balance_sheet_history", "get_balance_sheet_changes"} <= names


# ---------------------------------------------------------------------------
# Stage B: watchlist + new-filing alerts
# ---------------------------------------------------------------------------

def _file_new_period(assets=1100.0, restate_prev=None):
    """A new 10-Q lands for TST, optionally restating the previous period."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    p, filed = TODAY - dt.timedelta(days=5), TODAY - dt.timedelta(days=1)
    with session_scope() as s:
        for m, v in (("total_assets", assets), ("total_liabilities", assets * .6),
                     ("total_equity", assets * .4)):
            s.add(Fundamental(ticker="TST", metric=m, value=v, period_end=p,
                              fiscal_period="Q", filing_date=filed, source="sec"))
        if restate_prev is not None:
            s.add(Fundamental(ticker="TST", metric="total_liabilities",
                              value=restate_prev, period_end=_q(0.2),
                              fiscal_period="Q", filing_date=filed, source="sec"))


def test_free_cannot_watch(client):
    h = _key(client, "w0@example.com")
    r = client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["required_plan"] == "Starter"


def test_starter_watches_up_to_three(client):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        for t in ("AAA", "BBB", "CCC"):
            s.add(UniverseSnapshot(as_of_date=TODAY, ticker=t, name=t))
            s.add(Fundamental(ticker=t, metric="total_assets", value=1.0,
                              period_end=_q(0.3), fiscal_period="Q",
                              filing_date=_q(0.2), source="sec"))
    h = _key(client, "w1@example.com", "starter")
    for t in ("TST", "AAA", "BBB"):
        assert client.post("/api/watchlist", json={"ticker": t}, headers=h).status_code == 200
    # Idempotent: watching again is not a fourth.
    assert client.post("/api/watchlist", json={"ticker": "TST"}, headers=h).json()["added"] is False
    r = client.post("/api/watchlist", json={"ticker": "CCC"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["error"] == "watchlist_full"
    assert client.post("/api/watchlist", json={"ticker": "NOPE"}, headers=h).status_code == 404
    assert client.delete("/api/watchlist/AAA", headers=h).json()["removed"] is True
    assert len(client.get("/api/watchlist", headers=h).json()["items"]) == 2


def test_a_new_watch_does_not_alert_on_old_filings(client):
    from src import watchlist

    h = _key(client, "w2@example.com", "pro")
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    assert watchlist.pending() == []


def test_a_new_filing_is_mailed_once_with_the_restatement(client, monkeypatch):
    from src import mailer, watchlist

    sent = []
    monkeypatch.setattr(mailer, "_send",
                        lambda to, *, subject, text, event: sent.append((to, subject, text)) or True)
    h = _key(client, "w3@example.com", "pro")
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    _file_new_period(restate_prev=620.0)
    assert len(watchlist.pending()) == 1
    assert watchlist.run_alerts()["sent"] == 1
    (to, subject, text), = sent
    assert to == "w3@example.com" and "TST" in subject
    assert "Reconciles" in text
    assert "Restated: total_liabilities" in text and "600 -> 620" in text
    # Told once: the next run has nothing to say.
    assert watchlist.pending() == []
    assert watchlist.run_alerts()["sent"] == 1 - 1


def test_a_lapsed_watcher_is_advanced_silently(client, monkeypatch):
    from src import accounts, mailer, watchlist

    sent = []
    monkeypatch.setattr(mailer, "_send",
                        lambda to, *, subject, text, event: sent.append(to) or True)
    h = _key(client, "w4@example.com", "starter")
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    accounts.apply_admin_action("w4@example.com", "revoke_pro")
    _file_new_period()
    out = watchlist.run_alerts()
    assert out["skipped"] == 1 and sent == []
    assert watchlist.pending() == []


# ---------------------------------------------------------------------------
# Stage C: bulk verify (Pro+) and filing provenance (Business)
# ---------------------------------------------------------------------------

def _seed_filing_event():
    from src.storage.db import session_scope
    from src.storage.models import FilingEvent

    with session_scope() as s:
        s.add(FilingEvent(ticker="TST", cik="0000123456", form="10-Q",
                          filing_date=_q(0.2) + dt.timedelta(days=40),
                          accession="0000123456-26-000001", primary_doc="tst-10q.htm"))


def test_bulk_verify_needs_pro(client):
    h = _key(client, "v0@example.com", "starter")
    r = client.post("/api/verify", json={"tickers": ["TST"]}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["required_plan"] == "Pro"


def test_bulk_verify_bills_found_tickers_only(client):
    h = _key(client, "v1@example.com", "pro")
    before = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    body = client.post("/api/verify", json={"tickers": ["TST", "NOPE", "tst"]}, headers=h).json()
    assert body["checked"] == 1 and body["not_found"] == 1
    assert body["results"][0]["reconciles"] is True
    assert "provenance" not in body["results"][0]
    after = client.get("/api/user/status", headers=h).json()["calls_used_this_month"]
    assert after - before == 1


def test_bulk_verify_caps_the_batch_and_the_quota(client, monkeypatch):
    from src.config.settings import get_settings

    h = _key(client, "v2@example.com", "pro")
    r = client.post("/api/verify", json={"tickers": [f"T{i}" for i in range(51)]}, headers=h)
    assert r.status_code == 422
    monkeypatch.setenv("PRO_TIER_MONTHLY_CALLS", "2")
    get_settings.cache_clear()
    r = client.post("/api/verify", json={"tickers": ["TST", "AA", "BB"]}, headers=h)
    assert r.status_code == 429


def test_business_gets_filing_links_and_pro_does_not(client):
    _seed_filing_event()
    pro = _key(client, "p1@example.com", "pro")
    biz = _key(client, "b1@example.com", "business")
    assert "provenance" not in client.get("/api/company/TST", headers=pro).json()
    prov = client.get("/api/company/TST", headers=biz).json()["provenance"]
    assert prov["matched"] is True and prov["form"] == "10-Q"
    assert prov["sec_url"].endswith("/tst-10q.htm")
    row = client.post("/api/verify", json={"tickers": ["TST"]}, headers=biz).json()["results"][0]
    assert row["provenance"]["accession"] == "0000123456-26-000001"
    hist = client.get("/api/company/TST/history", headers=biz).json()["balance_sheets"]
    assert hist[0]["provenance"]["matched"] is True
    # An older period with no matching filing event says so instead of guessing.
    assert hist[-1]["provenance"]["matched"] is False


# ---------------------------------------------------------------------------
# Stage D: webhooks (Business)
# ---------------------------------------------------------------------------

@pytest.fixture()
def public_dns(monkeypatch):
    """Every host resolves to a public address unless the test says otherwise."""
    from src import webhooks

    table = {"hooks.example.com": ["93.184.216.34"], "evil.example.com": ["10.0.0.5"],
             "meta.example.com": ["169.254.169.254"], "v6.example.com": ["::1"],
             "mapped.example.com": ["::ffff:10.0.0.5"]}
    monkeypatch.setattr(webhooks, "_resolve", lambda h: table.get(h, ["93.184.216.34"]))
    return table


@pytest.fixture()
def fake_http(monkeypatch):
    import httpx

    calls = []
    state = {"status": 200}

    def post(url, content, headers, timeout, follow_redirects):
        assert follow_redirects is False
        calls.append({"url": url, "body": content, "headers": headers})
        return httpx.Response(state["status"])

    monkeypatch.setattr(httpx, "post", post)
    from src import webhooks

    monkeypatch.setattr(webhooks, "BACKOFF_S", (0.0, 0.0, 0.0))
    return calls, state


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/x", "https://user:pw@hooks.example.com/x",
    "https://hooks.example.com:22/x", "https://evil.example.com/x",
    "https://meta.example.com/x", "https://v6.example.com/x",
    "https://mapped.example.com/x",
])
def test_unsafe_webhook_urls_are_refused(client, public_dns, url):
    h = _key(client, f"u{abs(hash(url))}@example.com", "business")
    assert client.post("/api/webhooks", json={"url": url}, headers=h).status_code == 422


def test_webhooks_need_business(client, public_dns):
    h = _key(client, "wh0@example.com", "pro")
    r = client.post("/api/webhooks", json={"url": "https://hooks.example.com/in"}, headers=h)
    assert r.status_code == 403 and r.json()["detail"]["required_plan"] == "Business"


def test_the_secret_is_shown_once_and_signs_the_ping(client, public_dns, fake_http):
    import hashlib
    import hmac

    calls, _ = fake_http
    h = _key(client, "wh1@example.com", "business")
    made = client.post("/api/webhooks", json={"url": "https://hooks.example.com/in"}, headers=h).json()
    secret = made["secret"]
    listed = client.get("/api/webhooks", headers=h).json()["endpoints"][0]
    assert "secret" not in listed and listed["secret_prefix"] == secret[:8]
    assert client.post(f"/api/webhooks/{made['id']}/test", headers=h).json()["delivered"] is True
    (call,) = calls
    ts, v1 = (kv.split("=", 1)[1] for kv in call["headers"]["BalanceProof-Signature"].split(","))
    expect = hmac.new(secret.encode(), f"{ts}.".encode() + call["body"], hashlib.sha256).hexdigest()
    assert v1 == expect and call["headers"]["BalanceProof-Event"] == "ping"


def test_failing_endpoints_retry_then_switch_off(client, public_dns, fake_http, monkeypatch):
    from src import webhooks

    calls, state = fake_http
    state["status"] = 500
    monkeypatch.setattr(webhooks, "DISABLE_AFTER", 2)
    h = _key(client, "wh2@example.com", "business")
    eid = client.post("/api/webhooks", json={"url": "https://hooks.example.com/in"}, headers=h).json()["id"]
    client.post(f"/api/webhooks/{eid}/test", headers=h)
    assert len(calls) == webhooks.ATTEMPTS
    client.post(f"/api/webhooks/{eid}/test", headers=h)
    ep = client.get("/api/webhooks", headers=h).json()["endpoints"][0]
    assert ep["active"] is False and "disabled" in ep["last_error"]


def test_a_watch_alert_reaches_the_webhook_even_when_email_fails(client, public_dns, fake_http, monkeypatch):
    import json

    from src import mailer, watchlist

    calls, _ = fake_http
    monkeypatch.setattr(mailer, "_send", lambda *a, **k: False)
    h = _key(client, "wh3@example.com", "business")
    client.post("/api/webhooks", json={"url": "https://hooks.example.com/in"}, headers=h)
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    _file_new_period()
    assert watchlist.run_alerts()["sent"] == 1
    body = json.loads(calls[-1]["body"])
    assert body["event"] == "watchlist.filed" and body["data"]["filings"][0]["ticker"] == "TST"
    assert watchlist.pending() == []
