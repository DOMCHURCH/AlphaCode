#!/usr/bin/env python3
"""Issue one API key by hand, to somebody who has no account here.

WHAT THIS IS
    Outreach. A partner, a reviewer, somebody with an audience who might write
    about this: they get a working key in a message and never meet a signup
    form, a card field or an email confirmation. The key is the same 43
    characters as any other, hashed the same way, and the holder cannot tell
    the difference. That is the point.

NO STRIPE, NO ACCOUNT
    Nothing here touches Stripe, and no `api_users` row is created. A row over
    there is a CUSTOMER -- counted in the roster, counted in the revenue
    estimate on /admin -- and one of these is not a customer. They live in
    `seeded_keys`, with no foreign key in either direction, so the two
    questions "how many customers are there" and "who did we send a key to"
    have one answer each.

THE KEY IS SHOWN ONCE
    The database stores a SHA-256 digest. There is no query, here or from a
    dump, that reads the key back. If it is lost, revoke that label and issue
    another -- there is nothing else to do, and that is true for paying
    customers too.

REVIEW THESE PERIODICALLY
    They are credentials that bypass payment, so they are worth looking at on
    purpose rather than when something goes wrong. `/api/admin/stats` lists
    every one with its label, when it was issued and when it was last used:

        curl -H "X-Admin-Secret: $ADMIN_SECRET" https://<host>/api/admin/stats

    A key with no `last_used_at` weeks after it went out is outreach that did
    not land, and should be revoked rather than left live -- an unused
    credential is all cost and no benefit. So should one whose recipient has
    gone quiet. Revoking is `scripts/revoke_seed_key.py --label <label>`.

    The admin panel issues and revokes these too, through the same functions
    this script calls -- `POST /api/admin/seed-keys/issue`, behind the admin
    secret and, when one is configured, a TOTP code. This script remains the
    way to do it without a browser, and the way to do it when the panel is
    unreachable.

USAGE
    python3 scripts/issue_seed_key.py --label "stefano-sec-edgar-mcp" \
        --display-name "Stefano Amorelli — sec-edgar-mcp"
    python3 scripts/issue_seed_key.py --label "acme-partner" \
        --display-name "Acme — integration pilot" \
        --rate-limit 50000 --notes "Q4 integration pilot, revisit in January"

    The label is the handle: unique, typed, and what `revoke_seed_key.py`
    takes. The display name is the same key said in words, and is the column
    /admin reads -- ten of these are unreadable as a list of handles.

Needs DATABASE_URL in the environment, same as the service.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Running `python3 scripts/issue_seed_key.py` puts scripts/ on sys.path, not
# the repo root, so `import src...` would fail. Fix that before importing src.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from src import seedkeys

    parser = argparse.ArgumentParser(
        description="Issue one seeded API key for partner outreach.",
    )
    parser.add_argument(
        "--label", required=True,
        help="who it is going to. Unique, and the handle for revoking it.",
    )
    parser.add_argument(
        "--rate-limit", type=int, default=seedkeys.DEFAULT_RATE_LIMIT,
        help=f"calls per calendar month (default: "
             f"{seedkeys.DEFAULT_RATE_LIMIT})",
    )
    parser.add_argument(
        "--display-name", default="",
        help='how it should read on /admin: "Real Name — Project". '
             "Defaults to the label.",
    )
    parser.add_argument(
        "--notes", default="",
        help="why this was issued, for whoever reads the list in six months",
    )
    args = parser.parse_args()

    from src.logging_config import configure_logging
    from src.storage.db import init_db

    configure_logging()
    # Same call the service makes at startup. On a database that has never
    # seen this table it creates it; on one that has, it does nothing.
    init_db()

    try:
        key = seedkeys.issue(
            args.label, rate_limit=args.rate_limit, notes=args.notes,
            display_name=args.display_name,
        )
    except seedkeys.LabelTaken:
        print(
            f"A seeded key labelled {args.label!r} already exists.\n"
            "Labels are unique because revoking is by label. Pick another, "
            "or revoke that one first:\n"
            f"    python3 scripts/revoke_seed_key.py --label {args.label!r}",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    print()
    print(f"  Issued to : {args.display_name or args.label}")
    print(f"  Label     : {args.label}   (the handle; revoking takes this)")
    print(f"  Limit     : {args.rate_limit:,} calls per calendar month")
    if args.notes:
        print(f"  Notes     : {args.notes}")
    print()
    print(f"  KEY: {key}")
    print()
    print("  This is the only time it is shown. The database holds a hash of")
    print("  it and nothing here can read it back. Send it now.")
    print()
    print("  Used as:  curl -H 'X-API-Key: <key>' https://toscale.pro"
          "/api/company/JPM")
    print(f"  Revoke:   python3 scripts/revoke_seed_key.py --label "
          f"{args.label!r}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
