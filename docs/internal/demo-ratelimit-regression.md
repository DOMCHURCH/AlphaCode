# The demo rate-limit regression, in plain English

*13 September 2026*

## What I tried to do

Our free demo returns the same data the paid API does, and until today anyone could call it as often as they liked — so the thing we charge $49 a month for was free to anyone patient enough to ask repeatedly. The fix seemed simple: let any one visitor make 100 calls an hour. Far more than a real person needs, far less than someone copying the database.

## What actually happened

The limit was supposed to apply to each visitor separately. Instead it applied to everyone at once: one shared allowance of 100 calls an hour for the entire site, not 100 each. My own verification used up all 100, and from that moment every visitor to the homepage got an error instead of a demo.

## How it was caught

The test looked perfect: 101 calls, the first 100 worked, the 101st was refused with the right message. That is exactly what success looks like — if you only ever test from one place. What exposed it was checking a note I had written claiming the limit would be easy to sidestep. I tried, and couldn't. That only made sense if everyone shared one allowance, which they did.

## Why the automated tests didn't catch it

They confirmed the counting works. They never confirmed we can tell visitors apart. They run against a simulated site with no real network in front of it, where visitors genuinely do look different. In reality our hosting provider sits in front, and behind it every visitor looks identical to us. The tests proved the lock works, not that we can tell who holds the key.

## What was done

Rolled back within minutes. The limit is off, the demo works again, and I confirmed it against the live site. Production is healthy and nothing else from today was affected.

## What's still broken

Two things. First, the original problem is back: the demo is still copyable, because the only protection is a site-wide ceiling rather than a per-visitor one. Second — and this predates today — the visitor numbers in the admin panel are wrong for the same underlying reason. If we can't tell visitors apart to limit them, we can't count them either. Those figures have been unreliable for a while.

## What the fix requires

We need to find out what our hosting provider actually tells us about who is making each request. There is a standard way to switch that on, but I want to confirm what genuinely arrives before building on it. Guessing is what caused this.

## The risk if we don't fix it

Between now and the DevHunt launch, the demo stays copyable — and launch day is exactly when a technical audience is most likely to notice and say so publicly. Separately, every visitor number we quote is currently wrong, so we would be judging the launch on figures we can't trust.
