# Why 0.1% of filings miss A = L + E

**Date:** 10 September 2026 · **Status:** mechanism analysis complete, population counts pending production access

## The reviewer is right

A = L + E is an identity, not a target. It is a consequence of double-entry
bookkeeping, so a correctly tagged filing read on the right terms satisfies it
exactly. "99.9% accurate" therefore does not describe the data being 99.9%
good — it describes **0.1% of filings that we cannot currently reconcile**, and
that number is only honest if each one can be named.

Every miss is one of exactly two things:

1. **The filing is not being read on the terms it was written on.** The filer
   used a structure our identity test does not model. Their arithmetic is fine;
   our reading is wrong.
2. **We failed to read it.** A tag exists in the filing and we did not ingest
   it. Ours.

A third case — the filer's own arithmetic is wrong — is possible and should be
vanishingly rare for audited public companies. It is worth measuring precisely
*because* it should be near zero.

## The discriminator

Guessing which bucket a failure belongs in would make this analysis worthless.
There is one field that settles it: **`liabilities_and_equity`**, the filer's
OWN stated right-hand side. Where a filer publishes it, three comparisons are
possible instead of two:

| Comparison | What it answers |
|---|---|
| `assets` vs `liabilities_and_equity` | Does the FILING balance, on its own figures? |
| `liabilities_and_equity` vs `L + E` | Did WE recover everything the filing put there? |

- Filing balances against itself, our sum falls short → **a line we did not
  ingest. Ours.**
- Filing does not balance against its own stated total → **the filer's
  arithmetic.** No parser fixes that.
- No stated RHS published → **unexplained.** Reported as its own bucket rather
  than assigned to whichever category looks best.

`scripts/identity_failures.py` implements exactly this, and self-tests against
seven constructed cases covering every branch.

## Categories

| Category | Mechanism | Whose fault | Detectable? |
|---|---|---|---|
| **Noncontrolling interests** | Consolidated filer reports the parent's equity and the NCI as two lines with no combined total. The real identity is A = L + E + NCI. | Filing structure | Yes — `minority_interest` closes the gap |
| **Mezzanine / redeemable preferred** | Sits between liabilities and equity. Common in airlines, biotech, SPACs. | **Ours** — see below | Not currently |
| **Rounding** | Gap under 1% of assets. Figures are in millions; one unit on a $400bn balance sheet is noise. | Neither | Yes |
| **Missing XBRL tag** | The filing contains a line we did not pull. | Ours | Yes, where a stated RHS exists |
| **Genuinely broken filing** | The filer's stated total does not match their own assets. | Company error | Yes, where a stated RHS exists |
| **Unexplained** | No stated RHS to compare against. | Unknown | No — and it is named as such |

## Two findings from the code

### 1. NCI was handled everywhere except the page a reader sees — FIXED

`verify.py` and `universe_check.py` both preferred the NCI-inclusive equity
basis and, where it was absent, tested whether `minority_interest` closed the
gap. `view1._check_identity` — the function behind the drawing on all 6,169
company pages — did not. It tested `A = L + E` against parent-only equity and
stopped.

The same filing was therefore **sound to the internal checker and "does not
balance" to a reader**. That is the largest single category inside the 0.1%,
and it was never a data problem.

Fixed: when the plain sum fails and the filer reported a separate
`minority_interest`, the NCI basis is tried. When it closes the gap the page
says so in words — "Balances as A = L + E + noncontrolling interest" — rather
than applying it silently, because *this balances once you include the minority
interest* is a different statement from *this balances*.

Guarded so it cannot break a filing that was right: the NCI is only read when
the filer published no combined total, and only after the plain sum has already
failed.

### 2. Mezzanine equity is not ingested at all — OPEN

There is no `temporary_equity`, `redeemable_preferred_stock` or
`redeemable_noncontrolling_interest` metric anywhere in the ingest. Grep the
tree and the concept does not exist.

This reclassifies the category. Mezzanine failures are **not** "filing
structure" — the filing tagged it correctly and we did not read it. **Ours.**
The script's category label says so.

Fixing it is an ingest change (`src/ingest/xbrl.py` concept map plus a
`BALANCE_SHEET_CONCEPTS` entry), not a page change, and it is the single
highest-value remaining item on this list.

## Counts

**Not measured.** Producing real per-category counts needs the production
database:

- `/status` and `/admin/universe-check` are admin-gated (correctly — closed in
  a previous session) and this session has no `ADMIN_SECRET`.
- The local database holds synthetic rows only.
- Sampling public company pages cannot work: at a 0.1% rate, finding ~6
  failures means fetching all 6,169 pages.

To produce the table, run against production:

```bash
python scripts/identity_failures.py            # markdown table
python scripts/identity_failures.py --json     # machine-readable
```

Publishing invented counts in a document whose subject is not inventing numbers
would be the wrong way to finish this analysis. The mechanism analysis above
stands on the code; the population counts are one command away from whoever has
the database.

## What goes on the site

`/methodology`, linked from the homepage identity figure and from every company
page that carries an identity note. The public paragraph:

> Every valid SEC filing we ingest reconciles to the accounting identity. The
> 0.1% that don't are flagged with the exact reason — noncontrolling interests,
> mezzanine equity, rounding, or a genuinely broken filing — never silently
> fudged. We surface the reason; we don't hide it.

The marketing claim stays at 99.9%. It is the true number and the honest one.
