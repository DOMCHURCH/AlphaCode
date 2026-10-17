# Changelog

## Unreleased: fix/audit-batch-a

### A filing that cannot be tested is no longer reported as balancing

Some filings report total assets but not total liabilities or not total equity, and give no stated liabilities-plus-equity total, so there is nothing to compare assets against. They used to come back as passing: `reconciles: true` from `POST /api/verify`, and "balances ... within tolerance" from the MCP `check_balance_sheet` tool. They are now reported as **not testable**, which is neither a pass nor a failure. They use the same classification as the home-page counts and the exceptions feed (`not_testable`).

**Breaking for typed consumers.** `reconciles` on `POST /api/verify` is now `true`, `false` or **`null`**. A consumer that expects a boolean will now see `null` for filings that cannot be tested. Treat `null` as "no answer", not as `false`.

What changed on `POST /api/verify`:

- `reconciles` is `null` for a `not_testable` filing (nullable in the OpenAPI schema).
- New per-result fields: `identity_status` (`balanced`, `nci`, `mezzanine`, `nci+mezzanine`, `rounding`, `missing_tag`, `broken`, `unexplained`, `not_testable`) and `missing_for_check` (the totals the filing does not report, e.g. `["total liabilities", "total equity"]`; empty otherwise).
- New top-level count `not_testable`. `reconcile` and `do_not_reconcile` no longer include those filings, so `reconcile + do_not_reconcile + not_testable = checked`.
- A filing with no components but a stated liabilities-plus-equity total is tested against that total; if the stated total disagrees with assets it now reports `reconciles: false` instead of `true`.
- Billing is unchanged: each found ticker is one call, whatever the answer.

MCP: `check_balance_sheet` answers `not_testable` (not an error, one call billed) and names what is missing; its structured `balances` is `null`. The server instructions say that each period is checked when the filing reports the lines the check needs, and that otherwise the check returns `not_testable`. The server title is now "BalanceProof - SEC balance sheets, with checks" (it said "verified"). `get_financial_statements` says `not_testable` instead of "checks 0/0 passed" when no check could run.

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
