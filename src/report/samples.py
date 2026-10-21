"""Real API responses, captured from the live service, shared by /verification
and the home page's buyer tabs.

Excerpts: some fields are left out, none is edited. Each block records where it
came from and when. A response that has not been captured from production is
not written by hand here; it is left out (see `PAID`).
"""

from __future__ import annotations

CAPTURED = "2026-10-19"

# ---- real responses, captured from the live service on CAPTURED -----------
# Excerpts: some fields are left out, none is edited. The first comes from
# GET /api/demo/AAPL (no key), the other two from the check_balance_sheet MCP
# tool, which reads the same database.
DEMO_AAPL = """{
  "ticker": "AAPL",
  "company_name": "Apple Inc.",
  "period_end": "2026-06-27",
  "filing_date": "2026-07-31",
  "assets": {
    "total_assets": {
      "value": 383266000000.0,
      "period_end": "2026-06-27",
      "filing_date": "2026-07-31",
      "missing": false,
      "restated": false
    }
  },
  "liabilities": {
    "total_liabilities": {
      "value": 275746000000.0,
      "filing_date": "2026-07-31",
      "restated": false
    },
    "liabilities_and_equity": {
      "value": 383266000000.0,
      "filing_date": "2026-07-31",
      "restated": false
    }
  },
  "equity": {
    "shareholders_equity": {
      "value": 107520000000.0,
      "filing_date": "2026-07-31",
      "restated": false
    }
  },
  "source": "SEC Financial Statement Data Sets (as reported)"
}"""

CHECK_AAPL = """{
  "ticker": "AAPL",
  "period_end": "2026-06-27",
  "filing_date": "2026-07-31",
  "total_assets": 383266000000,
  "total_liabilities": 275746000000,
  "total_equity": 107520000000,
  "balances": true,
  "identity_status": "balanced",
  "identity_basis": "as_filed",
  "imbalance_pct": 0
}"""

CHECK_AACB = """{
  "ticker": "AACB",
  "company_name": "Artius II Acquisition Inc.",
  "period_end": "2026-06-30",
  "filing_date": "2026-08-05",
  "total_assets": 232407651,
  "total_liabilities": 17309711,
  "total_equity": -17147832,
  "balances": false,
  "identity_status": "missing_tag",
  "imbalance_pct": 99.93,
  "filer_totals_agree": true,
  "filer_totals_note": "The filer's own stated total for liabilities plus equity equals its total assets, so the filing balances as reported; the shortfall is in what this dataset read, not in the filing."
}"""

# Real responses for the two paid-plan calls, added once captured from
# production with a key on that plan: {"as_of": "...", "restatement": "..."}.
# Empty until then; pages show the call, the plan it needs, and no output.
PAID: dict[str, str] = {}

# Shorter excerpts of the same captured responses, for the home page's cards.
# Fields left out; every value shown is exactly as captured above.
AGENT_SHORT = """{
  "ticker": "AACB",
  "balances": false,
  "identity_status": "missing_tag",
  "imbalance_pct": 99.93,
  "filer_totals_agree": true
}"""

QUANT_SHORT = """{
  "ticker": "AAPL",
  "total_assets": {
    "value": 383266000000.0,
    "period_end": "2026-06-27",
    "filing_date": "2026-07-31"
  }
}"""

FINTECH_SHORT = """{
  "ticker": "AAPL",
  "total_assets": 383266000000,
  "total_liabilities": 275746000000,
  "total_equity": 107520000000,
  "balances": true,
  "identity_basis": "as_filed"
}"""
