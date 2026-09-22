# Homepage audit — Jakob's Law + copy checklist — 2026-09-22

Measured against the **live page** (`https://balanceproof.dev/`, fetched
2026-09-22, HTTP 200, 33,820 bytes, `charset=utf-8`), not against the source.
1,299 visible words in `<main>`, 18 headings, 32 links, 5 `.btn` elements.

Research only. Nothing in `home_page.py` was edited — the standing rule from
`homepage-review.md` is that nothing here is applied without a decision.

**The prior review's #1 finding is fixed.** It reported `class="btn"` inside
`<main>: 0`. The live page now has 5: two in the hero (`Get a free API key`,
`See how it works`) and three in a CTA bar above the footer. That was the
highest-value change available and it shipped.

---

## Part 1 — Jakob's Law

**Is the site too complicated for users? No.** Rendered at 390×844 and
1440×900, the page is conventional, fast to parse, and the phone fold does its
whole job. The complexity problem is not structural, it is four unfamiliar
words on familiar controls, and on desktop a stat block that pushes the proof
below the fold. Both are cheap.

> Users spend most of their time on other sites. They expect yours to work the
> same way.

### What the page gets right

Brand top-left, links right, hamburger below the breakpoint, `Sign in` at the
end of the nav. Hero is H1 → lede → search → primary/secondary button. Pricing
is a four-card row with a billing toggle and `Best for:` lines. FAQ link under
the cards. Footer carries Terms / Privacy / Support. A visitor who has used any
developer-tool site in the last five years knows where everything is.

### Four places the page asks the visitor to learn something new

**1. The search button says `Draw it`.**

```html
<button type="submit">Draw it</button>
```

This is the single most convention-bound control on the page and it is the one
carrying brand voice. Every search box this visitor has ever used says Search,
Go, or shows a magnifier. `Draw it` is charming, and it costs a beat of
hesitation at the primary interaction on the fold.

Fix: `Search`. The label above it already says "Ticker or company name", and the
JPM drawing directly below already tells them what they get.

**2. `Notes` is the blog's nav label.**

People scan a nav for Blog, Research, Resources, Docs. Nobody scans for Notes.
The three posts behind it are the strongest acquisition asset in the product
(per `positioning-review-2026-09-19.md` §"The real headline asset"), and they
are filed under a word that does not match any search the eye is running.

Fix: `Research`.

**3. The stat strip publishes two internal ops words.**
*(Touches the stat strip, which `homepage-review.md` records as explicitly
off-limits. Reported, not recommended past your decision.)*

```
Source · Facts 1.6M · Companies 6,238 · Latest filing 2026-09-21 ·
Reconciled 4,943 · Flagged with a reason 194 · Hidden 0 · Pipeline 6h ago
```

`Hidden 0` reads to a visitor as "things you are not showing me" — the exact
opposite of the claim the strip exists to support. No comparable site publishes
a Hidden counter, so there is no prior reading for it. `Pipeline` is an
engineering word for what a visitor calls "updated".

Fix: drop `Hidden` from the public strip (keep it on `/admin`, where it means
what it means), rename `Pipeline` → `Updated`.

**4. The pricing ladder is not in ascending order.**

`Free $0` → `Full dataset $79.99 once` → `Pro $49/month` → `Enterprise`. Users
scan a pricing row left-to-right expecting price to climb. Here the second card
is the largest number on the row and the third is a different unit. The
dataset-vs-API explainer above the cards does the work, but it does it in prose
above a row the eye has already scanned.

Lowest-cost fix: move `Full dataset` to position 3, after `Pro`. It is the
side-product, not the step between free and paid.

### Verified as non-issues

Three things in the raw markup look like bugs and are not. `Sign in`, `Log out`
and `Dashboard — log in to access your dashboard` all appear in the nav HTML
simultaneously — `whoami` is `hidden` and swapped by JS, and the dashboard
tooltip text is inside `<span class="vh">` (screen-reader only). The billing
toggle appears twice in the source; `.bill-toggle.in-card{display:none}`
(`dashboard.css:518`) leaves one visible, and the duplicate is the deliberate
no-JS fallback. The page serves zero U+FFFD characters.

