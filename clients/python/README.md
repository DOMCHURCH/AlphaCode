# balanceproof

Python client for [BalanceProof](https://balanceproof.dev): US public-company
fundamentals from SEC EDGAR, where every balance sheet, income statement and
cash flow is checked against its own totals, and a filing that does not add up
is flagged with the reason instead of passed through.

```bash
pip install "balanceproof[pandas]"
```

```python
import balanceproof as bp

client = bp.Client("YOUR_KEY")        # free key: https://balanceproof.dev/dashboard
                                      # or set BALANCEPROOF_API_KEY

client.balance_sheet("AAPL")
client.statements("AAPL", period="quarterly")

# Point-in-time (Pro and above): only what had been filed by that date,
# so a backtest never sees a figure before it was public.
client.balance_sheet("AAPL", as_of="2025-03-01")

# A pandas panel, one row per company and period.
df = client.panel(["AAPL", "MSFT", "KO"], ["revenue", "net_income", "operating_cash_flow"],
                  period="quarterly")
clean = df[df.checks_failed.str.len() == 0]      # drop periods that failed a check

# Failed checks and restatements across every company (Pro: 90 days, Business: all).
feed = client.exceptions(type="restatement")
client.to_frame(feed)
```

Derived figures (Q4 as the year minus nine months, Q2/Q3 cash flow from
year-to-date filings) are listed in each period's `derived`.

A call that needs a higher plan raises `balanceproof.PlanRequired`, whose
`required_plan` names it.
