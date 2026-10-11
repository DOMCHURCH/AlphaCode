"""The /api page names the keyless demo's allowance from the limit in force.

It said "Five a day" while its own endpoint list said 100 an hour. The daily
counter defaults to off (0); the hourly per-address window is what limits a
caller, and a deployment that turns the daily one on is told so.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.report.api_page import demo_allowance_phrase


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api_demo.db'}")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    from src.api import app

    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_phrase_prefers_the_daily_quota_when_it_is_on():
    assert demo_allowance_phrase(100, 0) == "100 an hour"
    assert demo_allowance_phrase(100, 25) == "25 a day"
    assert demo_allowance_phrase(0, 0) == "Rate limited"


def test_page_says_what_the_endpoint_list_says(client):
    text = client.get("/api").text
    assert "100 an hour from any one address" in text
    assert "No key · 100/hour per address" in text
    assert "Five a day" not in text


def test_page_names_the_daily_quota_when_a_deployment_turns_it_on(client, monkeypatch):
    from src.config.settings import get_settings

    monkeypatch.setenv("DEMO_CALLS_PER_IP_PER_DAY", "25")
    get_settings.cache_clear()
    assert "25 a day from any one address" in client.get("/api").text
