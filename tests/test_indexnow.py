"""IndexNow key file, URL list, and that tests never ping a real engine."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_key_file_is_served_at_its_own_path():
    from src.api import app
    from src.indexnow import KEY, key_file_path

    r = TestClient(app).get(key_file_path())
    assert r.status_code == 200 and r.text == KEY


def test_core_urls_cover_the_new_pages():
    from src.indexnow import core_urls

    urls = core_urls()
    assert "https://balanceproof.dev/restatements" in urls
    assert "https://balanceproof.dev/blog/point-in-time-fundamentals-sec-edgar" in urls
    assert all(u.startswith("https://balanceproof.dev/") for u in urls)


def test_warm_does_nothing_outside_production(monkeypatch):
    import src.indexnow as ix

    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    called = []
    monkeypatch.setattr(ix, "submit", lambda *a, **k: called.append(1))
    ix.warm()
    assert called == []
