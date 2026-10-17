# Stripe test-mode plan: chargeback, superseded subscription, refund

Three checks of `src/billing.py` against real Stripe **test-mode** events.
Written 2026-10-17 on `fix/audit-batch-a` (the earlier copy was lost with a
closed session). Run it yourself; nothing here has been run against Stripe.

What the code does is read from `src/billing.py`; the log lines and database
rows below were also **rehearsed offline** on 2026-10-17 by sending correctly
signed fake events to the real `/api/billing/webhook` route on a scratch
SQLite file. What was *not* rehearsed is anything Stripe-side: CLI flag
spellings, how long a dispute takes to appear, and the dispute `reason` text.
Those are marked **(unrehearsed)**.

## Rules

- Test mode only. Every secret you paste must start with `sk_test_`.
- No `stripe trigger charge.dispute.created`. It builds its own customer, which
  is not linked to any local account, so it can only ever produce
  `stripe_reversal_unknown_customer` and looks like a bug that is not one.
- Nothing is pushed or deployed. The database is a throwaway file.

## Three windows

| Window | Job |
|---|---|
| W1 | the server (holds the secret key) |
| W2 | `stripe listen` (forwards events, shows the HTTP status of each) |
| W3 | Stripe CLI commands and the database helper |

Every window starts with `. C:\Users\Dominique\stripe-test\env.ps1` (step 1).
That one file is the only place the database path is written, and both the
server (W1) and `scripts\stripe_test_helper.py` (W3) read it from the same
`DATABASE_URL`. The helper refuses a relative path and refuses a file that does
not exist, because opening a relative SQLite path from the wrong folder quietly
creates a new empty database and every check then "passes" against nothing.

## 1. One-time setup (W3)

Keep the file ASCII-only (Windows PowerShell 5.1 reads scripts as ANSI).

```powershell
Set-ExecutionPolicy -Scope Process Bypass
New-Item -ItemType Directory -Force C:\Users\Dominique\stripe-test | Out-Null
@'
$Repo = "C:\Users\Dominique\AlphaCode-fixa"
$Dir  = "C:\Users\Dominique\stripe-test"
$DB   = Join-Path $Dir "bp-stripe-test.db"
$Py   = "$HOME\alphacode-testenv\Scripts\python.exe"
$env:DATABASE_URL = "sqlite:///" + ($DB -replace '\\','/')
$env:PYTHONPATH   = "$HOME\alphacode-testenv\shim;$Repo"
$env:ENV = "dev"
$env:SESSION_SECRET = "stripe-test"
$env:ADMIN_SECRET = "stripe-test-admin-secret-0123456789"
$env:AGENTMAIL_API_KEY = ""
if (Test-Path "$Dir\prices.ps1") { . "$Dir\prices.ps1" }
Set-Location $Repo
'@ | Set-Content -Encoding ASCII C:\Users\Dominique\stripe-test\env.ps1

. C:\Users\Dominique\stripe-test\env.ps1
& $Py -m src.migrate
& $Py scripts\stripe_test_helper.py where
```

Expected: `migrate_complete ... created=[... 'api_users' ... 'stripe_events' ...]`,
then one absolute path ending `stripe-test\bp-stripe-test.db`.
Failure: `REFUSED: ... must be sqlite:///C:/absolute/path.db` means env.ps1 was
not dot-sourced in this window.

## 2. Confirm test mode, then start the server (W1)

