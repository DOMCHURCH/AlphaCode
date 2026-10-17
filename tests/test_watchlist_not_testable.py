"""A watch alert never says a filing reconciles when it could not be tested.

The alert still goes out: it announces a new filing, and the reconciliation
line is one part of it. What changed is only what that line says, and that the
webhook payload carries `reconciles: null` and `identity_status`, as
`/api/verify` does.
"""

from __future__ import annotations

import datetime as dt
import json

from tests.test_plan_features import (  # noqa: F401
    TODAY,
    _file_new_period,
    _key,
    client,
    fake_http,
    public_dns,
)


def _file_assets_only():
    """A new 10-Q for TST that reports total assets and nothing to check them against."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        s.add(Fundamental(
            ticker="TST", metric="total_assets", value=1100.0,
            period_end=TODAY - dt.timedelta(days=5), fiscal_period="Q",
            filing_date=TODAY - dt.timedelta(days=1), source="sec",
        ))


def test_the_email_for_an_untestable_filing_says_so_and_is_still_sent(client, monkeypatch):
    from src import mailer, watchlist

    sent = []
    monkeypatch.setattr(mailer, "_send",
                        lambda to, *, subject, text, event: sent.append(text) or True)
    h = _key(client, "wn1@example.com", "pro")
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    _file_assets_only()

    assert watchlist.run_alerts()["sent"] == 1

    (text,) = sent
    assert "Not testable" in text
    assert "Reconciles" not in text
    assert "Does NOT reconcile" not in text


def test_the_webhook_for_an_untestable_filing_sends_null_and_the_status(
        client, public_dns, fake_http, monkeypatch):
    from src import mailer, watchlist

    calls, _ = fake_http
    monkeypatch.setattr(mailer, "_send", lambda *a, **k: False)
    h = _key(client, "wn2@example.com", "business")
    client.post("/api/webhooks", json={"url": "https://hooks.example.com/in"}, headers=h)
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    _file_assets_only()

    assert watchlist.run_alerts()["sent"] == 1

    filing = json.loads(calls[-1]["body"])["data"]["filings"][0]
    assert filing["reconciles"] is None
    assert filing["identity_status"] == "not_testable"
    assert filing["imbalance_pct"] is None


def test_an_ordinary_filing_still_says_reconciles(client, monkeypatch):
    from src import mailer, watchlist

    sent = []
    monkeypatch.setattr(mailer, "_send",
                        lambda to, *, subject, text, event: sent.append(text) or True)
    h = _key(client, "wn3@example.com", "pro")
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    _file_new_period()

    assert watchlist.run_alerts()["sent"] == 1

    assert "Reconciles: Assets = Liabilities + Equity holds" in sent[0]
    assert "Not testable" not in sent[0]


def test_an_ordinary_webhook_payload_carries_true_and_the_status(
        client, public_dns, fake_http, monkeypatch):
    from src import mailer, watchlist

    calls, _ = fake_http
    monkeypatch.setattr(mailer, "_send", lambda *a, **k: False)
    h = _key(client, "wn4@example.com", "business")
    client.post("/api/webhooks", json={"url": "https://hooks.example.com/in"}, headers=h)
    client.post("/api/watchlist", json={"ticker": "TST"}, headers=h)
    _file_new_period()

    watchlist.run_alerts()

    filing = json.loads(calls[-1]["body"])["data"]["filings"][0]
    assert filing["reconciles"] is True
    assert filing["identity_status"] == "balanced"
