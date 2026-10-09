"""The live facts section of a company page.

What is defended here: the section is built from the SAME view the drawing is
drawn from, so the paragraph can no longer say one quarter while the chart says
another; the arithmetic line is the check that was actually run; and every
number is one that is in `fundamentals`. The figures below are test fixtures,
not filed data.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from src.report import company_facts as cf

P1 = dt.date(2025, 12, 31)   # newest
P2 = dt.date(2025, 9, 30)
P3 = dt.date(2025, 6, 30)
F1 = dt.date(2026, 2, 13)
F2 = dt.date(2025, 11, 7)
F3 = dt.date(2025, 8, 8)


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'facts.db'}")
    get_settings.cache_clear()
    reset_engine_cache()
    init_db()
    cf.clear_cache()
    try:
        yield
    finally:
        cf.clear_cache()
        get_settings.cache_clear()
        reset_engine_cache()


def _add(ticker, metric, value, period, filed):
    from src.storage.db import session_scope
    from src.storage.models import Fundamental

    with session_scope() as s:
        s.add(Fundamental(
            ticker=ticker, metric=metric, value=float(value), period_end=period,
            fiscal_period="Q", filing_date=filed, source="sec", restated=False,
            source_tag=f"us-gaap:{metric}",
        ))


def _sheet(ticker, period, filed, assets, liab, equity, cash=None):
    _add(ticker, "total_assets", assets, period, filed)
    _add(ticker, "total_liabilities", liab, period, filed)
    _add(ticker, "total_equity", equity, period, filed)
    if cash is not None:
        _add(ticker, "cash", cash, period, filed)


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------
class _B:
    def __init__(self, label, value, kind="asset", rem=False):
        self.label, self.value, self.kind, self.is_remainder = label, value, kind, rem


class _V:
    ticker = "ZZZ"
    company_name = "Zed Corp"
    sector = "Technology"
    total_assets = 1000.0
    total_liabilities = 600.0
    total_equity = 400.0
    identity_basis = ""
    minority_interest = None
    mezzanine = None
    liabilities_derived_from = None
    assets = [_B("Cash", 300.0), _B("Other assets", 500.0, rem=True)]
    claims = [_B("Long-term debt", 450.0, "liability"),
              _B("Shareholders' equity", 400.0, "equity")]


def test_arithmetic_line_shows_the_zero_in_whole_dollars():
    line = cf.arithmetic_line(_V)
    assert line == (
        "Assets $1,000 − (liabilities $600 + equity $400) = $0"
    )


def test_arithmetic_line_names_the_extra_term_the_identity_needed():
    class V(_V):
        total_liabilities = 500.0
        identity_basis = "nci"
        minority_interest = 100.0

    line = cf.arithmetic_line(V)
    assert "noncontrolling interest $100" in line
    assert line.endswith("= $0")


def test_arithmetic_line_is_absent_when_a_term_is_missing():
    class V(_V):
        total_liabilities = None

    assert cf.arithmetic_line(V) == ""


def test_a_derived_liability_is_called_arithmetic_not_a_check():
    class V(_V):
        liabilities_derived_from = "total assets, less equity"

    html = cf.build_facts_html(V, [], "technology")
    assert "true by construction" in html


def test_ratios_exclude_the_remainder_when_naming_the_largest_line():
    rows = {label: result for label, _w, result in cf.ratio_rows(_V)}
    assert rows["Liabilities to assets"] == "60.0%"
    assert rows["Equity to assets"] == "40.0%"
    assert rows["Liabilities to equity"] == "1.50×"
    # "Other assets" is 500 and bigger than Cash, but it is our remainder.
    assert rows["Largest asset line: Cash"] == "30.0%"
    assert rows["Largest liability line: Long-term debt"] == "45.0%"


def test_no_liabilities_to_equity_ratio_for_negative_equity():
    class V(_V):
        total_liabilities = 1200.0
        total_equity = -200.0

    labels = [r[0] for r in cf.ratio_rows(V)]
    assert "Liabilities to equity" not in labels
    assert "Liabilities to assets" in labels


def test_balance_verdict_does_not_blame_the_filer():
    assert cf.balance_verdict({"balances": True, "basis": "as_filed"}) == "Balanced"
    assert "noncontrolling" in cf.balance_verdict(
        {"balances": True, "basis": "nci"})
    out = cf.balance_verdict(
        {"balances": False, "basis": "", "imbalance_pct": 3.24})
    assert out == "Does not balance (off by 3.2%)"


def test_a_row_whose_filer_totals_agree_never_says_does_not_balance():
    """GYRE's shape: the page said "balances" in the paragraph and "does not
    balance" in the table beside it."""
    out = cf.balance_verdict({
        "balances": False, "basis": "", "imbalance_pct": 11.257,
        "stated_agrees": True,
    })
    assert "Does not balance" not in out
    assert "11.3%" in out and "own totals agree" in out