```powershell
Set-ExecutionPolicy -Scope Process Bypass
. C:\Users\Dominique\stripe-test\env.ps1

# Read the key without echo; refuse anything that is not a test key.
$sec  = Read-Host "Paste sk_test_ key" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$key  = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
if (-not $key.StartsWith("sk_test_")) { Remove-Variable key; throw "REFUSED: not an sk_test_ key" }
$env:STRIPE_SECRET_KEY = $key
Remove-Variable key, sec, bstr

# Livemode must be false. Prints a word, never the key.
$bal = stripe get /v1/balance --api-key $env:STRIPE_SECRET_KEY | ConvertFrom-Json
if ($bal.livemode -ne $false) { Remove-Item Env:STRIPE_SECRET_KEY; throw "STOP: livemode is not false" }
"livemode = $($bal.livemode)"

# The CLI used in W2 and W3 must be the same Stripe account as the key.
$a1 = (stripe get /v1/account --api-key $env:STRIPE_SECRET_KEY | ConvertFrom-Json).id
$a2 = (stripe get /v1/account | ConvertFrom-Json).id
if ($a1 -ne $a2) { throw "STOP: CLI is logged into a different account than the key" }
"account match: $a1"

# Webhook secret for `stripe listen` (captured, not shown).
$env:STRIPE_WEBHOOK_SECRET = (stripe listen --print-secret).Trim()

# Test-mode product and prices; ids go in a file W3 reads.
$prod = stripe products create --api-key $env:STRIPE_SECRET_KEY -d "name=BP TEST plans" | ConvertFrom-Json
$pMonth = stripe prices create --api-key $env:STRIPE_SECRET_KEY -d "product=$($prod.id)" -d unit_amount=4900 -d currency=usd -d "recurring[interval]=month" | ConvertFrom-Json
$pYear = stripe prices create --api-key $env:STRIPE_SECRET_KEY -d "product=$($prod.id)" -d unit_amount=49000 -d currency=usd -d "recurring[interval]=year" | ConvertFrom-Json
$pData = stripe prices create --api-key $env:STRIPE_SECRET_KEY -d "product=$($prod.id)" -d unit_amount=9900 -d currency=usd | ConvertFrom-Json
$env:STRIPE_PRICE_PRO = $pMonth.id; $env:STRIPE_PRICE_PRO_ANNUAL = $pYear.id; $env:STRIPE_PRICE_DATASET = $pData.id
@"
`$env:STRIPE_PRICE_PRO = "$($pMonth.id)"
`$env:STRIPE_PRICE_PRO_ANNUAL = "$($pYear.id)"
`$env:STRIPE_PRICE_DATASET = "$($pData.id)"
"@ | Set-Content -Encoding ASCII "$Dir\prices.ps1"

& $Py -m uvicorn src.api:app --host 127.0.0.1 --port 8099
```

Expected: `livemode = False`, `account match: acct_...`, then uvicorn startup
ending `Uvicorn running on http://127.0.0.1:8099`.
Failure: `STOP: livemode is not false` or `REFUSED` means do not continue;
close the window. A later `stripe_webhook_unconfigured missing=[...]` in W1
means an env var above did not reach this process (start uvicorn from this
same window, after the variables are set).

## 3. Forward events (W2)

```powershell
stripe listen --forward-to http://127.0.0.1:8099/api/billing/webhook --events charge.dispute.created,charge.refunded,customer.subscription.updated,customer.subscription.deleted,invoice.payment_succeeded
```

Expected: `Ready! ... Your webhook signing secret is whsec_...` (a test-only
secret). Each event then prints `--> type [evt_...]` and `<-- [200] POST ...`.
Failure: `[400]` is a signature mismatch (`stripe_webhook_bad_signature` in W1):
`STRIPE_WEBHOOK_SECRET` in W1 is not the secret this listener uses. `[500]` is
covered per test below.

## Test A: a chargeback revokes access (W3)

```powershell
. C:\Users\Dominique\stripe-test\env.ps1
$H = "scripts\stripe_test_helper.py"
$email = "test-a@example.com"
& $Py $H register $email
```
Expected: `registered test-a@example.com | key ...`, then a row with
`subscription_tier=free`.

```powershell
$cus = (stripe customers create -d "email=$email" -d payment_method=pm_card_createDispute -d "invoice_settings[default_payment_method]=pm_card_createDispute" | ConvertFrom-Json).id
& $Py $H seed $email --customer $cus --download
```
Expected row (the state a completed checkout leaves):
`subscription_tier=pro  has_paid_download=1  pro_expires_at=<31 days ahead>  stripe_customer_id=cus_...  stripe_subscription_id=None`, `tier in force: pro`.

