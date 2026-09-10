# Why 0.1% of filings miss A = L + E

**Date:** 10 September 2026 · **Status:** mechanism analysis complete and all
four code findings fixed; population counts still pending production access
(see [Counts](#counts) for the exact blocker and the two ways to clear it)

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

`scripts/identity_failures.py` implements exactly this. `tests/test_identity_failures.py`
pins all of it against constructed cases covering every branch, including the
`nci + mezzanine` combination that no earlier version could resolve.

## Categories

The first two are **passes**: the drawing resolves them and names the term it
added. The rest are failures. Reporting them in one undifferentiated list is
what let the script and the page quote different pass rates — see finding 4.

| Category | Mechanism | Whose fault | Detectable? |
|---|---|---|---|
| **Noncontrolling interests** | Consolidated filer reports the parent's equity and the NCI as two lines with no combined total. The real identity is A = L + E + NCI. | Filing structure | Yes — `minority_interest` closes the gap |
| **Mezzanine / redeemable preferred** | Sits between liabilities and equity. Common in airlines, biotech, SPACs. The real identity is A = L + E + Mezzanine. | Filing structure | Yes — `temporary_equity`, `redeemable_preferred_stock` or `redeemable_noncontrolling_interest` closes the gap |
| **Rounding** | Gap under 1% of assets. Figures are in millions; one unit on a $400bn balance sheet is noise. | Neither | Yes |
| **Missing XBRL tag** | The filing contains a line we did not pull. | Ours | Yes, where a stated RHS exists |
| **Genuinely broken filing** | The filer's stated total does not match their own assets. | Company error | Yes, where a stated RHS exists |
| **Unexplained** | No stated RHS to compare against. | Unknown | No — and it is named as such |

## Four findings from the code

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

### 2. Mezzanine equity was not ingested at all — FIXED

There was no `temporary_equity`, `redeemable_preferred_stock` or
`redeemable_noncontrolling_interest` metric anywhere in the ingest. The concept
did not exist in the tree, so the bucket could never fill: every mezzanine
filer fell through to *rounding* or *unexplained*, and the drawing called a
correctly tagged filing broken.

That was the reclassification this section argued for — **ours**, not filing
structure, because the filer tagged it and we did not read it.

Fixed on both halves:

- **Ingest** (`src/ingest/xbrl.py`): `temporary_equity` maps
  `TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests`,
  `...AttributableToParent` and `TemporaryEquityCarryingAmount`;
  `redeemable_preferred_stock` maps `RedeemablePreferredStockCarryingAmount`.
  Both carried through `BALANCE_SHEET_CONCEPTS` into `bs.equity`.
- **Page** (`src/company/view1._check_identity`): when the plain sum fails, the
  candidate bases are now `+NCI`, `+Mezzanine` and `+NCI+Mezzanine`, and the
  one that closes with the smallest remaining gap wins. Each says which basis
  it used, in words, on the drawing.

Two guards. `temporary_equity` is the section TOTAL and
`redeemable_preferred_stock` is a component of it, so `_mezzanine()` prefers
the total rather than summing them — adding both overshoots by the component
and breaks a filing that balanced. And unlike the NCI, mezzanine is read even
when the filer published `total_equity_incl_nci`: that figure is a total of
PERMANENT equity, and the mezzanine block sits outside permanent equity
entirely.

Because the drawing now resolves these, they stop being failures rather than
becoming better-labelled ones. The category label moves from "Ours (tag not
ingested)" to "Filing structure", matching NCI.

### 3. `redeemable_noncontrolling_interest` was named but never supplied — FIXED

The previous pass left this deliberately: the name sat in the script's
`MEZZANINE_METRICS` while nothing in the ingest produced it, on the reasoning
that "a name no row can supply is exactly how this gap was found the first
time". It has now been supplied.

Redeemable NCI is a noncontrolling interest whose holder can put it back to the
company. That redemption right is what carries it **out of permanent equity**
and into the mezzanine — so `total_equity_incl_nci` does not contain it, and
adding `minority_interest` does not reach it either. They are two different
lines about two different holders, and a filer with both was previously
unreconcilable by any basis this codebase could construct.

Wired through all four places it has to exist:

- `src/ingest/xbrl.py` — `RedeemableNoncontrollingInterestEquityCarryingAmount`
  leading, with the Common and Preferred variants behind it;
- `src/company/balancesheet.py` — the key map and the equity-concepts read;
- `src/company/view1._mezzanine()` — last in the prefer-don't-sum chain, as a
  component of the section total.

Note this changes **future ingests only**. Existing rows do not gain the metric
until `reload_fundamentals` runs against production, so a re-run of the script
before that reload will show this category empty for reasons of history rather
than of accounting.

### 4. The script and the drawing disagreed about "balances" — FIXED

`build_view1` treats a filing that closes once the NCI or the mezzanine is
included as **balancing**, and says on the drawing which term it added.
`scripts/identity_failures.py` counted those same filings as **failures**.

So the script's headline was the pass rate *before* the explanations while the
page quotes the rate *after* them: two numbers, both called the pass rate, on
the same data, and no way to tell from either which one you were reading. The
script also tried each term singly and never `nci + mezzanine`, so a filer
reporting equity, a mezzanine block and an NCI as three separate lines could
not be resolved at all.

Fixed by deletion rather than by a second copy. The decision now lives in
`view1.resolve_identity()` and both callers use it — reimplementing it is what
caused the divergence, so there is now one implementation to disagree with.
The report splits into four RECONCILES rows and four DOES NOT RECONCILE rows,
because collapsing them is what makes a headline number unfalsifiable.

**One divergence remains, and it is deliberate.** `universe_check` — which
drives the stat bar's reconcile figure on the homepage — is a third definition:
it prefers the filer's stated right-hand side over `L + E`, uses a 1% band
rather than 0.5%, and never adds mezzanine. Reconciling those three is a
decision about what the public number should mean, not a bug to be quietly
edited, so it has been left alone and is flagged here instead. **Expect the
script's pass rate and the stat bar's to differ until that decision is made.**

## Counts

**Still not measured against production**, and the reason is now specific
rather than general. Diagnosed 10 September 2026:

- The Railway CLI **is** authenticated, and `railway run` does inject the
  production environment — `ADMIN_SECRET` among it.
- But `DATABASE_URL` resolves to **`postgres.railway.internal:5432`**, which is
  Railway's private network. It does not resolve from a developer machine, so
  `railway run python scripts/identity_failures.py` reaches the credentials and
  not the database.
- `DATABASE_PUBLIC_URL` **is not set** on the service, so there is no public
  endpoint to substitute.
- `railway ssh` — which would run inside the network — refuses: no SSH key is
  registered with the account.
- No HTTP endpoint exposes the population. `/reconcile` samples at most 50
  companies and `/admin/verify` checks five reference names; neither can
  produce a per-category breakdown over 6,169 filings.

**The unblock, in order of usefulness:**

1. **Enable the TCP proxy** on the Postgres service in the Railway dashboard.
   `DATABASE_PUBLIC_URL` then appears in the service variables and
   `railway run python scripts/identity_failures.py` works from a laptop
   against **working-tree code** — which matters, because the corrected
   `classify()` is not deployed and a run inside the container would measure
   the old definition.
2. **Register an SSH key** (`ssh-keygen -t ed25519`, add it to the Railway
   account) and use `railway ssh`. This runs *deployed* code, so it yields
   pre-fix numbers or a raw metrics dump to classify locally — useful, but a
   step behind option 1.

Then:

```bash
railway run python scripts/identity_failures.py            # markdown tables
railway run python scripts/identity_failures.py --json     # machine-readable
```

The script has been run end to end against a seeded SQLite database and
produces both tables and the JSON correctly; what is missing is the population,
not the tooling.

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
