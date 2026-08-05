# To Scale

**The first version reported JPMorgan's total assets as $641 billion. The real
figure is $4.42 trillion.**

The number wasn't corrupted, and nothing miscalculated it. $641 billion is
JPMorgan's assets *in Europe* — a real figure, from the same filing, sitting in
the same file as the consolidated total and looking identical to it.

That is the shape of the whole problem. SEC's bulk data carries the same tag
many times per company per quarter, distinguished only by dimensional columns
saying which slice each row describes. Discard those columns and a segment
total, a legal-entity total and the company's actual total become three
indistinguishable numbers in a list. There is no checksum, no flag, no
plausibility signal: the wrong figure is a real figure about a real thing, just
not the thing you asked for. It renders perfectly. It is off by a factor of
seven.

Finding that bug is what this project is mostly about.

---

## What it is

A website that draws any US public company's balance sheet at true proportion,
from what the company filed with the SEC. Two columns of the same height — what
the company owns on the left, sorted by what it is; who has a claim on it on
the right, lenders first and owners last. The columns match because they are
the same money counted twice, which makes the accounting identity something you
see rather than something you are told.

It is for anyone who wants the shape of a company without learning to read a
balance sheet first. A bank, a retailer and an airline look nothing alike, and
that difference is legible in about two seconds when the figures are drawn to
scale and invisible when they are in a table. Three views: the balance sheet at
proportion, revenue as a flow from sales through costs to what is left, and the
company's revenue against national GDP for a sense of scale.

---

## How the data works

