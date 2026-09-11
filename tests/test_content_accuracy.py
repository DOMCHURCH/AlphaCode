"""No page may publish a numeric accuracy rate.

WHY THIS FILE EXISTS. The site published "99.9% accurate" for a long time, and
the audit in docs/internal/identity-failures.md established that the figure
measured whether a filing balances against ITS OWN stated total -- close to a
self-consistency check -- rather than whether we recovered every component of
it. Those are two different numbers on the same rows (99.8% and 86.84%), and
the flattering one was the one on the homepage under the word "accuracy".

The extraction figure is not published either. It is a temporary engineering
state -- mezzanine tags are mapped but not yet re-ingested -- and printing it
would convert a fixable bug into a permanent marketing claim.

So the rule is: the METHOD is public, the RATE is not. This test is what stops
a percentage drifting back in, which is easy to do by accident because the
figure is still computed and still sitting in `stats.identity()`.

The patterns are matched WITH the percent sign. A bare "99.9" would fire on a
legitimate `$99.9B` in a seeded balance sheet, and a test that cries wolf on
real data gets deleted rather than fixed.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

# The specific figures the audit retired, plus the shape of any new one.
BANNED_EXACT = ("99.9%", "99.8%", "86.84%", "78.6%")
# Any "NN.N% accurate/accuracy" construction, whatever the digits.
BANNED_SHAPE = re.compile(r"\d{1,3}(?:\.\d+)?\s*%\s*(?:accurate|accuracy)", re.I)

PAGES = (
    "/",
    "/pricing",
    "/compare/to-scale-vs-intrinio",
    "/best/sec-filings-api-for-quants",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'content.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.company.lookup import reset_cache
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    reset_cache()
    init_db()

    from src.api import app

    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    reset_engine_cache()


def _seed_balanced() -> None:
    """One company that balances, so the identity block renders at all."""
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, SectorMap, UniverseSnapshot

    q = dt.date(2025, 12, 31)
    with session_scope() as s:
        s.add(UniverseSnapshot(
            as_of_date=dt.date.today(), ticker="AAA", name="Alpha Inc",
            sector="Financials",
        ))
        s.add(SectorMap(ticker="AAA", sector="Financials", sector_source="sic"))
        for metric, value in (
            ("total_assets", 1_000e9),
            ("total_liabilities", 600e9),
            ("total_equity", 400e9),
        ):
            s.add(Fundamental(
                ticker="AAA", metric=metric, value=value, period_end=q,
                fiscal_period="FY", filing_date=dt.date(2026, 2, 13), source="sec",
            ))


@pytest.mark.parametrize("path", PAGES)
def test_no_page_publishes_a_numeric_accuracy_rate(client, path):
    _seed_balanced()
    r = client.get(path)
    assert r.status_code == 200, f"{path} did not render"
    body = r.text

    for banned in BANNED_EXACT:
        assert banned not in body, f"{path} still publishes {banned}"
    match = BANNED_SHAPE.search(body)
    assert match is None, f"{path} publishes an accuracy rate: {match.group(0)!r}"


def test_the_method_is_still_stated_on_the_homepage(client):
    """Removing the number must not remove the claim -- otherwise the page says
    nothing about reconciliation at all, which is a worse outcome than a
    misleading percentage."""
    _seed_balanced()
    body = client.get("/").text
    assert "reconcile" in body.lower()
    assert "assets = liabilities + equity" in body


def test_the_rate_is_still_computed_even_though_it_is_not_shown(client):
    """The figure is not wrong, it is unpublishable. `identity_failures.py` and
    the internal audit both read it, so it must keep working."""
    from src.company.stats import identity

    _seed_balanced()
    ident = identity(max_age_s=0.0)
    assert ident["checkable"] == 1
    assert ident["pass_rate_pct"] == 100.0


def test_the_machine_readable_files_publish_no_rate():
    """llms.txt and financial-data.txt are read by answer engines, which will
    quote a number back with more confidence than the page gave it."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "static_seo"
    for name in ("llms.txt", "financial-data.txt"):
        text = (root / name).read_text(encoding="utf-8")
        for banned in BANNED_EXACT:
            assert banned not in text, f"{name} still publishes {banned}"
        assert "0.999" not in text, f"{name} still publishes 0.999"
        assert "0.786" not in text, f"{name} still publishes 0.786"