def test_stated_total_note_names_who_is_short():
    class Ours(_V):
        total_liabilities = 500.0           # 100 short of the filer's 1,000
        stated_rhs = 1000.0

    class Theirs(_V):
        total_liabilities = 500.0
        stated_rhs = 800.0

    assert "balances against its own arithmetic" in cf._stated_note(Ours)
    assert "disagreement is in the filing's own totals" in cf._stated_note(Theirs)
    # Nothing to referee when the line is already zero.
    assert cf._stated_note(_V) == ""


def test_hubs_link_to_the_sector_and_the_directories():
    html = cf.hubs_html(_V, "technology")
    for href in ("/sector/technology", "/companies", "/methodology"):
        assert f'href="{href}"' in html
    # The restatement detector is not audited; company pages do not point at it.
    assert "/restatements" not in html


# ---------------------------------------------------------------------------
# Against a database
# ---------------------------------------------------------------------------
def test_history_judges_each_period_and_the_first_row_is_the_drawing(db):
    from src.company.view1 import build_view1

    _sheet("HIST", P1, F1, 1000, 600, 400)
    _sheet("HIST", P2, F2, 900, 500, 400)
    _sheet("HIST", P3, F3, 800, 500, 200)        # 700 against 800: does not balance
    view = build_view1("HIST")
    rows = cf._history_rows(view, "HIST")
    assert [r["period_end"] for r in rows] == [
        "2025-12-31", "2025-09-30", "2025-06-30"]
    assert [r["balances"] for r in rows] == [True, True, False]
    assert rows[0]["assets"] == view.total_assets
    assert rows[2]["imbalance_pct"] == pytest.approx(12.5)


def test_the_intro_is_built_from_the_drawn_period_not_a_stored_one(db):
    """The AAPL bug: a stored paragraph from one quarter beside a chart from
    the next. Here nothing is stored, so the paragraph can only be the chart's."""
    from src.company.view1 import build_view1

    _sheet("LIVE", P1, F1, 1000, 600, 400)
    _sheet("LIVE", P2, F2, 800, 500, 300)
    view = build_view1("LIVE")
    intro, facts = cf.render_facts(view)
    text = _text(intro)
    assert "total assets at $1,000" in text
    assert "as at 2025-12-31" in text
    assert "grew 25.0%" in text and "2025-09-30" in text
    assert "Assets $1,000 − (liabilities $600 + equity $400) = $0" in _text(facts)


def test_a_new_filing_changes_the_output_without_any_rebuild(db):
    from src.company.view1 import build_view1

    _sheet("MOVE", P2, F2, 800, 500, 300)
    first, _ = cf.render_facts(build_view1("MOVE"))
    assert "as at 2025-09-30" in _text(first)
    _sheet("MOVE", P1, F1, 1000, 600, 400)
    second, _ = cf.render_facts(build_view1("MOVE"))
    assert "as at 2025-12-31" in _text(second)


def test_the_page_does_not_claim_anything_about_restatements(db):
    """The detector behind /restatements is not audited, so company pages say
    neither "restated" nor "none restated"."""
    from src.company.view1 import build_view1

    _add("RST", "total_assets", 1000, P2, F2)
    _add("RST", "total_assets", 1100, P2, F1)     # a later filing differs
    _add("RST", "total_liabilities", 600, P2, F2)
    _add("RST", "total_equity", 500, P2, F2)
    _sheet("RST", P1, F1, 1200, 700, 500)
    intro, facts = cf.render_facts(build_view1("RST"))
    assert "estated" not in _text(facts)
    assert "estated" not in _text(intro)


def test_a_failure_returns_none_so_the_stored_intro_survives(db, monkeypatch):
    from src.company.view1 import build_view1

    _sheet("BOOM", P1, F1, 1000, 600, 400)
    view = build_view1("BOOM")

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(cf, "_history_rows", boom)
    assert cf.render_facts(view) is None