**One ZIP per quarter, not one request per company.** SEC publishes quarterly
[Financial Statement Data Sets](https://www.sec.gov/dera/data/financial-statement-data-sets):
`sub.txt` (one row per filing) and `num.txt` (roughly two million numeric XBRL
facts). Seven quarters is seven downloads. The per-company XBRL API would be
around 85,000 requests against a 10/second limit.

**Selection rules.** One fact per concept per company-period:

- Consolidated only — `coreg` empty *and* `segments` empty. This is the rule
  the $641B bug violated.
- Balance sheet items are **instants** (`qtrs=0`): a photograph of one day.
- Income and cash-flow items are **durations** matching the period
  (`qtrs ∈ {1,4}`): a film over three months or a year.
- Where several tags map to one concept, a deterministic preference order
  decides and the first that resolves wins.

**Validation before storage.** Total assets must be positive. A component
cannot exceed the total it belongs to. The accounting identity is checked and
drift recorded. Every rejection is logged with its reason, and if more than 20%
of a quarter's company-periods fail, the entire load aborts rather than writing
what survived — a half-right table is worse than an empty one, because it looks
finished.

**Point-in-time correctness.** Every fact carries `period_end` *and*
`filing_date`, the date it actually became public. A quarter ending 30 September
may not be filed until 8 November; using it on 1 October is lookahead. Within a
load the earliest filing date wins, so a figure is stored as first reported.
Restatements are separate facts with their own dates, not overwrites.

---

## Three problems worth writing up

### 1. Dimensional facts — the $641B bug

`num.txt` carries `Assets` twenty-three times for JPMorgan in a single filing:
by geography, by business segment, by legal entity, by fair-value level.
Exactly one row is the consolidated company. The original parser read only
`[adsh, tag, ddate, qtrs, uom, value]` — it never loaded `coreg` or `segments`,
so it could not have told the rows apart even in principle. It took the first
one.

Equity was worse. The stored figure was **−$1.43 billion** against an actual
$362.44 billion, because the first `StockholdersEquity` row in the file was
`EquityComponents=AccumulatedGainLossNetCashFlowHedgeParent` — a hedging
component, correctly labelled, that a dimension-blind reader takes for the
company's equity.

**Fixed** by reading the dimensional columns and keeping only undimensioned
rows. **Found** by dumping every raw row carrying the tag and reading them,
rather than reasoning about what the tags probably meant. That dump is a
permanent endpoint now rather than a one-off script, because the same question
returns every time a new kind of filer is added.

### 2. Noncontrolling interests

After the rebuild a whole class of company still failed the identity by 5–30% —
Cheniere, Air Products, S&P Global among them. Every one had partly-owned
subsidiaries.

The two sides were drawn from different scopes. `Assets` is consolidated: it
includes a 60%-owned subsidiary's assets *in full*. `StockholdersEquity` is
parent-only: it excludes the 40% belonging to somebody else. Subtract one from
the other and the gap is exactly the minority interest — a real number that was
simply never on the page.

**Fixed** by mapping
`StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`, and by
preferring the filer's own stated `LiabilitiesAndStockholdersEquity` over a
reconstructed `liabilities + equity` wherever it exists. The stated total is the
filer's arithmetic; reconstructing it substitutes ours.

**The identity pass rate went from 78.6% to 99.9%.**

### 3. Industry-specific tags

Banks do not file the tags everything else files. There is no inventory and no
property-heavy asset base; there are loans, deposits and investment securities.
Looking for the retail tags on JPMorgan produced a page that was 93% "other" — a
grey rectangle where a balance sheet should be.

Two plausible-sounding guesses — `LoansAndLeasesReceivableNetReportedAmount` and
`TradingSecurities` — both returned **zero rows**. The tags that worked were
found by dumping the raw file and reading what was actually in it:

| Concept | Tag that works | JPM |
|---|---|---|
| Loans | `FinancingReceivableExcludingAccruedInterestAfterAllowanceForCreditLoss` | $1.47T |
| Investment securities | `DebtSecuritiesAvailableForSaleExcludingAccruedInterest` | $507B |

The `ExcludingAccruedInterest` suffix is not cosmetic. It comes from ASU 2016-13
(CECL), effective 2020, which changed how banks present receivables; the short
names most references still use are deprecated. Loans are 33% of JPMorgan's
balance sheet and were invisible until those two tags were mapped.

---

## The verification approach

This is the part that actually kept the numbers honest.

**A reference figure is only ever corrected against the raw filing, never
against the parser's own output.** Correcting a reference to match what the
parser produced is how a wrong number gets blessed and then defended. Midway
through this project a remembered figure for Microsoft's equity ($300B) was
contradicted by the parser ($390.9B). The parser was right — but it was only
allowed to be right after the raw `num.txt` rows were dumped and read. A test
enforces the rule: any reference marked `confirmed` must cite a raw dump.

**Five companies check the mechanism; the whole universe checks the result.**
JPM, MSFT, WMT, FCX and AAL are spot-checks — enough to catch a parser reading
the wrong fact, nowhere near enough to say anything about coverage. So the
identity also runs across every company with a complete balance sheet, bucketed
by drift and broken down by sector. A sector failing systematically is a tag
problem for that class of filer; scattered failures are noise. That breakdown is
what surfaced both the noncontrolling-interest gap and the bank tags.

**The reload fetches everything before it deletes anything.** An earlier version
wiped the table and then discovered SEC was returning 429 on every quarter:
1,231,927 rows deleted, zero written. The rule that came out of it is that the
delete and the reload share one transaction, and nothing is touched until every
quarter is on disk. A quarter SEC has not published yet (404) is skipped as
absent; a 429 aborts with the existing data intact. The guard that failed had
been placed on the *trigger* — a `confirm=true` parameter — rather than on the
operation, which stopped stray taps and did nothing whatever about the failure
that actually happened.

---

## Running it

FastAPI, Postgres, SQLAlchemy, deployed on Railway. Server-rendered HTML: the
pages are drawings of numbers already in the database, so they arrive complete
with nothing to hydrate. The only JavaScript is an optional question box.

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql://...
export SEC_USER_AGENT="Your Name your@email.com"   # SEC blocks blank UAs
uvicorn src.api:app --reload
```

Load the data — seven downloads, a few minutes:

```bash
python -m src.backfill --fundamentals --skip-bars   # or POST /admin/reload-fundamentals
pytest                                   # no network; synthetic ZIPs throughout
```

| Variable | |
|---|---|
| `DATABASE_URL` | Postgres. Falls back to SQLite for local work |
| `SEC_USER_AGENT` | Required — SEC blocks default and blank user agents |
| `OPENROUTER_API_KEY` | Optional. Without it the question box does not appear |
| `LLM_ASK_MODEL` | Optional. Resolved against OpenRouter's model list at startup |

`/admin` is the operations page: data health, the five-company verification, the
whole-universe identity check, the raw `num.txt` dump, and the reload. It found
every bug described above.

---

## What it deliberately doesn't do

No predictions. No scores. No recommendations. Nothing imputed — where a company
does not report something, the page names the missing line instead of showing a
zero.

That is a design decision, not a gap. The whole value of this thing is that
every number on screen traces to a specific filing on a specific date and a
reader can check any of them. A score is an opinion with the provenance stripped
off. Adding one would cost the only property that makes the rest worth trusting.

---

Data source: [SEC Financial Statement Data Sets](https://www.sec.gov/dera/data/financial-statement-data-sets).
Built by Dominique Church.
