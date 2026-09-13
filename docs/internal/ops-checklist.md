# Ops checklist

Manual steps that live outside the repository. Nothing here can be done by a
deploy, which is exactly why it is written down: each one is a change made in
a console somebody has to remember to open.

## www subdomain — manual DNS setup

`www.balanceproof.dev` does not resolve. It is not a redirect and not a 404 —
the connection fails outright, because no record exists. Anyone who types or
links `www.` reaches nothing, and whatever authority that link carried is lost
rather than forwarded to the apex.

`www.toscale.pro` is in the same state, so the retired domain's redirect covers
the apex only. The 301 middleware in `src/api.py` already matches
`*.toscale.pro`, so the www leg starts working the moment DNS points at the
service — there is no code change waiting behind this.

Steps:

1. Railway → the web service → **Settings → Networking → Custom Domain**
2. Add `www.balanceproof.dev`
3. Railway returns a CNAME target
4. At the DNS registrar, add a CNAME record: `www` → `<railway target>`
5. Repeat for `www.toscale.pro` if the old domain's redirect is being kept
6. Wait ~10 minutes for propagation, then verify:

```
curl.exe -I https://www.balanceproof.dev/
```

Expect `200`, or a `301` to the apex if Railway is set to redirect www. A
connection failure or an empty reply means DNS has not propagated yet or the
CNAME is wrong. `curl -I` is a usable check here because HEAD now answers with
the real status rather than 405.

Check both hosts resolve to the service rather than to a local gateway. A
registrar or ISP that answers NXDOMAIN with a redirection address makes
`nslookup` look successful while nothing is actually served:

```
nslookup www.balanceproof.dev
```

An address on a private range (`192.168.x.x`, `10.x.x.x`) is that failure, not
a working record.

## Company-page JSON-LD after a domain change

`company_page_extras.jsonld` is rendered once and stored, so it keeps the
origin it was built with. A rebrand moves the canonical tag and `og:url` with
the code and leaves every stored block naming the old host.

The boot log says which state the table is in:

- `page_extras_origin_ok` — every row names the current origin
- `page_extras_origin_drift` — with the row count, a sample of tickers, and
  the endpoint that repairs it

To repair:

```
POST /admin/page-extras/backfill      (X-Admin-Secret)
```

Run it inside the cluster, not from a laptop: `scripts/backfill_page_extras.py`
does the same work, but over the public TCP proxy each ticker costs 4–8 seconds,
which is 7–14 hours for the universe.

`_refresh_page_extras` treats origin drift as staleness, so this rebuilds the
drifted rows. Before that it compared `source_period_end` only — and since a
rebrand changes no filing data, it would have rebuilt nothing and reported a
successful run.
