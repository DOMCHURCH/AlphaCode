"""Resend is the preferred sender when RESEND_API_KEY is set; AgentMail is the fallback."""
import httpx

from src import mailer
from src.config.settings import get_settings


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _with_resend(monkeypatch, key="re_test"):
    s = get_settings()
    monkeypatch.setattr(s, "resend_api_key", key, raising=False)
    monkeypatch.setattr(s, "mail_from", "BalanceProof <login@balanceproof.dev>", raising=False)


def test_magic_link_goes_through_resend_from_our_domain(monkeypatch):
    _with_resend(monkeypatch)
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return _Resp(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(mailer, "_client", lambda: (_ for _ in ()).throw(AssertionError("AgentMail used")))
    assert mailer.send_magic_link("user@example.com", "https://balanceproof.dev/x") is True
    assert seen["url"] == "https://api.resend.com/emails"
    assert seen["json"]["from"] == "BalanceProof <login@balanceproof.dev>"
    assert seen["json"]["to"] == ["user@example.com"]
    # Replies go to the public support address, never the owner's personal inbox.
    assert seen["json"]["reply_to"] == "support@balanceproof.dev"
    assert "gmail" not in str(seen["json"]).lower()


def test_resend_rejection_is_a_false_not_an_exception(monkeypatch):
    _with_resend(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(422, "invalid"))
    assert mailer.send_recovery_link("user@example.com", "https://balanceproof.dev/x") is False


def test_without_resend_key_resend_is_skipped(monkeypatch):
    _with_resend(monkeypatch, key="")
    assert mailer._resend_send("user@example.com", subject="s", text="t") is None
