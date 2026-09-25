# Launch copy (2026-09-25)

## MCP directory listing (Smithery, Glama, PulseMCP, mcp.so)

**Name:** BalanceProof

**Short description (one line):**
Checked SEC fundamentals for US public companies: balance sheets, income statements and cash flow, point-in-time, with every filing tested against its own totals.

**Long description:**
BalanceProof serves US public-company fundamentals straight from SEC EDGAR filings and checks every one before serving it: the balance sheet against Assets = Liabilities + Equity, the income statement against revenue minus cost of revenue, the cash flow against its change in cash. A filing that does not add up is flagged with the reason instead of being passed through silently. Figures are as filed, never restated or estimated, and each carries its filing date, so `as_of` queries return exactly what was public on a given day.

Works with no key at a demo rate; a free API key (no card) raises the limit.

**Server URL (Streamable HTTP):** `https://balanceproof.dev/mcp`

**Auth:** optional, `Authorization: Bearer <API key>` (free key at https://balanceproof.dev/dashboard)

**Tools:**
- `search_companies`: company name to ticker
- `get_balance_sheet`: the latest filed balance sheet
- `check_balance_sheet`: does A = L + E hold on this filing, and by how much it misses
- `get_balance_sheet_history`: every filed balance sheet over past periods
- `get_balance_sheet_changes`: what moved since last period, and what a later filing restated
- `get_financial_statements`: income statement and cash flow, annual or quarterly, each period checked; `as_of` for point-in-time
- `get_exceptions`: filings that failed their check and figures later restated, across all companies
- `get_api_key`: issue a free key

**Example prompts:**
- "Does Coca-Cola's latest balance sheet actually balance?"
- "Show Microsoft's quarterly revenue and operating cash flow for the last year."
- "Which companies restated their numbers this month?"
- "What was Tesla's Q1 2024 net income as first reported, and what is it now?"

**Category / tags:** Finance, Data, SEC, EDGAR, fundamentals, accounting

**Links:** https://balanceproof.dev · API docs https://balanceproof.dev/api · Python `pip install balanceproof`

**Client config (for directories that ask):**
```json
{
  "mcpServers": {
    "balanceproof": {
      "type": "http",
      "url": "https://balanceproof.dev/mcp"
    }
  }
}
```

## QuantConnect forum post

**Title:** Point-in-time SEC fundamentals that check themselves, plus a feed of restatements

**Body:**
I got burned by a fundamentals source that quietly served restated numbers, so my backtests were trading on figures that did not exist yet. I built my own pipeline from SEC EDGAR and I'm opening it up. Feedback from people who backtest on fundamentals would help a lot.

What's different:

- **Every filing is checked against its own totals.** Balance sheet: A = L + E. Income statement: revenue minus cost of revenue equals gross profit. Cash flow: operating + investing + financing + FX equals the change in cash. If a filing fails, you get the reason, not a silently adjusted number.
- **Point-in-time.** Every figure carries its filing date. `as_of=2024-06-01` returns only what had been filed by then: the original number before a restatement, the revised one after.
- **A restatements feed.** Across all companies, which figures a later filing changed. Example: Tesla's Q1 2025 10-Q restated Q1 2024 net income from $1,129M to $1,390M (the filing itself tags it as a restatement adjustment). A model trained on the revised figure would have "known" something the market didn't at the time.
- **Derived quarters are labelled.** Q4 is almost never filed on its own (it's the year minus nine months) and 10-Q cash flow is year-to-date only. Both are computed and marked `derived`, so you can drop them if you want only filed figures.

Python:

```python
pip install "balanceproof[pandas]"

import balanceproof as bp
c = bp.Client("YOUR_KEY")   # free key, no card: https://balanceproof.dev/dashboard
df = c.panel(["AAPL", "MSFT", "KO"], ["revenue", "net_income", "operating_cash_flow"],
             period="quarterly", as_of="2025-03-01")
```

Honest limits: US SEC filers only, headline figures rather than every line item, and history currently goes back about two years (deeper history is next if people want it). There's a free tier; paid plans start at $19 CAD/month.

What would make this useful in your research: longer history, more line items, or something else?

https://balanceproof.dev
