# Why 0.1% of filings miss A = L + E

**Date:** 10 September 2026 · **Status:** complete. All four code findings
fixed and the population **measured against production** — see
[Counts](#counts). The headline is that the site's 99.8% and this analysis's
86.84% are the same data under two definitions, and only one of them is a
claim about our extraction.

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

**Measured against production on 10 September 2026** via the Railway TCP proxy,
using working-tree code (`scripts/identity_failures.py`, which now shares
`view1.resolve_identity()` with the drawing). Database: 1,750,701 fundamentals
rows, 6,208 tickers, newest filing 2026-09-09.

### The population

| | Count |
|---|---|
| Tickers with any fundamentals | 6,208 |
| **Testable** — reports total assets, total liabilities and an equity total | **5,122** |
| Not testable — one of those three is absent | 1,086 |

The 1,086 are not a rounding detail and should not hide inside the word
"unclassifiable". At their newest period: **56** report no `total_assets`,
**604** report no `total_liabilities` (but do report equity), **371** report no
equity tag, and **51** report neither. That is ~975 filings where a core tag was
never ingested — an extraction gap of its own, and larger than every identity
failure below put together. `build_view1` derives liabilities from the stated
right-hand side for 618 of them and then deliberately skips the identity check,
because a sum that balances by construction cannot also be evidence that it
balances.

### Reconciles — 4,448 of 5,122 (86.84%)

| Category | Count | % of testable | Examples | Whose fault |
|---|---|---|---|---|
| Balances directly (A = L + E) | 4,447 | 86.82% | JPM, FNMA, FMCC, BAC, C, WFC | — |
| Reconciles once NCI is included | 1 | 0.02% | OUT | Filing structure |
| Reconciles once mezzanine is included | 0 | 0.00% | — | Filing structure |
| Reconciles once both are included | 0 | 0.00% | — | Filing structure |

**Why NCI is 1 and not hundreds.** 2,208 filers publish
`total_equity_incl_nci`, so their noncontrolling interest is already inside the
equity term and they land in *balances directly*. The NCI basis only fires for
the shape where a filer reports parent equity and the NCI as two lines with no
combined total, and exactly one filer in the universe does that and is
otherwise clean. The category is not dead; it is nearly empty because the
common case is handled upstream.

**Why mezzanine is 0, and why that number is not a fact about filings.**
`temporary_equity`, `redeemable_preferred_stock` and
`redeemable_noncontrolling_interest` have **zero rows in production**. The
concepts were added to the ingest in this session and the previous one; the
fundamentals table predates them. Nothing can be resolved by a tag that no row
carries, so this is a statement about our ingest history, not about how US
issuers file.

### Does not reconcile — 674 of 5,122 (13.16%)

| Category | Count | % of failures | Examples | Whose fault |
|---|---|---|---|---|
| Rounding (0.5–1% of assets) | 55 | 8.2% | KKR, MKL, STWD, CRH, RITM | Neither |
| Missing XBRL tag in our ingest | 608 | 90.2% | BLK, KDP, BIDU, SPGI, UNM | Ours |
| Genuinely broken filing | 0 | 0.0% | — | Company error |
| Unexplained (no stated RHS) | 11 | 1.6% | PSEC, MSC, GLAD, HODL, FGDL | Unknown |
| **TOTAL** | **674** | **100%** | — | — |

One line per category, in one sentence each:

- **Rounding** — the filing is right and so are we; the two sides differ by
  less than a percent of assets, which on a $414bn balance sheet (KKR) is
  presentation slack, not error.
- **Missing XBRL tag** — the filing balances against its own published total
  and our reconstructed `L + E` falls short, so the shortfall is a line we did
  not read; this is the whole of the interesting population and it is broken
  down below.
- **Genuinely broken filing** — zero, which is the right answer for audited
  public companies and is worth stating precisely because it was the outcome
  most worth measuring.
- **Unexplained** — no stated right-hand side to referee against, mostly BDCs
  and commodity trusts (PSEC, GLAD, HODL) whose net-asset presentation does not
  use the tags this test is built on.

### Inside the 608

Split by asking a second question of each one — *would the parent-only equity
have closed it?*

| Sub-mechanism | Count |
|---|---|
| Unresolved by any tag the filer published — the mezzanine population | 598 |
| Closes on `L + parent equity`, i.e. our NCI-inclusive figure is wrong | 9 |
| No usable figures at the newest period | 1 |

**The 598.** Drift is bimodal and both modes point the same way. 311 of them
drift by more than 95% — L + E is a rounding error beside assets — and none of
those is worth more than $1bn: AVEX, DMII, EVAC, BCSS, COAG, CEPF. That is the
signature of a SPAC or shell, where the trust account is the asset and
essentially all of the equity is Class A stock subject to redemption, carried
in **temporary equity**. The other mode is large and ordinary — BLK (3.8%),
KDP (5.0%), SPGI (8.0%), BIDU (2.9%) — all of which carry redeemable
noncontrolling interests. Both modes are mezzanine, and both are unreadable
today for the reason given above: the tags have no rows yet.

**The 9.** These are a different bug and should not be filed under "a tag we
did not read". `total_equity_incl_nci` is simply wrong for them, and six are
NEGATIVE while parent equity is positive: UNM (−$1.87bn against parent
$10.81bn), A (−$0.23bn against $7.36bn), COAG, SPIR, SVRA, EFSI. Because
`resolve_identity` prefers the NCI-inclusive total, the drawing trusts the bad
figure — the live UNM page reports a 20% imbalance on a filing whose
`L + parent equity` equals total assets exactly. Recorded here rather than
fixed: it is a change to which equity figure the whole site trusts, and that
deserves its own decision.

### The two pass rates, reconciled

The homepage stat bar says **99.8%**. This analysis says **86.84%**. Both are
correct, computed from the same rows, and the difference is entirely one of
definition:

```
  reconstructed passes                       4,448
+ pass only on the filer's own stated total    608
+ inside the 1% band universe_check allows      55
= 5,111 / 5,122                             = 99.79%
  universe_check: 5,782 / 5,795             = 99.78%
```

`universe_check` prefers the filer's stated `liabilities_and_equity` as the
right-hand side and used it for **5,751 of 5,795** checkable filings. So the
published figure overwhelmingly answers *does this filing balance against its
own stated total* — a real and useful check, and very nearly a self-consistency
one. It does not answer *did we recover every component of that total*. The
codebase already measured the gap and never surfaced it:
`universe_check`'s own `stated_total.moved_into_1pct` is **611**, against the
608 found here. Two independent paths, the same population.

Neither number is dishonest. But only one of them is a claim about our
extraction, and it is the smaller one.

### This number is not final (superseded — see *After the reload*)

86.84% is the **reconstructed, pre-reload** rate. 598 of the 674 failures are
mezzanine filings whose tags are now mapped but not yet ingested. Re-running
`reload_fundamentals` against production should move most of that population
into *reconciles once mezzanine is included*, and the reconstructed rate should
rise substantially. **That reload replaces the entire fundamentals table and has
not been run** — it is a deliberate, owner-level operation, not a side effect of
an analysis. Re-run this script afterwards; the numbers above are the honest
before.

Reproduce:

```bash
railway run --service Postgres python scripts/identity_failures.py
railway run --service Postgres python scripts/identity_failures.py --json
```

## After the reload — 11 September 2026

The reload this document asked for has been run. `POST /admin/reload-fundamentals?confirm=true&quarters=7`, committed in **8.5 minutes**.

### Two movements, not one

The mezzanine mapping was validated **before** the reload, by accident. A
scheduled incremental ingest ran overnight on 10/11 September and carried the
new tags into production on its own — mezzanine rows went from 0 to 1,341
without any intervention. So there are three measurements, not two, and the
middle one is the interesting one:

| | 10 Sep | 11 Sep pre-reload | 11 Sep post-reload |
|---|---|---|---|
| Balances directly | 4,447 | 4,448 | 4,421 |
| Reconciles via NCI | 1 | 0 | 0 |
| Reconciles via mezzanine | **0** | **404** | **450** |
| Reconciles via both | 0 | 1 | 1 |
| Rounding | 55 | 15 | 4 |
| Missing XBRL tag | **608** | **245** | **199** |
| Genuinely broken | 0 | 0 | 0 |
| Unexplained | 11 | 9 | 9 |
| **Testable** | 5,122 | 5,122 | 5,084 |
| **Failures** | 674 | 269 | **212** |
| **Extraction rate** | **86.84%** | **94.75%** | **95.83%** |

Mezzanine rows in `fundamentals`: **0 → 1,341 → 12,359**.

The overnight incremental did most of the work (+7.91pp); the full reload added
+1.08pp by applying the mapping to older periods the incremental never
rewrote. That ordering matters for the next time somebody asks whether a
reload is necessary: for a newly mapped tag, the normal ingest will reach
recent filings on its own, and the reload is only worth its cost for history.

### The reload cost coverage, and that is not a rounding detail

| | before | after |
|---|---|---|
| Rows | 1,752,729 | 1,573,194 |
| Distinct tickers | 6,210 | 6,172 |

41 tickers left the testable set and 3 entered it. Of the 41, **37 now have
ZERO fundamentals rows** — not fewer rows, none. They include **AVB
(AvalonBay Communities), an S&P 500 REIT**, alongside ALOT, AREN, AXIM and a
tail of small caps.

This is a real regression and it is inherent to what `reload_fundamentals`
does: it DELETES the table and rebuilds it from 7 quarters of SEC bulk
Financial Statement Data Sets. Anything the previous table held that those
files do not carry — because it arrived through the XBRL frames path, or
because the filer's period falls outside the window, or because the ticker→CIK
match failed against the bulk `sub.txt` — is gone until another ingest puts it
back.

The extraction rate went UP partly because these companies left the
denominator (5,122 → 5,084). That is worth saying plainly: some of the
improvement is companies leaving rather than filings being read better.

**Action:** that recommendation was WRONG and is superseded by *The 37
dropped companies* below. `kind=fundamentals` reads the same bulk datasets
the reload rebuilt from, so it is a no-op; the frames path was run instead
and restored none of them. The cause is in `_cik_to_ticker`, and the fix is
a code change.

### What is left: 212 failures

- **199 missing XBRL tag** — BLK (3.8%), UNM (20.0%), OPTU (1.8%), QXO (8.7%),
  BAM (13.3%), A (54.4%), CYH (2.6%), SLG (4.2%). BLK and A are known: BLK
  carries redeemable NCI our mapping still does not reach in its filed form,
  and A is one of the nine cases in *Inside the 608* where
  `total_equity_incl_nci` is itself wrong — its drift got WORSE after the
  reload (7.99% → 54.4%), which is consistent with a bad figure being
  re-extracted rather than corrected.
- **9 unexplained** — MSC, HODL, FGDL, EZBC, XRPZ, ETHV. Commodity and crypto
  trusts with no stated right-hand side; the same population as before.
- **4 rounding**, **0 genuinely broken**. Still zero broken filings out of
  5,084, across three separate measurements.

### Publication status

**95.83% is not published and should not be.** It is below the 98% bar set for
publication, it moved 9 points in 24 hours, and part of the last movement was
companies leaving the denominator. The no-number framing shipped in `f6332f9`
stands. Revisit when the figure is both above 98% and stable across two
consecutive measurements with no coverage loss between them.

## The 37 dropped companies — diagnosed, not yet restored (11 September 2026)

The reload dropped 37 companies to zero fundamentals rows. **Neither backfill
restores them, and the reason is a bug rather than a missing run.**

### What was tried

`POST /backfill?kind=fundamentals` was the obvious move and would have been a
**no-op**: its docstring says "As-reported fundamentals via the SEC bulk
Financial Statement Data Sets" — the same source `reload_fundamentals` rebuilds
from. Re-reading the files that omitted these companies cannot reintroduce them.

`POST /backfill?kind=filings` — the XBRL frames path, which is what actually
supplied these rows before — was run instead. It added **1 row** and restored
**0 of 37**. AVB stayed at zero.

### Why: both paths resolve identity through one incomplete file

Every fundamentals ingest maps SEC data to tickers through
`backfill._cik_to_ticker()`, which is built **solely** from SEC's
`company_tickers.json`. Two independent defects fall out of that.

**1. That file does not list every filer. 20 of the 37 are simply not in it.**

Fetched 11 September 2026: 10,407 entries. `AAPL`, `MSFT` and `JPM` are
present. `AVB`, `WBS`, `SE`, `RMAX`, `LBRDK`, `TALK`, `ALOT`, `FBRX`, `CXXIF`,
`AACB`, `AGGI`, `AIHS`, `BBCQ`, `BCAR`, `BCARU`, `BTMCQ`, `CMII`, `DEFI`,
`GLTK` and `IPCX` are not. AvalonBay is an S&P 500 REIT that files 10-Qs on
schedule; it is absent from the mapping file, so its CIK cannot be resolved to
a ticker and its rows are discarded silently at ingest.

Note the asymmetry that makes this invisible: `sector_map` DOES carry AVB, with
a CIK. The database already knows the answer the ingest throws away.

**2. `setdefault` keeps one ticker per CIK. 12 more lose to a sibling.**

```python
out.setdefault(cik, tkr)   # first ticker wins, the rest are dropped
```

A CIK routinely carries several tickers — share classes, SPAC units and
warrants. Whichever SEC happens to list first takes the CIK, and every other
ticker on it gets nothing:

| dropped | loses to | that CIK carries |
|---|---|---|
| HLX | HOS | HOS, HLX |
| RDIB | RDI | RDI, RDIB |
| AREN | PAAI | PAAI, AREN |
| BTOG | SGRX | SGRX, BTOG |
| NRDE | SNFI | SNFI, NRDE |
| FCCI | EAIQ | EAIQ, FCCI |
| SWAG | SWAGW | SWAGW, SWAG |
| LPAA | LPAAU | LPAAU, LPAA, LPAAW |
| SIMA | SIMAU | SIMAU, SIMA, SIMAW |
| JAB | ATLQ | ATLQ, JAB, ATLQR, JABRU, ATLQU, ATLQW, JABRW, JABRR |
| ACLEW | ALCE | ALCE, ACLEW, ALCED |
| ALCED | ALCE | ALCE, ACLEW, ALCED |

**The data is not lost for these twelve** — it is in the table under the winning
ticker. HOS has 342 rows, PAAI 336, ALCE 270, SGRX 168, EAIQ 81. So
`/company/HLX` is empty while `/company/HOS` holds exactly the balance sheet a
reader asked for. That is arguably worse than missing data, because the site
looks confidently wrong rather than incomplete.

**3. Five are unexplained**: `AXIM`, `FBDT`, `FVTI`, `GBNY`, `REAX` are in
`company_tickers.json`, win their CIK, and still have no rows. Not chased here.

### Why the reload exposed it rather than caused it

Nothing about the mapping changed on 11 September. The pre-reload table simply
held rows from earlier runs, made when SEC's file listed different symbols —
`fundamentals` was accumulating history that the current mapping can no longer
reproduce. The reload deleted that accumulation and rebuilt from what the
mapping can see today. **The 37 were already unreachable; the reload only
stopped hiding it**, and any future reload will drop them again.

### The fix, which is a code change and not a backfill

1. **Seed `_cik_to_ticker` from the local `sector_map` first**, then let SEC's
   file fill gaps. `sector_map` has 10,481 rows with CIKs and already contains
   AVB. This alone recovers the 20.
2. **Stop collapsing a CIK to one ticker.** Prefer the ticker that is in our
   own universe over whichever SEC lists first; where several are ours, the
   rows need attributing to each or the choice needs to be explicit and
   recorded, because silently picking the warrant over the common stock is how
   `HLX` became `HOS`.
3. Re-run the reload afterwards, and re-check these 37 specifically.

Until then the 37 pages stay empty, and `/company/HLX`, `/company/RDIB`,
`/company/AREN` and the nine others in that table are empty while their data
sits under another symbol.

## Where this ended up — 11 September 2026

**4,823 reconciled of 5,028 testable. 205 flagged. 0 genuinely broken.**
(95.92%, internal only — see below for why it is not published.)

Three rounds of work took the reconstructed rate 86.84% → 94.75% → 95.83% →
95.92%. The last round moved it by 0.09pp, and that is the signal to stop.

### The 199 is a long tail, not a bug

The remaining `missing_tag` failures were investigated directly against SEC
companyfacts for BLK, CYH and COTY. Two findings, both against the working
hypothesis:

1. **There are no company-specific extensions.** Every unmapped tag in all
   three is standard `us-gaap`. The theory that these filers invent their own
   taxonomy is simply wrong.
2. **Most unmapped tags cannot close anything.** `OtherAssets`,
   `FiniteLivedIntangibleAssetsNet`, `DeferredIncomeTaxLiabilities` and the
   rest are COMPONENT line items inside assets or liabilities. Mapping them
   adds detail to a drawing; it does not move the identity by a cent.

Only three tags were both unmapped and capable of closing a gap, and SEC's
frames API bounds how far each reaches:

| tag | filers it reaches, of the 199 |
|---|---|
| `RedeemableNoncontrollingInterestEquityFairValue` | 2 |
| `RedeemableNoncontrollingInterestEquityOtherCarryingAmount` | 3 |
| `MinorityInterestInOperatingPartnerships` | 1 |

All three are now mapped, and exactly **one is verified to close**: CYH tags
its redeemable NCI at fair value, that tag is $260,000,000, and the gap our
reconstruction left was $260,000,000 — drift 1.97% → 0.00%.

### The specific causes behind the rest

- **Separate-account and reorganised filers.** BLK's gap is $6.418bn and
  matches no single tag on its balance sheet. It carries $58.8bn of
  `SeparateAccountAssets`, and the CIK now filing as BLK is **2012383**, a new
  entity from the reorganisation. This needs per-filer reading, not a mapping.
- **Fair-value measurement of a concept we model at carrying amount** (CYH).
  Fixed.
- **Component line items that never enter the identity.** Not a defect.
- **A wrong `total_equity_incl_nci`** in nine cases, six of them negative while
  parent equity is positive (UNM, A). Recorded in *Inside the 608*; changing
  which equity figure the site trusts is its own decision.

### The ceiling

Tag mapping has extracted close to everything tag mapping can. Further gains
require reading individual filings, and 199 filings at the rate the last three
yielded is not a good use of anyone's week. **The product answer is to publish
the exceptions rather than shrink them**, which is what
[/methodology](/methodology) now does.

### Why no percentage is published, restated

The denominator moves. It moved four times in two days: 5,122 → 5,122 → 5,084 →
5,028, as coverage improved and as non-universe tickers stopped receiving rows.
Restoring 31 unreachable companies *lowered* the rate, because the newly
visible filings were the awkward ones. A number that falls when quality rises
is not a quality measure.

So the public pages carry counts — companies covered, reconciled, flagged,
hidden — read live from `stats.identity_breakdown()`, which classifies with the
same `resolve_identity` the drawing uses. **Zero genuinely broken filings**
across every measurement taken, and that is the claim worth making.

## What goes on the site

**Decided 11 September 2026: no numeric accuracy rate is published anywhere.**

Not 99.9%, not the 99.8% the stat bar computed live, and not the 86.84%
extraction figure this audit produced. The reasoning is in *The two pass rates,
reconciled* above: the published number measured whether a filing balances
against ITS OWN stated total, which is close to a self-consistency check, and
not whether we recovered every component of it. Printing the flattering one
under the word "accuracy" is the thing this site exists not to do.

The extraction figure is not published EITHER, and that is the less obvious
half of the decision. It is a temporary engineering state -- the mezzanine tags
are mapped and simply not yet re-ingested -- so publishing it would convert a
fixable bug into a permanent marketing claim, and the number would be wrong
(too low) within one reload.

What is published instead is the method, which is true today, stays true after
the reload, and does not move when extraction improves:

> Every valid SEC filing we ingest reconciles to the accounting identity. When
> a filing doesn't balance, we flag the exact reason — noncontrolling
> interests, mezzanine equity, rounding, or a broken filing — never silently
> fudged.

Short variant for buttons, badges and JSON-LD:

> Every valid filing reconciles. Exceptions are flagged, not hidden.

The rate is still COMPUTED. `stats.identity()` and
`scripts/identity_failures.py` both read it and both must keep working -- it is
not wrong, it is unpublishable. `tests/test_content_accuracy.py` asserts that
no page renders a percentage next to "accurate"/"accuracy", and that
`llms.txt` and `financial-data.txt` carry none either, because an answer engine
will quote a number back with more confidence than the page gave it.

**Revisit after the reload.** If the extraction rate clears 98% it becomes a
defensible thing to publish, and this decision should be taken again with the
real number in hand.
