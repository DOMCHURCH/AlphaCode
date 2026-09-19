# Outreach: the 1-in-5 finding

**Written 2026-09-19.** For the cold-email push. The blog post already exists
at `/blog/sec-xbrl-data-wrong-one-in-five` — this is how to use it.

## The premise

You are not pitching. You are sending somebody a **bug report about data they
already rely on**, and the product is the fix. That is the only reason a cold
email from a stranger gets read, and it is a position almost nobody else can
take because almost nobody else has measured this.

The finding: a naive XBRL first-tag extraction disagrees with the consolidated
figure roughly **one time in five**, because SEC stores the consolidated fact
as the one with *no* dimensions attached — a figure defined by an absence, and
the easiest thing in the world for a parser to miss. JPMorgan reports "total
assets" twenty-three times in one filing. Take the wrong one and you get
$641bn instead of $4.4tn, and nothing about it looks wrong.

## Targeting, best first

**1. GitHub — people whose code has the bug.**

This is the strongest list and nobody else is working it. Search for public
code that parses EDGAR XBRL:

- `edgartools`, `sec-edgar-downloader`, `python-xbrl`, `sec-api` in
  `requirements.txt` / `package.json`
- code search for `companyfacts` together with `[0]` or `next(` — the
  first-match pattern
- `us-gaap:Assets` handled without a dimensions check

These people are **demonstrably** doing this work and **demonstrably**
exposed. You can often confirm the bug in their actual code before writing,
which turns a cold email into a specific, checkable claim about their repo.
That is not spam by any reasonable definition.

**2. Customers of the paid alternatives.**

Anyone quoting sec-api.io, FMP or EODHD in a blog post, README, or job ad is
already paying for SEC data. Search those names on GitHub, on X, and in job
descriptions. They have a budget and a current vendor.

**3. Small systematic funds and fintech data teams.**

Harder to find and slower to close, but the ones who care most. Job ads
mentioning "XBRL", "EDGAR" or "fundamental data pipeline" identify the team
and often the person.

Fifty well-chosen names beats five hundred scraped ones. Send them by hand.

## The email

Subject lines — the finding, never the product:

- `your EDGAR parser and JPMorgan's 23 "total assets" tags`
- `one in five XBRL extractions returns the wrong figure`
- `quick data note on <THEIR TICKER / their repo>`

Body. Keep it this short:

> Hi <name>,
>
> I saw <specific thing — their repo, their post, their job ad>. I've been
> measuring XBRL extraction accuracy against SEC filings and found something
> you may already know about, or may not:
>
> SEC stores the consolidated figure as the fact with *no* dimensions on it.
> Segment, geography and legal-entity breakdowns are stored identically.
> JPMorgan reports "total assets" 23 separate times in one filing, and exactly
> one of them is the company. A first-match extraction takes the wrong one
> roughly **one time in five** — you get $641bn instead of $4.4tn, and nothing
> about it looks wrong. Your parser runs clean, the JSON has a number in it,
> the backtest returns a Sharpe ratio.
>
> Method and the numbers are here:
> https://balanceproof.dev/blog/sec-xbrl-data-wrong-one-in-five
>
> I check every filing against Assets = Liabilities + Equity and flag the ones
> that don't reconcile instead of silently adjusting them. If it's useful,
> here's <THEIR TICKER> drawn from the filing:
> https://balanceproof.dev/company/<TICKER>
>
> And if you ever find a figure of mine that disagrees with the filing, tell
> me — that's the one bug report I actually want.
>
> — Dominique

**Rules for it:**

- **One specific thing about them in the first line.** Without it this is
  spam and reads like it.
- **Link the company page, not just the blog.** The blog is the argument; a
  drawing of a ticker they know is the proof, and it is the thing that takes
  ten seconds to check.
- **Never mention price.** The ask is a look, not a purchase.
- **No follow-up sequence.** One follow-up after a week, maximum, and only if
  you have something new to say.

## Why the ticker matters

Pick a company they actually work on and check it first. If it reconciles
cleanly, that is the demo. If it does *not*, that is a better email — you are
telling them something specific and verifiable about data in their own
pipeline.

Do not send a flagged one without reading it first. Per
`docs/internal/missing-tag-149-diagnosis.md`, 58.8% of the flagged set is
SPAC-shape — a structural artefact, not an accounting problem. Sending
"your company doesn't reconcile" about a SPAC tagging quirk is the one
mistake that would cost the credibility the whole email depends on.

## What success looks like

Not a conversion rate. **Replies.** Fifty hand-written emails that produce
five conversations is working; the same fifty producing one signup and no
conversations is not, because the conversations are where you learn what
these people would actually pay for — which is still the thing nobody knows.
