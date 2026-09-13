"""Keys issued by hand, to people who have no account here.

Partner and influencer outreach: somebody is sent a working key in a message
and never meets a signup form, a card field or an email confirmation. Nothing
in this module touches Stripe, `api_users`, or the flow a real customer walks
through -- `accounts.register` is not called and is not changed.

What IS shared is the key itself. `issue` calls the same `generate_api_key`,
`hash_api_key` and `key_prefix` that registration calls, so a seeded key is
the same 43 characters, hashed the same way, unrecoverable in the same way.
A key that looked different would tell its holder they were being treated
differently, and would need its own verification path -- which is the sort of
second code path that ends up being the one with the bug in it.

Issuing happens two ways and they are the same function. The two `scripts/`
wrappers are one; `POST /api/admin/seed-keys/issue` is the other, added later
and deliberately. That endpoint was refused at first, and the argument for
refusing it was sound while it held: an operator at a terminal with the
database URL is a high bar for a credential that skips payment, and an HTTP
route is a lower one. What changed is the other side of the comparison --
`require_admin` now carries a second factor, so reaching the route costs a
leaked secret AND a device, which is a harder bar than shell access to the
box rather than a softer one. Nothing else may call `issue` or `revoke`.

Metering lives on the row rather than in `usage_logs`, because that table's
`user_id` is a foreign key into `api_users` and these keys are decoupled from
it on purpose. See `models.SeededKey`.
"""

from __future__ import annotations

import datetime as dt

import structlog
from sqlalchemy import func, select

from src.storage.db import session_scope
from src.storage.models import SeededKey, current_month

log = structlog.get_logger(__name__)

# One value today. A second outreach programme gets its own rather than being
# told apart by reading labels.
#
# `find` matches on it, so a row carrying any other source does not
# authenticate AT ALL -- it is not a key with a default allowance, it is not a
# key. That is deliberate and it is what lets the rate-limit branch downstream
# read `source` and mean it: the set of rows that authenticate and the set
# whose source is this one are the same set, by construction. Adding a second
# programme is then an edit in both places, made on purpose, rather than a new
# kind of key silently inheriting this one's metering.
SOURCE = "influencer_seed"

# What an outreach key gets if nobody says otherwise. The scripts read this
# rather than spelling the number again -- two spellings of one number is
# exactly the drift this codebase has spent the last week removing.
DEFAULT_RATE_LIMIT = 10_000


class LabelTaken(Exception):
    """That label already names a key. Revoking is by label, so it must be
    one key -- and re-using a label would quietly orphan the older one."""


def issue(label: str, *, rate_limit: int = DEFAULT_RATE_LIMIT,
          notes: str = "", display_name: str = "") -> str:
    """Create one seeded key. Returns the PLAINTEXT, which exists only here.

    The database gets the digest. There is no query, here or from a dump, that
    produces the key afterwards -- the same property real keys have, and the
    reason the script prints it once and says so.
    """
    from src.accounts import generate_api_key, hash_api_key, key_prefix

    name = (label or "").strip()
    if not name:
        raise ValueError("a seeded key needs a label: who is it going to?")
    if len(name) > 120:
        # Truncating would store a label the operator cannot then revoke by,
        # because `revoke` looks up what they typed. Refusing is the only
        # honest option: this is the handle for turning the key off.
        raise ValueError(
            f"label is {len(name)} characters; the column holds 120, and a "
            "truncated label is one you cannot revoke by"
        )
    if rate_limit <= 0:
        raise ValueError("rate_limit must be a positive number of calls")

    plaintext = generate_api_key()
    with session_scope() as session:
        clash = session.execute(
            select(SeededKey).where(SeededKey.label == name)
        ).scalar_one_or_none()
        if clash is not None:
            raise LabelTaken(name)
        session.add(SeededKey(
            api_key=hash_api_key(plaintext),
            api_key_prefix=key_prefix(plaintext),
            label=name,
            # Defaulted to the label rather than left NULL, so the listing has
            # something to show for a key issued without one and the fallback
            # below is only ever exercised by rows older than the column.
            display_name=(display_name or "").strip()[:200] or name,
            rate_limit_override=int(rate_limit),
            source=SOURCE,
            notes=(notes or "").strip()[:500] or None,
        ))
    log.info("seed_key_issued", label=name, rate_limit=int(rate_limit),
             prefix=key_prefix(plaintext))
    return plaintext


