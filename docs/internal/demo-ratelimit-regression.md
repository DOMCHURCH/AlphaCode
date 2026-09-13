# The demo rate-limit regression, in plain English

*13 September 2026*

## What I tried to do

Our free demo returns the same data the paid API does, and until today anyone could call it as often as they liked — so the thing we charge $49 a month for was free to anyone patient enough to ask repeatedly. The fix seemed simple: let any one visitor make 100 calls an hour. Far more than a real person needs, far less than someone copying the database.

## What actually happened

The limit worked. I concluded it didn't, and rolled back a working feature.

My test used up a visitor's full hourly allowance, which is exactly what it was meant to do. Then I checked whether the limit could be cheated by faking the sender's address — and it couldn't, which I misread as evidence that everyone shared one allowance. It wasn't evidence of anything. Our host discards a faked address and substitutes the real one, so my "second visitor" was never a second visitor. The refusal I got was the correct answer to the same visitor asking again.

## How the wrong conclusion was reached

The first test looked perfect: 101 calls, the first 100 worked, the 101st was refused with the right message. Then I tried to cheat the limit and failed, and read that failure as proof of a fault rather than proof it was working. One observation didn't fit, I had no explanation for it, and instead of going and getting one I treated a guess as settled and acted on it.

## What the automated tests did and didn't tell us

There was nothing to catch — the tests were right and I was wrong. Worth noting anyway: they run against a simulated site with no real network in front of it, so they could not have settled the question either way. The thing that settled it was a two-minute check against the live site, which I should have run before rolling anything back.

## What was done

I rolled the limit back within minutes, which turned out to be unnecessary — it had been working. It is now restored, the demo is healthy, and the only lasting cost was a short window where the demo refused visitors because my own testing had used up an allowance.

## What's still broken

Nothing, from this. The limit is back on and doing its job, and the visitor numbers were never affected — that claim came from the same mistake and is withdrawn.

One unrelated thing was found along the way and has been fixed: the "ask a question" box on each company page was counting all visitors as one, so the first ten questions anyone asked used up the hour for everybody. That was a genuine bug, and it predated all of this.

## What the fix requires

Nothing further. The question — what our host tells us about who is making each request — is answered, and the answer was that it already told us correctly. See below for how it was confirmed and how to confirm it again.

## What this leaves behind

The specific risks named here no longer apply: the demo is protected again and the visitor numbers were never wrong. The risk this leaves behind is a different one, and it is about process rather than code — I rolled back a working feature on a hunch I could have checked in two minutes, and briefly made the live site worse doing it. The lesson is the one in the last section: verify before acting, and never from a single vantage point.

## How this was resolved

A temporary diagnostic page was added and opened from a laptop on WiFi. The visitor address our code worked out — 70.51.62.205 — matched that laptop's real public address exactly, checked independently against ipify. Our host had been passing the right information all along; it discards any address a visitor claims for themselves and substitutes the real one. The per-visitor limit was restored in commit `1c5410d`, and the diagnostic page was removed in `66def74`.

## How to test this correctly going forward

Do not test by faking the sender's address. Our host throws that away, so the test measures their equipment rather than our code — that is precisely the mistake that caused the rollback. Test instead from two genuinely different connections, such as a laptop on WiFi and a phone on cellular, and compare the address each one reports.
