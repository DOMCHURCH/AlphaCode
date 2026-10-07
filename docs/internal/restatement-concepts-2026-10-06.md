# Restatements compared across XBRL tags (found 2026-10-06, fixed same day)

## What was wrong

A restatement is "a later filing reported the same figure for the same period at a different value".
The tracker compared stored `fundamentals` rows by `(ticker, metric, period_end)` across filings, and a
*metric* is not a *concept*: several metrics are fed by more than one us-gaap tag, in preference order,
and the tag a filing happens to use decides which value is stored.

* `long_term_debt` = `LongTermDebtNoncurrent` (excludes the current portion), else `LongTermDebt`
  (includes it). Conagra, period 2011-05-29: $2,870.3M noncurrent in the 10-K, $3,200M total in the 10-Qs,
  reported as a 12% "revision". Same shape for NKE, CPRI, HAE, SGHT, BUKS, TSSI and OFAL in the live
  window.
* `revenue` = `RevenueFromContractWithCustomer...`, `Revenues`, `SalesRevenueNet`,
  `RevenuesNetOfInterestExpense`. These are not synonyms (Berkshire: `Revenues` $65.0B, contract revenue
  $45.2B).

The Frames loader makes it worse: the SEC Frames API returns only the *last-filed* fact per tag and
period, so each tag's fact attaches to a different filing (Nike FY2026 10-K supplies `LongTermDebt`
$7.94B, the later 10-Q supplies `LongTermDebtNoncurrent` $5.94B: a "25% revision").

Two detectors had the defect: `src/company/exceptions.py` (the feed, `/restatements`, `/api/exceptions`,
MCP `get_exceptions`) and `src/company/history.py::restatements` (`/api/company/{t}/changes`, MCP
`get_balance_sheet_changes`; it covers eight multi-tag balance-sheet metrics).

## The rule now

Compare filings only within one XBRL concept (`xbrl.comparison_tag`):

* single-tag metric: its one tag (so rows loaded before the column existed still compare);
* multi-tag metric: the stored `fundamentals.source_tag`;
* multi-tag metric with no stored tag: unknown concept, not compared.

`source_tag` is written by the bulk extractor and the Frames loader. Equal re-reports are still ignored.

## Interim behaviour until rows are reloaded

Existing multi-tag rows have `source_tag` NULL, so their restatements are *withheld*, not guessed.
Single-tag metrics (assets, liabilities, equity, cash, current assets/liabilities, net income, operating
income, operating cash flow, EPS) are unaffected. Withheld until reloaded: long-term debt and revenue in
the feed, plus intangibles, securities, loans, minority interest, short-term borrowings and temporary
equity in the per-company changes view.

Regenerate by reloading fundamentals (admin, atomic: the delete rolls back unless every quarter loads):
`POST /admin/reload-fundamentals` (header `X-Admin-Secret`), or `python -m src.backfill --fundamentals
--skip-bars`. The Frames sweep refreshes recent quarters on its own. Then rebuild the feed cache (it
rebuilds itself within `FEED_TTL_S`, 6 h, or on restart).

## Measured impact

* Live `/restatements` window on 2026-10-06: 124 figures at 60 companies. Ten rows are cross-tag: eight
  long-term debt (CAG, NKE, CPRI, HAE, SGHT, BUKS, TSSI, OFAL) and two revenue (INFQ, KULR). The eight
  long-term-debt companies drop out entirely: **114 figures at 52 companies**. Each row was checked against
  SEC companyfacts by tag and filing date; the three revenue rows that stay (GAMG, NUAI, PRPH) are same-tag.
* Sample of the 100 largest filers (`scripts/audit_restatement_tags.py`), changes of 1% or more: 25 of 40
  long-term-debt changes and 184 of 489 revenue changes were cross-tag.

## Known limits

* A tag rename with a genuine recast (e.g. `SalesRevenueNet` to `RevenueFromContract...` at ASC 606) is no
  longer flagged: nothing proves two different tags are the same line.
* The extractor stores only the preferred tag per filing. If an earlier filing reported both tags and a
  later one only the lower-ranked tag, a real revision of that lower-ranked tag is not visible.
* Revisions that are genuine in the filings but surprising (HMH equity "10" then $710M, SEAT sign flip,
  post-merger recasts at HDRN and FAC) are same-tag differences and stay in the feed.
