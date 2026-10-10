# flagged-filings-2026-10-09.csv

The 995 filings that failed the BalanceProof balance sheet check in a frozen
snapshot of our database taken on 2026-10-09 (17:12 UTC). It is the data behind
the report "995 of 38,568 SEC Filings Failed Our Balance Sheet Check"
(https://balanceproof.dev/blog/which-sec-balance-sheets-fail-the-identity-check).

A failure of our check is not a statement that a filer's filing is incorrect.
It means that the figures we hold for the filing did not satisfy
Assets = Liabilities + Equity. In the 20 filings we read against the filer's own
XBRL on EDGAR (a seeded random sample of these 995), every one balanced on the
filer's own figures; the shortfall was in a line our reader did not pick up.
See the report for what that sample can and cannot show.

## What is in the file

One row per failed filing: 995 rows plus a header row, UTF-8, comma separated,
sorted by `ticker` then `period_end`. A blank cell means the value is absent,
not zero.

The unit is one filing per company and period (a ticker and a `period_end`).
It is the first filing our database holds for that period, kept only if it was
filed 0 to 105 days after the period ended, so that a later filing carrying the
period as a comparative column is not counted. Every figure in a row comes from
that single filing.

Coverage is meant to be 10-K and 10-Q periodic filings. Six rows are the
exception: our database holds figures for them, but EDGAR lists them as 20-F or
40-F foreign private issuer annual reports (see `link_status`). They are counted
in the 995. Our database has no form column for the 38,568 filings that were
tested, so a small number of passing filings may also be 20-F or 40-F reports.

Period ends run from 2024-08-31 to 2026-08-31 in this file (2024-06-30 to
2026-09-05 across all filings tested). Filing dates in the snapshot run from
2024-10-01 to 2026-10-08. The 995 rows come from 286 tickers, so one company can
appear several times.

## The check

A filing balances if total assets equal total liabilities plus total equity to
within 0.5% of total assets. Equity is total equity including noncontrolling
interests where the filer reports it, otherwise total equity. If the plain sum
misses, the check also tries adding the lines that sit between liabilities and
equity or alongside it: noncontrolling interest, temporary equity, redeemable
preferred stock, redeemable noncontrolling interest and minority interest in an
operating partnership. A filing that balances on that basis passes. The rows in
this file are the ones that still did not balance. The full definition is at
https://balanceproof.dev/methodology.

## Columns

| Column | Meaning |
|---|---|
| `ticker` | Ticker as held in our database. |
| `cik` | SEC Central Index Key. Blank for one row (CCTSF) where we hold none. |
| `sector` | The sector our sector map gives the ticker, or `Unclassified` where it maps to none. |
| `period_end` | Balance sheet date, `YYYY-MM-DD`, as held in our database. |
| `filing_date` | Date the filing was filed, `YYYY-MM-DD`. |
| `form` | Form type as listed by EDGAR: `10-K`, `10-Q`, `10-KT`, or `20-F` / `40-F` for the six foreign-filer rows. Blank where no filing was matched. Never inferred. |
| `accession` | EDGAR accession number of the filing. |
| `filing_url` | Link to the filing's main document on sec.gov. |
| `filing_index_url` | Link to the filing's index page on sec.gov. |
| `link_status` | How well the row matched an EDGAR filing. See below. |
| `total_assets` | Total assets, US dollars, as filed. |
| `total_liabilities` | Total liabilities, US dollars, as filed. |
| `total_equity` | The equity figure the check used, US dollars, as filed: equity including noncontrolling interests if present, otherwise total equity. |
| `liabilities_and_equity_stated` | The total of liabilities and equity as the filer stated it, US dollars. Blank where we hold none (the 10 `unexplained` rows). |
| `gap_vs_components_usd` | `total_assets - (total_liabilities + total_equity)`, US dollars, signed. Positive means assets exceed what the components we hold add up to. Measured before any noncontrolling or mezzanine line is added. |
| `gap_vs_stated_usd` | `total_assets - liabilities_and_equity_stated`, US dollars, signed. Blank where no stated total. |
| `gap_pct_of_assets` | The check's gap as a percentage of total assets: the absolute value of `gap_vs_components_usd` divided by `total_assets`, times 100. This is the `drift_pct` the site computes. |
| `check_category` | Our internal label for why the check failed. See below. |
| `note` | Free text for the ten rows whose link is not exact. Blank otherwise. |

All amounts are in US dollars exactly as filed. Nothing is adjusted or rescaled. Amounts that a filer reports in thousands are expressed in dollars.

## check_category

These are labels from our own check, not findings about the filer.

- `missing_tag` (971 rows). The gap is 1% of total assets or more, and the filer's
  own stated total of liabilities and equity agrees with its total assets to
  within 0.5% (in 969 rows exactly, in two rows within $1). So the filing agrees with
  itself and the components we hold fell short: a credit-side line the filer
  reported did not reach our sum. It is a label for "the filer's own stated total
  agrees with assets but our components fell short". It is not a finding about the
  filer, and it does not say why the line did not reach us.
- `rounding` (14 rows). The gap is between 0.5% and 1% of total assets.
- `unexplained` (10 rows). The gap is 1% of total assets or more and we hold no
  stated total of liabilities and equity to compare with, so the check cannot say
  whose figures fall short.
- `broken` is the fourth category the check can assign: the filer's own stated
  total disagrees with its own total assets by more than the tolerance. No filing
  in this snapshot has it, so it does not appear in the file.

## link_status

- `exact` (985 rows). Filing date and report date both match one EDGAR filing of
  form 10-K, 10-Q or 10-KT (218 10-K, 766 10-Q, 1 10-KT).
- `not_10k_10q_foreign_filer` (6 rows: GLAS 2024-12-31 and 2025-12-31, MSC
  2024-12-31 and 2025-12-31, YMM 2024-12-31 and 2025-12-31). Filing date and
  report date match an EDGAR filing, but it is a 20-F or 40-F, outside the 10-K and
  10-Q filings the report covers. `form`, `accession` and the URLs are those
  filings'.
- `near_match_period_date_differs` (3 rows: IRIX 2025-03-31, KOPN 2025-12-31,
  KOPN 2026-03-31). A 10-K or 10-Q was filed on the same date, but EDGAR gives its
  report date as 2025-03-29, 2025-12-27 and 2026-03-28, a few days before our
  `period_end`. That is consistent with a 52/53-week fiscal year; we have not
  confirmed it. The URL is the candidate filing and should be checked before it is
  relied on.
- `no_cik` (1 row: CCTSF 2024-09-30). We hold no CIK for the ticker, so no EDGAR
  filing was matched and the link columns are blank.

## Things to know before using it

- This file lists only the failures. The counts of filings tested (38,568) and the
  denominators in the report come from our full table, which is not published here.
- 433 of the 995 rows carry the SIC description "Blank Checks" in our sector map,
  and all 433 have `sector` set to `Unclassified`. The file has no column for the
  SIC description. The report says what is and is not known about that group.
- Some rows report very small total assets (one row is under $10,000). They are
  as filed and have not been cleaned.
- The 995 rows are a count of filings, not companies.

## Source and licence

Source: public filings on SEC EDGAR (https://www.sec.gov/). The figures are public
SEC data, as filed. The category labels, the gap columns and the sector column are
our calculation, from the snapshot named above.

## Reproducing it

The method is written out at https://balanceproof.dev/methodology. The balance
sheet lines for any company and period can be read from the free API
(https://balanceproof.dev/xbrl-balance-sheet-api), or from the filing at the URL in
each row, and the check is plain arithmetic on them. Each company's page on the site
(https://balanceproof.dev/company/NTWO, for example) shows the check on its latest
period.