def revoke(label: str) -> bool:
    """Stop one key. True if it was live, False if unknown or already revoked.

    Stamped rather than deleted: "we gave this person a key and took it back"
    is the fact worth being able to see in six months, and a deleted row says
    nothing at all.
    """
    name = (label or "").strip()
    with session_scope() as session:
        row = session.execute(
            select(SeededKey).where(SeededKey.label == name)
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = dt.datetime.now(dt.UTC)
    log.info("seed_key_revoked", label=name)
    return True


def find(api_key_hash: str) -> dict | None:
    """The live seeded key with this digest, as a plain dict, or None.

    Revoked keys are not found, which is the whole of requirement 7: the
    caller gets the same None an unknown key gets, and the same 401 with it.
    """
    with session_scope() as session:
        row = session.execute(
            select(SeededKey)
            .where(SeededKey.api_key == api_key_hash)
            .where(SeededKey.source == SOURCE)
            .where(SeededKey.revoked_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "label": row.label,
            "prefix": str(row.api_key_prefix or ""),
            "rate_limit": int(row.rate_limit_override),
            "source": row.source,
        }


def row(label: str) -> dict | None:
    """One key as the panel shows it, by label. None if there is no such key.

    Revoked rows ARE returned -- unlike `find`, which is the authentication
    path and must not see them. This is the reporting path, and "it is
    revoked" is the answer it exists to give.
    """
    with session_scope() as session:
        found = session.execute(
            select(SeededKey).where(SeededKey.label == (label or "").strip())
        ).scalar_one_or_none()
        return _as_dict(found) if found is not None else None


def usage(seed_id: str) -> tuple[int, dt.datetime | None]:
    """(calls counted this month, last used) for one seeded key.

    A month that has rolled over reads as zero without anything having reset
    it: `usage_month` says which month the counter counts, so a stale month is
    a stale count and is ignored rather than corrected on read.
    """
    with session_scope() as session:
        row = session.get(SeededKey, seed_id)
        if row is None:
            return 0, None
        used = int(row.calls_this_month or 0)
        return (used if row.usage_month == current_month() else 0), row.last_used_at


def record_call(seed_id: str) -> None:
    """Bump the meter and stamp `last_used_at`. Never raises.

    One UPDATE for both, because they are the same fact arriving at the same
    moment. The month rolls over here rather than on a schedule: the first
    call of a new month finds a stale `usage_month` and starts again at one.

    Two callers racing can lose a count, exactly as two callers racing the
    check in `accounts.enforce_monthly_limit` can overshoot it. Accepted for
    the same reason: this is a ceiling that exists to stop bulk scraping, not
    a meter anybody is billed against.
    """
    try:
        month = current_month()
        with session_scope() as session:
            row = session.get(SeededKey, seed_id)
            if row is None:
                return
            if row.usage_month != month:
                row.usage_month = month
                row.calls_this_month = 0
            row.calls_this_month = int(row.calls_this_month or 0) + 1
            row.last_used_at = dt.datetime.now(dt.UTC)
    except Exception as exc:  # noqa: BLE001 - a lost meter row beats a 500
        log.warning("seed_usage_record_failed", seed=seed_id, error=str(exc)[:200])


def listing(limit: int = 50) -> dict:
    """Every seeded key, for /api/admin/stats. No key material, ever.

    Newest first, revoked ones included -- the list is the record of the
    programme, and a revoked key that was never used is the one worth seeing,
    because it says the outreach did not land.
    """
    with session_scope() as session:
        rows = session.execute(
            select(SeededKey).order_by(SeededKey.issued_at.desc()).limit(limit)
        ).scalars().all()
        # Live keys first, then revoked, newest first within each. Sorted here
        # rather than in the panel: a second reader of this payload should not
        # have to rediscover that a revoked key belongs at the bottom.
        rows = sorted(rows, key=lambda r: (r.revoked_at is not None,), reverse=False)
        active = int(session.execute(
            select(func.count()).select_from(SeededKey)
            .where(SeededKey.revoked_at.is_(None))
        ).scalar_one())
        revoked = int(session.execute(
            select(func.count()).select_from(SeededKey)
            .where(SeededKey.revoked_at.is_not(None))
        ).scalar_one())
        return {
            "active": active,
            "revoked": revoked,
            "keys": [_as_dict(r) for r in rows],
        }


def _as_dict(r: SeededKey) -> dict:
    """One key, as every reporting caller sees it. One shape, one place."""
    return {
        "label": r.label,
        # Never blank: the label is the fallback, because a list of empty
        # cells is worse than a list of handles.
        "display_name": r.display_name or r.label,
        "source": r.source,
        "rate_limit": int(r.rate_limit_override),
        # Recomputed against the current month rather than read straight off
        # the row. The counter is only meaningful for the month it counts, and
        # a stale one shown as "calls this month" is a wrong number with a
        # confident label on it.
        "calls_this_month": (
            int(r.calls_this_month or 0)
            if r.usage_month == current_month() else 0
        ),
        "usage_month": current_month(),
        "issued_at": _iso(r.issued_at),
        "last_used_at": _iso(r.last_used_at),
        "revoked_at": _iso(r.revoked_at),
        "revoked": r.revoked_at is not None,
        "notes": r.notes or "",
    }


def _iso(when: dt.datetime | None) -> str | None:
    if when is None:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=dt.UTC)).isoformat()
