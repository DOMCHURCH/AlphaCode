#!/usr/bin/env python3
"""Stop one hand-issued API key.

    python3 scripts/revoke_seed_key.py --label "jane-doe-youtube"

The row is stamped, never deleted. "We gave this person a key and took it
back" is the fact worth being able to see in six months; a deleted row says
nothing at all, and an operator looking at a short list of labels cannot tell
a key that was revoked from one that was never issued.

Takes effect on the next request. Authentication looks for a seeded key with
no `revoked_at`, so a revoked one is simply not found -- the holder gets the
same 401 an invented key gets, from the same code path, with no second
rejection branch to keep in step with the first.

See `scripts/issue_seed_key.py` for what these are and why they exist.
Needs DATABASE_URL in the environment, same as the service.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Revoke one seeded API key by its label.",
    )
    parser.add_argument("--label", required=True, help="the label it was issued under")
    args = parser.parse_args()

    from src import seedkeys
    from src.logging_config import configure_logging
    from src.storage.db import init_db

    configure_logging()
    init_db()

    if seedkeys.revoke(args.label):
        print(f"Revoked {args.label!r}. It stops working on the next request.")
        return 0

    # One message for both misses, and it names both, because the operator
    # cannot tell them apart from here and the next step is the same either
    # way: look at the list.
    print(
        f"Nothing to revoke for {args.label!r} — no seeded key has that label, "
        "or it was already revoked.\nThe full list, labels included:\n"
        "    curl -H \"X-Admin-Secret: $ADMIN_SECRET\" https://<host>/api/admin/stats",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
