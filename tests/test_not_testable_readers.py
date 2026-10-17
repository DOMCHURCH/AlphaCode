"""Everything that shows or sends a verdict reads the tri-state, not `balances`.

`View1.balances` is the drawing's flag and is True for a filing with nothing to
test. Each reader below used to take it as an answer: the company page said
"reconciles directly" and "Verified against A = L + E", the history table said
"Balanced", the dashboard said "Yes". They now read `view.reconciles`
(True / False / None) and `identity_status`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_check_not_testable import _seed, client  # noqa: F401

UNTESTABLE = {
    "ONLYA": {"total_assets": 1000.0},
    "ALONLY": {"total_assets": 1000.0, "total_liabilities": 600.0},
    "AEONLY": {"total_assets": 1000.0, "total_equity": 400.0},
}
FULL = {"total_assets": 1000.0, "total_liabilities": 600.0, "total_equity": 400.0}


def _view(ticker: str):
    from src.company.view1 import build_view1

    view = build_view1(ticker)
    assert view is not None
    return view


# ---------------------------------------------------------------------------
# The tri-state itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticker", list(UNTESTABLE))
def test_the_view_says_none_for_a_filing_that_cannot_be_tested(client, ticker):
    _seed(ticker, **UNTESTABLE[ticker])
    view = _view(ticker)

    assert view.reconciles is None
    assert view.as_dict()["reconciles"] is None
    assert any(n.startswith("Not testable: ") for n in view.notes)


def test_the_view_says_true_for_a_filing_that_balances(client):
    _seed("FULLY", **FULL)

    view = _view("FULLY")

    assert view.reconciles is True
    assert not any(n.startswith("Not testable") for n in view.notes)


# ---------------------------------------------------------------------------
# Company page
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticker", list(UNTESTABLE))
def test_the_company_page_never_says_an_untestable_filing_was_checked(client, ticker):
    _seed(ticker, **UNTESTABLE[ticker])

    html = client.get(f"/company/{ticker}").text

    assert "Checked against A = L + E" not in html
    assert "Checked: A = L + E" not in html
    assert "The check, written out" not in html
    assert "Not testable: " in html
    assert 'class="note warn"' not in html  # not a failure either


def test_the_company_page_still_says_it_for_a_filing_that_was(client):
    _seed("FULLY", **FULL)

    html = client.get("/company/FULLY").text

    assert "Checked against A = L + E" in html or "Checked: A = L + E" in html
    assert "The check, written out" in html
    assert "Not testable" not in html


def test_the_snippet_description_drops_the_claim_when_all_three_figures_are_derived(client):
    """A + E reported: liabilities are derived, so all three figures are present
    and the snippet used to say 'Checked: A = L + E'."""
    from src.report.company_page import _company_meta

    _seed("AEONLY", **UNTESTABLE["AEONLY"])
    _seed("FULLY", **FULL)

    untested = _company_meta(_view("AEONLY").as_dict())
    tested = _company_meta(_view("FULLY").as_dict())

    assert "Checked: A = L + E" not in untested and "Checked against" not in untested
    assert "Checked: A = L + E" in tested


def test_the_opening_paragraph_does_not_say_it_reconciles_directly():
    from src.report.page_extras import build_intro

    kwargs = dict(ticker="X", company_name="X Inc", sector="Industrials",
                  assets=100.0, liabilities=60.0, equity=40.0, balances=True)

    untested = build_intro(identity_status="not_testable", **kwargs)
    ordinary = build_intro(**kwargs)

    assert "reconciles directly" not in untested
    assert "marked not testable instead of passing" in untested
    assert "reconciles directly" in ordinary  # unchanged for everything else


# ---------------------------------------------------------------------------
# History table, verdict words, landing sample
# ---------------------------------------------------------------------------


def test_the_verdict_for_none_is_not_testable():
    from src.report.company_facts import balance_verdict

    assert balance_verdict({"balances": None}) == "Not testable"
    assert balance_verdict({"balances": True}) == "Balanced"


def test_the_history_table_counts_only_tested_sheets_and_names_the_rest():
    from src.report.company_facts import history_html

    def row(period, balances):
        return {"period_end": period, "filing_date": period, "assets": 1.0,
                "liabilities": 0.6, "equity": 0.4, "balances": balances,
                "basis": "", "imbalance_pct": 0.0, "stated_agrees": False}

    html = history_html([row("2026-06-30", True), row("2026-03-31", None),
                         row("2025-12-31", False)])

    assert "1 of the last 2 tested balance sheets" in html
    assert "1 cannot be tested" in html
    assert "Not testable" in html
    assert html.count("Balanced") == 1


def test_the_landing_sample_verdict_reads_the_tri_state(client, monkeypatch):
    from src.report import company_facts, landing

    _seed("ONLYA", **UNTESTABLE["ONLYA"])
    _seed("FULLY", **FULL)
    # Force a line so the verdict is reached for a filing that has none.
    monkeypatch.setattr(company_facts, "arithmetic_line", lambda v: "A − (L + E) = $0")

    assert landing.check_line("ONLYA")[1] == "Not testable"
    assert landing.check_line("FULLY")[1] == "Balanced"


# ---------------------------------------------------------------------------
# The dashboard script
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("_features_verify_harness.js")
FEATURES = Path(__file__).resolve().parents[1] / "src" / "report" / "static" / "features.js"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_dashboard_shows_not_testable_and_counts_it():
    payload = {
        "checked": 3, "reconcile": 1, "do_not_reconcile": 1, "not_testable": 1, "not_found": 1,
        "results": [
            {"ticker": "AAA", "found": True, "period_end": "2026-06-30", "reconciles": True, "imbalance_pct": 0.0},
            {"ticker": "BBB", "found": True, "period_end": "2026-06-30", "reconciles": False, "imbalance_pct": 12.5},
            {"ticker": "CCC", "found": True, "period_end": "2026-06-30", "reconciles": None, "imbalance_pct": 0.0},
            {"ticker": "NOPE", "found": False},
        ],
    }
    out = subprocess.run([NODE, str(HARNESS), str(FEATURES), json.dumps(payload)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
    shown = json.loads(out.stdout)

    assert shown["note"] == "3 checked: 1 reconcile, 1 do not, 1 not testable, 1 not found."
    rows = {r[0]: r for r in shown["rows"]}
    assert rows["AAA"][2] == "Yes"
    assert rows["BBB"][2] == "No"
    assert rows["CCC"][2] == "Not testable"
    assert rows["CCC"][3] == "—"  # no gap is shown for a filing that was not tested