---

## Part 2 — The checklist

### ✅ Handle objections before CTA — **PASS**

Textbook, and the best-sequenced part of the page. `What this doesn't do` (no
predictions, no scores, no recommendations, nothing estimated) → `Check it
against a filing you already know` → the CTA bar. The last objection handled is
"why should I believe you", and the answer given is "go check us against EDGAR",
which is the only answer with an authority behind it. `1,000 calls a month, no
card` sits next to the hero CTA. Leave this alone.

### ❌ Unnecessary info — **FAIL, three spots**

1. **The Freshness paragraph.** "The loader decides it is behind by asking the
   database, not by watching a clock, so a container that was down for a week
   closes the gap on its next tick instead of waiting for a schedule." This
   answers a question nobody asked and plants one they weren't asking — *your
   container was down for a week?* Cut to: "Every load starts from the last
   filing already in the database, so a gap of any length closes on the next
   run."
2. **"The tag names move too — the short names most people use are deprecated,
   and the modern bank tags carry an 'ExcludingAccruedInterest' suffix from a
   2020 accounting standard."** The point ("every industry files differently")
   already landed two sentences earlier. This is the third example of it.
3. **"Every tag was confirmed against the raw filing data before being used.
   Nothing was mapped on the strength of it sounding right."** Nobody suspected
   otherwise. Raising it is the only thing that makes it a question.

### ✅ Break up blocks of text — **PASS**

Sections, three-up `tblock` grids, cards, a drawing between every two argument
blocks. Longest paragraph is ~60 words. Nothing to fix.

### ✅ Scannable copy — **PASS, one exception**

The three headings under `Why this is hard` are meant to be scanned and one of
them is a sentence:

> Balance sheet items and income items are different kinds of fact

