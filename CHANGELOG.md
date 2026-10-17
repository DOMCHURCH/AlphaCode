# Changelog

## Unreleased: release/period-fix

### Filing dates on historical balance sheets change

`filing_date` on a balance sheet and on each of its line items now means **the filing that first reported that value**. The sheet's own `filing_date` is **the filing that supplied total assets**.

Why: the old rule dated a figure by the *latest* filing that reported it. That is the right choice for the value (a restatement must beat the original) but the wrong one for the date. A later filing that only repeats a figure moves the printed date forward: every 10-Q repeats the prior year end as a comparative, and every statement of equity repeats its opening balance. Apple's 2026-03-31 quarter showed equity "filed 2026-07-31" beside assets "filed 2026-05-01", and a year on it would have said a year later.

What you will see:

- Newest sheets: almost unchanged (about 0.5% of companies' newest sheet changes date).
- Historical sheets (`/api/company/{ticker}/history`, `?as_of=`, the company page for an older period, `/api/verify` provenance links): about 61% of sheets since 2025 print a different header date, a median of about six months earlier. Values do not change.
- `/api/verify` builds its SEC filing link from the sheet's `filing_date`; for historical sheets it now points to the filing that carried the balance sheet, not a later filing that repeated a figure.
- A real restatement still takes the date of the filing that introduced the new value.

### Other changes in this release

- **Period ends.** SEC's bulk datasets round a period end to the nearest month end (Apple's 2026-06-27 reads 06-30). The real date is kept and every read path treats a rounded copy and its real-dated twin as one period. Read-side only: no data was rewritten.
- **Page freshness** is judged on the canonical newest period.
- **MCP.** The numbers are in the text answer; equity is labelled for what it is ("Equity incl. noncontrolling interest" when it includes the noncontrolling interest); `filer_totals_agree` is `null` when the filing balances; unknown tickers are `isError: true`; plan limits on `years` are stated; chart-rendering fields are gone from `check_balance_sheet`.
- **Site.** The equity band reads "Equity incl. NCI" and the legend "Equity incl. noncontrolling interest" where the filer's combined equity total is shown. The sign-in panel prices are labelled as Canadian dollars.
