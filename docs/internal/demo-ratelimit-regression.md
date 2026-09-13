# The demo rate-limit regression, in plain English

*13 September 2026*

## What I tried to do

The site has a free demo that returns the same data our paid API returns, and until today anyone could call it as many times as they liked. That meant the thing we charge $49 a month for could be taken for nothing by anyone patient enough to ask repeatedly. The fix was meant to be simple: let any one visitor make 100 calls an hour, which is far more than a real person browsing would ever need, and far less than someone copying the whole database.

## What actually happened

The limit was supposed to apply to each visitor separately. Instead it applied to everyone at once. There was one shared allowance of 100 calls an hour for the entire site, not 100 each. My own verification used up all 100, and from that moment every visitor to the homepage got an error instead of a demo.

## How it was caught

The test I ran looked perfect. I made 101 calls, the first 100 worked, the 101st was refused with the right message. That is exactly what success looks like — if you only ever test from one place. What actually exposed the problem was checking a note I had written in the code claiming the limit would be easy to sidestep. I tried to sidestep it, and couldn't. That was wrong in a way that only made sense if everyone shared one allowance, which turned out to be true.

## Why the automated tests didn't catch it

The tests confirmed the counting mechanism works. They never confirmed we can tell visitors apart. They run against a simulated version of the site with no real network in front of it, where every visitor genuinely does look different. In the real world our hosting provider sits in front of the site, and behind it every visitor looks identical to us. The tests proved the lock works; they never proved we can tell who is holding the key.

## What was done

Rolled back within minutes of finding it. The limit is switched off, the demo works normally again, and I confirmed that against the live site. Production is healthy. Nothing else from today's work was affected.

## What's still broken

Two things. First, the original problem is back: the demo is still copyable, because the only protection is a site-wide ceiling rather than a per-visitor one. Second — and this one predates today — the visitor numbers in the admin panel are wrong for the same underlying reason. If we can't tell visitors apart for rate limiting, we can't count them either. Those figures have been unreliable for a while.

## What the fix requires

We need to find out what information our hosting provider actually passes along about who is making each request. There is a standard way to switch that on, but I want to confirm what genuinely arrives before building on it — guessing is what caused this.

## The risk if we don't fix it

Between now and the DevHunt launch, the demo stays copyable. Launch day is exactly when a technical audience is most likely to notice and say so publicly. Separately, every visitor number we quote — to ourselves or anyone else — is currently wrong, so we'd be judging the launch on figures we can't trust.