Eleven words, no noun the eye can grab. Its two neighbours ("Filings don't say
things once", "Every industry files differently") scan fine.

Fix: a noun label the eye can grab — `Point-in-time figures`. (Avoid `An
instant, not a period`: that is the two-beat antithesis this audit is asking the
section to shed.)

### ✅ Benefit before feature — **PASS, with one inversion**

The hero lede opens with what the product *is* before what the reader *gets*:

> BalanceProof is the verification layer over SEC EDGAR balance sheets: when a
> filing reconciles you get the figure, and when it does not you get the reason
> instead of a number that looks fine.

The benefit is the second half. A lazy reader takes the first eight words and
leaves with a category, not a reason.

Fix: "When a filing reconciles you get the figure. When it doesn't, you get the
reason instead of a number that looks fine." Drop "BalanceProof is the verification
layer over SEC EDGAR balance sheets" entirely; the H1 and the nav already said
it.

### ✅ Write for lazy people — **PASS**

The lazy path is H1 → drawing → stat strip → price, and all four are on it. The
one drag is the 34-word lede above; splitting it (see previous item) fixes this
item too.

### ❌ Generic openers — **PASS (nothing generic)**

No "In today's world". No "Welcome to". No "In an era of". The H1 is a claim
with a verb in it.

### ✅ CTAs: clear verb + outcome — **FAIL, four of nine**

| CTA | Verdict |
|---|---|
| `Get a free API key` | ✅ verb + outcome + price |
| `Call the API` | ✅ |
| `Buy the dataset` | ✅ |
| `Email us` | ✅ |
| `See how it works` | ✅ weak but fine as a secondary |
| `API docs` | ❌ noun in a button → `Read the API docs` |
| `How we verify` | ❌ noun phrase in a button → `See how we verify` |
| `Go Pro` | ⚠️ verb + brand, no outcome → `Start Pro at $49/month` |
| `Draw it` | ⚠️ see Jakob #1 |

One more: the hero says `Get a free API key` and the Free pricing card says
`Get a key` for the same destination. Same action, two labels, and the reader
has to decide whether they are the same thing. Unify on the longer one.

### ❌ Fabricated claims — **Nothing fabricated. One number that contradicts itself.**

This is the highest-value finding in the audit, because it is the one failure
mode this product cannot afford.

**The arithmetic on the page does not close.**

```
Stat strip:   Reconciled 4,943  +  Flagged with a reason 194   =  5,137
"The check":  "All 5,823 companies with a complete balance sheet are checked…"
```

686 companies apart, in the same viewport, on the page whose entire pitch is
that numbers reconcile. The audience for this product is the audience that adds
those two numbers together.

Neither number is wrong. They come from two different functions with two
different definitions of "checkable":

* `_compute_breakdown()` — `src/company/stats.py:451` — requires
  `total_assets`, `total_liabilities` **and** an equity tag. Anything else is
  `not_testable`. Produces 4,943 + 194.
* `run_universe_check()` — `src/company/universe_check.py:152` — also accepts
  the filer's own stated `liabilities_and_equity` total as the right-hand side.
  Produces 5,823.

The second is the better check (the comment at `universe_check.py:150` is right
that a stated total has none of the equity-tag ambiguity). The problem is only
that both are published, three inches apart, with nothing saying they measure
different populations. `stats.py:362` states the design intent — "so the page
and the picture cannot disagree about what 'reconciles' means" — and the
homepage is currently the place where they disagree.

Fix, cheapest first: render `The check` off `identity_breakdown()` so the
sentence reads `All 5,137 companies…` and the strip's two numbers add to the
sentence's one. If 5,823 is the number worth publishing, publish it in the strip
too and drop the other pair.

**Second item — a percentage the reader will attach to the wrong thing.**

```
The check:   "…any that do not balance are flagged with the reason."
one line below:   "Why XBRL data is wrong one time in five"
```

194 / 5,137 is 3.8%. The 1-in-5 figure measures something else entirely: a naive
first-tag XBRL extraction disagreeing with the consolidated fact, per
`positioning-review-2026-09-19.md`. A skimmer reads "1 in 5 of these are wrong"
directly under "194 flagged" and either distrusts the 194 or over-reads the
1-in-5. Both readings cost you.

Fix the link text, not the post: `Why a naive XBRL parse reads total assets
wrong one time in five`.

### ✅ Talk to one buyer — **PASS**

Per `positioning-review-2026-09-19.md`, the buyer is the developer or analyst who
can already pull free SEC JSON and is being sold the reconciliation layer. The
hero, the demo, the MCP line and the Pro card all hold that. The four `Best for:`
lines name four different readers, but that is the standard pricing-row pattern
and the reader self-selects from it — not a leak. No change.

### ✅ Specific headlines — **PASS, two collisions**

`SEC balance sheets that prove they balance.` is specific, falsifiable and
proved directly below it. Good.

Two headings are not distinguishable from each other by their words alone:
`Live from the filings` (the JPM drawing) and `Live demo` (the API console).
They are two sections apart and a scanner cannot tell which is which.

Fix: `Live from the filings` → `One real filing, drawn`. And `How it works` is a
generic label carrying nine sub-blocks — see *One idea per section*.

### ✅ Punchy, not padded — **PASS, three padded lines**

* "Generous — plenty to evaluate with. A free key gives you more." — says one
  thing twice in twelve words.
* The Freshness paragraph (already listed).
* "…written to be useful even where it does not conclude in my favour." A good
  line, hung off a link in a link list, where it slows a scan that should be
  three words wide.

### ❌ Two-beat antithesis — **FAIL, systemic**

This is the page's signature move and it fires at least ten times:

1. "when a filing reconciles you get the figure, and when it does not you get the reason"
2. "the same money, counted twice: once by what it is, once by who it belongs to"
3. "Both columns are the same money, counted twice" *(the same line again, as an h3)*
4. "One is a photograph, the other is a film."
5. "A balance sheet figure is an instant… Revenue is a duration"
6. "The dataset is a **photograph**… The API is a **window**"
7. "They look nothing alike because they are nothing alike."
8. "As reported, never restated"
9. "not what it said later about the same quarter"
10. "Nothing is scraped, bought, or estimated."

Plus the `X rather than Y` frame seven more times: "rather than quietly
balanced", "rather than showing zero", "instead of a number that looks fine",
"not by watching a clock", "instead of waiting for a schedule", "rather than
with a placeholder", "not what it said later".

Every one of these is good writing on its own. At this density the reader stops
hearing the argument and starts hearing the cadence, and the page reads as
performed rather than reported — which is expensive on a page selling
trustworthiness.

There is also a concrete collision: **"photograph" is used for two different
things two screens apart** — #4 uses it for a balance-sheet instant, #6 uses it
for the static CSV. A reader who learned the first meaning misreads the second.

Fix: keep three — the hero's (#1), the counted-twice line (#2, once, not twice),
and the photograph/window pair in pricing (#6). Flatten the rest to plain
statements, and rename #4 so the photograph metaphor is spent only once.

### ✅ 1–3 key bullets per section — **FAIL, one section**

`How it works` (h2) contains nine h3s:

```
h2 How it works
   h3 Both columns are the same money, counted twice
   h3 Why this is hard
      h3 Filings don't say things once
      h3 Balance sheet items and income items are different kinds of fact
      h3 Every industry files differently
   h3 Where the figures come from
      h3 Source
      h3 Freshness
      h3 As reported, never restated
      h3 The check
```

Everything from the schematic to the method links is one `<section>`. The
heading hierarchy is also flat — `Why this is hard` and `Filings don't say
things once` are both h3, so the outline gives a screen reader and a search
crawler no nesting at all.

Fix: three h2s — `What you're looking at`, `Why this is hard`, `Where the
figures come from` — and demote their children to h3. Costs one function split
in `home_page.py:920` and fixes the outline, the scan, and this checklist item
at once.

### ✅ One idea per section — same finding as above

Every other section carries one idea. Pricing carries the explainer, the toggle,
four cards, the Stripe note and the FAQ link, which is what a pricing section is
supposed to carry.

### ❌ Em dashes — **FAIL, 15 in the visible copy**

Counted on bytes, not on rendering: 15 × U+2014 in `<main>`, 0 replacement
characters, 0 en-dashes, 2 curly double quotes. (The separators in the hero
summary line and the method-link row are `&middot;`, not em-dashes.)

| Where | Line |
|---|---|
| How it works | `with the reason — never silently fudged` |
| Counted twice | `who has a claim on it — lenders first` |
| Why this is hard | `per company per quarter — broken out by segment` |
| Pricing lede | `what costs money — and it comes two ways` |
| Free card | `Unlimited — look up as many companies as you like` |
| Free card | `runs the real endpoint — generous` |
| Demo note | `Generous — plenty to evaluate with` |
| Dataset card | `One-time download — no updates` |
| Dataset card | `Data is fixed — it does not change` |
| Pro card | `Programmatic access — query any company anytime` |
| Pro card | `Data updates daily — you always get the latest filings` |
| Doesn't do | `Nothing estimated — where a company doesn't report something` |
| Why this is hard | `an instant — what was there on one day` |
| Why this is hard | `Revenue is a duration — what happened over three months` |

Almost every one is doing a colon's job or a period's job. Mechanical fix:
colon where the second half explains the first (`Unlimited: look up as many
companies as you like`), period where it is a second sentence (`One-time
download. No updates.`), middot for the two link separators.

### ❌ Aphorism formulas — **FAIL**

Sentences built to be quoted rather than to inform:

* "Both columns are the same money, counted twice."
* "One is a photograph, the other is a film."
* "They look nothing alike because they are nothing alike."
* "Nothing was mapped on the strength of it sounding right."
* "Data is fixed — it does not change."
* "…written to be useful even where it does not conclude in my favour."

`home_page.py:950` marks one of them in the source: `<!-- THE QUOTABLE
SENTENCE -->`. That comment is the diagnosis.

Keep the counted-twice line — it genuinely teaches the reader the one thing they
need to read the drawing. Flatten the rest.

### ✅ Proof next to the claim — **STRONG PASS, one exception**

The H1 claims filings "prove they balance" and directly below it is JPM's actual
Q2 2026 filing: both columns drawn to the same height, every line item named,
`$4.64T + $374.6B` against `$5.02T` of assets, with the filing date. The claim
and its evidence are in the same viewport, and the evidence is the product. This
is the single best decision on the page — on a phone. At 1440×900 the drawing
falls below the fold (see the item above), so the desktop visitor gets the claim
and has to scroll for the evidence.

The other exception is that the one section whose *job* is proving the check is the
section carrying the 5,823-vs-5,137 mismatch.

### ✅ Make above the fold sell — **PASS on phone, FAIL on desktop**

Rendered and looked at, rather than reasoned about. Both shots at scroll 0.

**390×844 (phone): pass, and it is the better of the two.** H1, lede, search
box, both buttons, `1,000 calls a month, no card`, and the JPM drawing already
starting. Everything visible is selling. The source comment at
`home_page.py:~470` is right about why.

**1440×900 (desktop): two problems, and they are the same problem.**

The stat strip sits above the H1 and does not render as the thin masthead rule
the source comment describes. It is a **two-row block about 180px tall**:
`Pipeline / 6h ago` wraps onto its own second row and leaves most of that row
empty. So a desktop visitor's first read is nine labelled figures in a box,
`HIDDEN 0` among them, before the headline.

And that 180px costs the thing the page is best at: **at 1440×900 the JPM
drawing is below the fold.** The fold lands on the `Live from the filings`
heading. The proof is one scroll away on desktop and immediately present on
phone, which is backwards from how this audience arrives.

Fix, without touching the strip's contents: let `Pipeline` sit on the first row
(it fits — there is empty space beside it), which reclaims ~70px and pulls the
top of the drawing back up to the fold line. If the strip cannot be made one
row, the higher-value move is to put it back below the hero on desktop too,
which the phone order already proves reads well.
*(Touches the stat strip, off-limits per `homepage-review.md`. Reported as a
measurement, not applied.)*

### ✅ Body supports the headline — **FAIL, one headline**

> ## Compare any two companies at true scale

The body under it is four fixed thumbnails (MSFT, WMT, FCX, AAL) and one blog
link. There is no way to compare two companies. The section's only links are
`/company/MSFT`, `/company/WMT`, `/company/FCX`, `/company/AAL` and
`/blog/why-bank-balance-sheets-are-different`.

Checked whether the feature exists and is merely unlinked: it does not.
`/compare/{slug}` (`api.py:3789`, `src/report/compare.py`) serves
*competitor*-comparison pages — BalanceProof vs Intrinio, vs sec-api — not
company-vs-company. `https://balanceproof.dev/compare` → 404.
`https://balanceproof.dev/compare/MSFT-vs-WMT` → 404. Grepped
`company_page.py`, `view1.py` and every file in `report/static/*.js` for
`vs=`, `compare`, `versus`: the only hits are prose comments and an admin
period-comparison view. There is no compare control anywhere, linked or not.

So the headline offers a capability the product does not have. That is the one
place on the page where the copy outruns the code, and it is worth fixing on
those grounds alone regardless of the checklist.

Two options:

* **Retitle to what the section actually does:** `Every company is a different
  shape` — which is also the truer claim, and the four drawings prove it
  instantly.
* **Or build the thing the headline promises.** A `/compare/AAPL-vs-MSFT` route
  drawing two `build_view1`s side by side is a small build on top of machinery
  that already exists, it is an obvious SEO surface, and it would make the
  existing headline honest.

Everything else supports its headline: `Live demo` → a working endpoint,
`What this doesn't do` → four things it doesn't do, `Pricing` → prices.

---

## Scorecard

| Item | Verdict |
|---|---|
| ✅ Handle objections before CTA | **Pass** — best-sequenced part of the page |
| ❌ Unnecessary info | **Fail** — 3 spots |
| ✅ Break up blocks of text | **Pass** |
| ✅ Scannable copy | **Pass** — 1 heading to fix |
| ✅ Benefit before feature | **Fail** — the most-read line is inverted |
| ✅ Write for lazy people | **Pass** |
| ❌ Generic openers | **Pass** — none |
| ✅ CTAs: verb + outcome | **Fail** — 4 of 9 |
| ❌ Fabricated claims | **Pass on fabrication** — but 5,823 vs 5,137 |
| ✅ Talk to one buyer | **Pass** |
| ✅ Specific headlines | **Fail** — 2 headings collide |
| ✅ Punchy, not padded | **Pass** — 3 padded lines |
| ❌ Two-beat antithesis | **Fail** — systemic, ~10 instances |
| ✅ 1–3 bullets per section | **Fail** — 9 h3s under one h2 |
| ✅ One idea per section | **Fail** — same section |
| ❌ Em dashes | **Fail** — 15 |
| ❌ Aphorism formulas | **Fail** — 6 |
| ✅ Proof next to the claim | **Pass on phone** — desktop scrolls for it |
| ✅ Above the fold sells | **Pass on phone, fail at 1440×900** |
| ✅ Body supports the headline | **Fail** — 1 headline promises a missing feature |

## If only five things get changed

1. **Reconcile 5,823 with 4,943 + 194.** One function, one number. It is the
   one error this product cannot be seen making.
   *(One-line change at `home_page.py:936`.)*
2. **Retitle `Compare any two companies at true scale`** — or build the route.
   The copy currently outruns the code.
3. **Cut the antithesis count from ten to three,** and spend the "photograph"
   metaphor only once.
4. **Split `How it works` into three h2s.** Fixes the scan, the outline and two
   checklist items in one edit.
5. **`Draw it` → `Search` and `Notes` → `Research`.** Two words, two fewer
   things the visitor has to learn. Dropping `Hidden` and unwrapping
   `Pipeline` would be the third and would buy back the desktop fold, but both
   touch the stat strip you ruled off-limits, so they need your call first.

Items 3, 4 and 5 are hours. Item 1 is one line. Item 2 is a decision, then
either a string or a route.

Screenshots this was measured from:
`scratchpad/fold_desktop.png` (1440×900) and `scratchpad/fold_phone.png`
(390×844), both at scroll 0, fetched 2026-09-22.


---

# Applied — localhost preview, 2026-09-22

Running at `http://127.0.0.1:8099/` against a seeded SQLite copy
(`scripts/seed_demo_fundamentals.py`, the five tickers the homepage uses).
Nothing committed, nothing deployed.

**Verified on the rendered page after the change:** em-dashes in visible
`<main>` 15 → **0**; `Reconciled 4 + Flagged 0` now equals the sentence's
`All 4 companies` (was `All 5`); ten `h2`s with nested `h3`s where nine `h3`s
sat under one `h2`; `tests/test_home_and_search.py`,
`test_billing.py`, `test_accounts.py`, `test_llms_full.py` — 181 passed.

### `src/report/home_page.py`

| # | Change | Original |
|---|---|---|
| 1 | Hero lede opens on the benefit | `BalanceProof is the verification layer over SEC EDGAR balance sheets: when a filing reconciles…` |
| 2 | Search button `Search` | `Draw it` |
| 3 | `One real filing, drawn` | `Live from the filings` |
| 4 | `…says so with the reason. Never silently fudged.` | same words, em-dash before `never` |
| 5 | `The check` counts off `identity_breakdown()` | `ident.get("checkable")` |
| 6 | Freshness rewritten, one sentence | `The loader decides it is behind by asking the database, not by watching a clock, so a container that was down for a week closes the gap on its next tick instead of waiting for a schedule.` |
| 7 | `Why this is hard` → `h2` | `<h3 class="how-h">` |
| 8 | `Where the figures come from` → `h2` | `<h3 class="how-h">` |
| 9 | Cut tag-provenance line | `Every tag was confirmed against the raw filing data before being used. Nothing was mapped on the strength of it sounding right.` |
| 10 | `Point-in-time figures`, one photograph only | `Balance sheet items and income items are different kinds of fact` / `One is a photograph, the other is a film.` |
| 11 | Cut deprecated-tag aside | `The tag names move too — the short names most people use are deprecated, and the modern bank tags carry an “ExcludingAccruedInterest” suffix from a 2020 accounting standard.` |
| 12 | `Every company is a different shape` | `Compare any two companies at true scale` |
| 13 | `…own proportions, so no two of them look the same.` | `They look nothing alike because they are nothing alike.` |
| 14 | `Read the API docs` / `See how we verify` | `API docs` / `How we verify` |
| 15 | Five em-dashes → colons or full stops | see the em-dash table above |

### `src/report/pricing_page.py` — also changes `/pricing`

`Go Pro` → `Start Pro`. `Get a key` → `Get a free API key` (one label per
destination; the hero already used the longer one). Six card em-dashes to
colons or full stops, and the free card's demo bullet no longer repeats the
demo section's own sentence.

### Stat strip — OFF-LIMITS per `homepage-review.md` (2026-09-19)

Applied **for preview only**, so the effect can be seen and then kept or
reverted:

* `Hidden` cell removed from the public strip. The count is unchanged in the
  data and still on `/admin`.
* `Pipeline` label → `Updated`.

**This is what bought back the desktop fold.** At 1440×900 the strip was a
two-row, ~180px block because `Pipeline / 6h ago` wrapped. One cell shorter, it
fits on one row at ~90px — and the JPM drawing, which was below the fold, is
now above it. If the strip is to stay as it was, the fold finding stands and
needs a different fix.

### Not applied

* **Pricing card reorder** (Jakob #4) — layout rather than copy, and it was not
  in the five.
* **The `1 in 5` link text.** `Why XBRL data is wrong one time in five` is the
  blog post's own title; retitling the link alone would make the two disagree.
  Needs a decision on which to change.
* **The wider antithesis cull.** Four flattened; `As reported, never restated`,
  `Nothing is scraped, bought, or estimated`, `the same money, counted twice`
  and `never silently fudged` were kept — the last on purpose, it is the
  subject of commit `0bc39c2`.

### The one number question left for you

`The check` now reads from the same function the strip reports, so the three
figures agree. In production that moves the sentence from **5,823 to 5,137**,
because `identity_breakdown()` does not count the ~686 filers that
`run_universe_check()` can check from their own stated
`liabilities_and_equity` total. This audit called that stated-total path the
better check.

The alternative is to teach `_compute_breakdown()` the same fallback, so all
three figures agree at **5,823** instead. That is the better check and the
bigger number, and it is a real change to `src/company/stats.py` rather than a
one-line render change. Your call.


---

# Round 2 — site-wide consistency, lag, and cookies — 2026-09-22

## The lag was not the background

There is no WebGL on this site, and no video. `backdrop.py` replaced the film
with one still WebP (49 KB at 2400px) some time ago, so there was nothing to
turn into an image: it already was one.

The cost was `backdrop-filter`. `backdrop.css` put
`blur(16px) saturate(1.15)` on `main .sec`, which is every section on every
page. On the home page that is **nine full-width elements, ~5.3M px² per
frame, about four viewports** of live re-blur.

Measured at 1440×900, scrolling top to bottom:

| | median | p95 | frames over 20ms |
|---|---|---|---|
| blur on | 33.3ms (**30fps**) | 50ms | **61%** |
| blur off | 16.7ms (**60fps**) | 16.8ms | **0%** |

Identical under 4× CPU throttling, which is what proves it was the compositor
and not script.

**The rule had already outlived its reason.** Its own comment says the blur
existed to hold contrast steady under text *"against a moving glow, because
the contrast under any given word now changes as the light drifts past it"* —
written when the backdrop was a playing film. The backdrop stopped moving; the
blur kept paying for it.

`glass.css` had been careful about exactly this ("FOUR panes on the whole site,
and never more than three in one viewport", `.trust`/`.plans` explicitly set to
`backdrop-filter: none`). `backdrop.css` loads later and put the blur back on a
broader selector, at a bigger radius, with the `saturate()` that `glass.css` had
deliberately dropped.

### What replaced it

`main .sec` is now a flat `rgba(20, 22, 42, .85)`. Neither number is a guess:

* **The colour** was solved from the render. The blurred panel measured
  rgb(19.3, 22.0, 43.0) against a bare backdrop of rgb(16.8, 20.3, 51.0), so
  the fill that reproduces the same result at α=.85 is rgb(20, 22, 42).
* **The alpha** is set by striping, not taste. Blur was also smoothing the
  backdrop's reeded texture. Mean luminance step between adjacent columns over
  an empty part of the panel: blurred **0.153**, flat at .60 **0.892** (plainly
  visible), flat at .85 **0.337** — a third of one luminance level. .85 is
  where it stops reading, and it is still lighter than the .94 the inner panels
  use, so the two depths of glass survive.

The nav and the stat strip keep their blur: 170,760 px² between them, small and
sticky, and that is where glass actually reads. Blurred area is down **97%**.

`scripts/check_contrast.py`: 0 deterministic failures. All pages 59.9fps.

## Cookies: none needed, and one would cost you

* **No `Set-Cookie` on any marketing page.** Verified against the live site.
* **Two cookies exist, both strictly necessary** (`src/auth.py`): a signed
  session and a one-shot new-key carrier. Both `HttpOnly`, `Secure`,
  `SameSite=Lax`, and set only once somebody signs in.
* **Analytics is server-side and IP-keyed** — no client-side tracker, and
  `analytics.hash_ip` stores a salted digest rather than the address.
* **Zero third-party trackers.** No GA, no tag manager, no Segment, no pixel.

Strictly-necessary cookies are exempt from consent under ePrivacy/GDPR, so no
banner is required. Adding one would introduce a consent gate in front of a
site that has nothing to consent to — a conversion cost paid for no compliance
benefit. `/privacy` already documents all of this, including the hashing.

The only live obligation is the one already met: IP-derived data is personal
data, and it is disclosed.

## Site-wide copy consistency

The checklist was applied to every public page, not just the home page.

| Page | em-dashes before | after |
|---|---|---|
| `/` | 15 | **0** |
| `/methodology` | 18 | **0**\* |
| `/pricing` | 10 | **0** |
| `/api` | 6 | **0** |
| `/about` | 4 | **0** |
| `/dataset` | 3 | **0** |
| `/blog` | 2 | **0** |

\* four remain on `/methodology`, all of them the `—` that stands for "no
value" in the flag table. That is typography, not prose, and it stays.

Docstrings and `<title>` strings keep their dashes throughout: neither is body
copy, and a dash in a page title is the convention every search result uses.

Files touched: `about_page.py`, `methodology_page.py`, `api_page.py`,
`pricing_page.py`, `dataset_page.py`, `blog.py`, `backdrop.css`.

**Kept on purpose:** `A photograph.` / `A window.` on `/pricing`. It is the one
antithesis that does real work — it is the answer to the only question that
page exists to settle — and the home page keeps the same pair, so the two now
agree.

**Not done:** the blog *posts* themselves. The index and deks are clean, but
the essays are long-form editorial where a dash is ordinary punctuation rather
than a tic, and rewriting them is a different job from tightening landing copy.
Say the word if you want them done too.