```powershell
stripe payment_intents create -d amount=4900 -d currency=usd -d "customer=$cus" -d payment_method=pm_card_createDispute -d confirm=true -d "automatic_payment_methods[enabled]=true" -d "automatic_payment_methods[allow_redirects]=never"
```
Expected (unrehearsed timing): `status: succeeded`; the dispute follows within
about a minute. Then:

- W2: `--> charge.dispute.created [evt_...]` and `<-- [200] POST ...`
- W1: `[warning ] stripe_payment_REVERSED  customer=cus_... email=test-a@example.com reason=fraudulent`
  (the `reason` text is Stripe's **(unrehearsed)**; any non-empty value is fine)

```powershell
& $Py $H show $email
```
Expected row: `subscription_tier=free  has_paid_download=0  pro_expires_at=<yesterday, NOT None>  stripe_customer_id=cus_... (kept)  stripe_subscription_id=None`,
a `stripe_events` row of type `charge.dispute.created`, `tier in force: free`.
(`pro_expires_at` is backdated, never nulled, because NULL means "never expires".)

What failure looks like:

| You see | Meaning |
|---|---|
| W1 `stripe_reversal_unknown_customer customer=- ...`, row still `pro` | the charge had no customer (payment intent created without `customer=`) |
| same line with a real `customer=cus_...` | the account was not linked: `seed` was skipped or used another customer |
| W2 `[500]`, traceback from `stripe/...` in W1, no `stripe_events` row | the charge lookup failed (key wrong, expired or no network). The event id is released on purpose; fix the key, restart W1, then `stripe events resend evt_...` |
| W2 `[400]` | signature mismatch, see step 3 |
| nothing in W2 after two minutes | dispute not created yet, or the card did not succeed; `stripe events list --limit 5` |

## Test B: a superseded subscription keeps access (W3)

Two subscriptions on one customer; update, then cancel, the **first**.
Access must stay on the second.

```powershell
. C:\Users\Dominique\stripe-test\env.ps1
$H = "scripts\stripe_test_helper.py"
$email = "test-b@example.com"
& $Py $H register $email
$cus = (stripe customers create -d "email=$email" -d payment_method=pm_card_visa -d "invoice_settings[default_payment_method]=pm_card_visa" | ConvertFrom-Json).id
& $Py $H seed $email --customer $cus
$s1 = (stripe subscriptions create -d "customer=$cus" -d "items[0][price]=$env:STRIPE_PRICE_PRO" | ConvertFrom-Json).id
$s2 = (stripe subscriptions create -d "customer=$cus" -d "items[0][price]=$env:STRIPE_PRICE_PRO_ANNUAL" | ConvertFrom-Json).id
```
Wait until W2 has been quiet for about 10 seconds. Events about the new
subscriptions may appear (`stripe_subscription_updated`, or
`stripe_update_superseded` for `$s2`): that is normal and ignored here.

```powershell
& $Py $H point $email --sub $s2
& $Py $H show $email
```
`point` does what a second checkout's `_attach_ids` does. **Write down this
row; it is the baseline B0**: `subscription_tier=pro`,
`pro_expires_at=<date>`, `stripe_subscription_id=<$s2>`.

B1, update the first:
```powershell
stripe subscriptions update $s1 -d cancel_at_period_end=true
```
- W2: `--> customer.subscription.updated` then `<-- [200]`
- W1: `[info ] stripe_update_superseded  current=<s2> email=test-b@example.com updated=<s1>`
- `show`: identical to B0.

B2, cancel the first:
```powershell
stripe subscriptions cancel $s1
```
- W2: `--> customer.subscription.deleted` then `<-- [200]`
- W1: `[info ] stripe_cancel_superseded  current=<s2> email=test-b@example.com ended=<s1>`
- `show`: identical to B0, `tier in force: pro`, `stripe_subscription_id=<s2>`.

What failure looks like:

| You see | Meaning |
|---|---|
| B1: `stripe_subscription_updated ... changed=plan,tier,expiry`, or `stripe_subscription_id` flips to `<s1>` | the old subscription overwrote the live one |
| B2: `stripe_subscription_cancelled email=... tier=free`, row `free`, `stripe_subscription_id=None` | the old subscription's cancellation revoked an account that still pays |
| B1/B2 line missing, `[200]` shown | the pointer step was skipped, so `$s1` was still the stored id; run `show` and compare with B0 |

B3, control (optional): `stripe subscriptions cancel $s2`. Either
`stripe_subscription_cancelled ... tier=free` (row `free`, id cleared) or
`stripe_cancel_leaves_paid_time` (tier kept until expiry, id cleared) is
correct **(which one depends on Stripe's period fields, unrehearsed)**.
`stripe_cancel_superseded` here would mean the pointer was wrong.

## Test C: a refund still reverses access (W3)

```powershell
. C:\Users\Dominique\stripe-test\env.ps1
$H = "scripts\stripe_test_helper.py"
$email = "test-c@example.com"
& $Py $H register $email
$cus = (stripe customers create -d "email=$email" -d payment_method=pm_card_visa -d "invoice_settings[default_payment_method]=pm_card_visa" | ConvertFrom-Json).id
& $Py $H seed $email --customer $cus --download
$pi = (stripe payment_intents create -d amount=4900 -d currency=usd -d "customer=$cus" -d payment_method=pm_card_visa -d confirm=true -d "automatic_payment_methods[enabled]=true" -d "automatic_payment_methods[allow_redirects]=never" | ConvertFrom-Json).id
stripe refunds create -d "payment_intent=$pi"
& $Py $H show $email
```
Expected:

- W2: `--> charge.refunded [evt_...]` then `<-- [200]`
- W1: `[warning ] stripe_payment_REVERSED  customer=cus_... email=test-c@example.com reason=refund`
- row: `subscription_tier=free  has_paid_download=0  pro_expires_at=<yesterday>  stripe_subscription_id=None`, a `stripe_events` row of type `charge.refunded`, `tier in force: free`.

Failure: `stripe_reversal_unknown_customer customer=-` (payment intent made
without `customer=`), or no `charge.refunded` in W2 (refund not created; read
the CLI output of `stripe refunds create`).

## Pass table

| Test | Pass |
|---|---|
| A | `stripe_payment_REVERSED ... reason=<non-empty>`; row free, download 0 |
| B1 | `stripe_update_superseded`; row equals B0 |
| B2 | `stripe_cancel_superseded`; row equals B0, tier pro |
| C | `stripe_payment_REVERSED ... reason=refund`; row free, download 0 |

## Teardown

Ctrl+C in W1 and W2, then in W1 `Remove-Item Env:STRIPE_SECRET_KEY`. Test-mode
objects in Stripe are harmless. Delete `C:\Users\Dominique\stripe-test` to start
over; a stale database from an earlier run is the usual cause of odd rows.

## Rehearsed offline (what the lines above rest on)

Real server, scratch SQLite file, `STRIPE_WEBHOOK_SECRET=whsec_rehearsal`,
correctly signed fake events:

| Event sent | HTTP | Log line | Row after |
|---|---|---|---|
| `customer.subscription.updated` for the old sub | 200 `superseded` | `stripe_update_superseded current=sub_TWO ... updated=sub_ONE` | unchanged |
| `customer.subscription.deleted` for the old sub | 200 `superseded` | `stripe_cancel_superseded current=sub_TWO ... ended=sub_ONE` | unchanged, tier pro |
| `charge.dispute.created`, charge carrying the customer | 200 `reversed` | `stripe_payment_REVERSED ... reason=fraudulent` | free, download 0, expiry yesterday, sub id None |
| `charge.refunded` | 200 `reversed` | `stripe_payment_REVERSED ... reason=refund` | same |
| `charge.dispute.created`, charge without a customer, key unusable | 500 | traceback from `stripe/_stripe_service.py` | unchanged, **no** `stripe_events` row |

The last row is the "[500] and no `stripe_events` row" failure described in
test A: a lookup that cannot reach Stripe raises on purpose so Stripe retries.
