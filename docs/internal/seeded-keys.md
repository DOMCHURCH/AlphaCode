# Seeded keys

## What seeded keys are

Keys issued by hand to people who have no account here: a partner, a reviewer,
the author of a tool that would be better with this data in it. They are sent a
working key in a message and never meet a signup form, a card field or an email
confirmation. The key itself is identical to a paid one — same generator, same
SHA-256 digest, same 43 characters, unrecoverable in the same way — because a
key that looked different would need its own verification path, and that is the
path that ends up with the bug in it. What differs is everything around it:
there is no `api_users` row, no Stripe customer, no subscription and no expiry.
They live in `seeded_keys`, which has **no foreign key in either direction**, so
"how many customers are there" and "who did we send a key to" keep one answer
each. Their allowance is a per-key number set at issue time rather than a tier,
and they are metered on their own row because `usage_logs.user_id` is a foreign
key into `api_users` and there is nothing over there to point at.

## How to issue one

From the admin panel: **+ Issue new seeded key** under the Customers card.
Fill in the label, the display name, the limit and any notes, and the key is
shown once in a copyable box. That form posts to
`POST /api/admin/seed-keys/issue`, behind the admin secret and — once one is
configured — a TOTP code.

Or from a terminal, which is the way to do it when the panel is unreachable:

```
python scripts/issue_seed_key.py \
    --label "stefano-sec-edgar-mcp" \
    --display-name "Stefano Amorelli — sec-edgar-mcp" \
    --rate-limit 10000 \
    --notes "MCP server author, reached out about EDGAR coverage"
```

`--rate-limit` and `--notes` are optional; the limit defaults to 10,000 calls a
month. `--display-name` is optional and defaults to the label.

The key is printed **once**. The database holds a digest and nothing can read
it back, here or from a dump. If it is lost, revoke that label and issue
another — which is also what a paying customer has to do.

## How to revoke one

```
python scripts/revoke_seed_key.py --label "stefano-sec-edgar-mcp"
```

Takes effect on the next request. The row is stamped, never deleted.

## How to check usage

The `/admin` dashboard lists every seeded key under the customer counts, and
`/api/admin/stats` carries the same data as JSON:

```
curl -H "X-Admin-Secret: $ADMIN_SECRET" https://toscale.pro/api/admin/stats
```

- Each row shows the display name, the limit, when it was issued, when it was
  last used and whether it is revoked.
- Active and revoked are counted separately. Neither is added to
  `total_users` or to the revenue estimate — a seeded key is not a signup, and
  a dashboard that says the business is bigger than it is, is worse than none.
- The panel highlights a live key with **no `last_used_at`** in `warn`.
  Outreach that did not land is the thing worth revoking: an unused credential
  is all cost and no benefit.

## Rules of engagement

- Issue from the panel or the CLI. Both call one function and both are behind
  the admin secret; the panel is additionally behind TOTP when one is set.
- One key per recipient. The label is their handle, it is unique, and it is
  what revocation takes — so it must not be edited after the fact.
- `--display-name` should read at a glance: `"Name — Project"`. It is prose and
  may be changed freely; that is why it is a separate column from the label.
- Free for as long as it is live, ~10,000 calls a month by default. There is no
  expiry: revoking is a decision somebody makes, not a date that passes.
- Review the list periodically and revoke anything unused after about 30 days,
  or whose recipient has gone quiet.
- A seeded key cannot download the dataset. `/api/download-dataset` answers
  402, the same answer any free account gets, because that is a purchase.
- `/api/user/status` reports `"tier": "seeded"` with the override, so the
  recipient sees their real access rather than being told they are on the free
  tier.

## What not to do

- **Do not add a route outside `/api/admin/` that touches these.** This rule
  used to read "do not add an HTTP endpoint that issues these", and the panel
  form is that endpoint — added on purpose, once `require_admin` carried a
  second factor, which makes reaching it cost a leaked secret *and* a device.
  `test_only_the_admin_routes_can_issue_a_seeded_key` pins what survived: every
  route whose path mentions seeded keys sits under the admin gate, and
  `test_issue_seed_key_requires_admin_secret` proves the gate fires before
  `issue` is reached rather than after.
- **Do not change the format of keys issued to real users.** The two share one
  generator on purpose.
- **Do not expose seeded keys via `/api/auth/me`.** That route is cookie-session
  auth; an API key means nothing to it and the answer must stay a 401.
- **Do not delete a row on revoke.** Set `revoked_at`. "We gave this person a
  key and took it back" is the fact worth having in six months, and a deleted
  row says nothing at all.
- **Do not give `seeded_keys` a foreign key to `api_users`,** in either
  direction. The decoupling is the design, not an oversight.

## Where the code is

`src/seedkeys.py` (issue, revoke, find, usage, listing) · `SeededKey` in
`src/storage/models.py` · `Account.is_seeded` and the lookup fallthrough in
`src/accounts.py` · the two `scripts/` wrappers, which are the only callers of
issue and revoke · the seeded-key block at the end of `tests/test_api.py`.
