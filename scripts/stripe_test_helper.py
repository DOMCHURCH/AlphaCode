"""Stripe test-mode helper. Reads and seeds the SAME database the server uses.

The database path is never typed here. It is parsed from DATABASE_URL, the
variable uvicorn is started with, so the server and this helper cannot point at
two different files. A relative path, or a file that does not exist, is
refused: opening a relative sqlite path from another folder silently creates a
new empty database and every check then "passes" against nothing.

Usage (run from the repo root, with PYTHONPATH and DATABASE_URL set):
  where
  register EMAIL
  seed EMAIL --customer cus_X [--sub sub_Y] [--download]
  point EMAIL --sub sub_Y
  show [EMAIL]
"""
import argparse
import datetime as dt
import os
import re
import sqlite3
import sys


def db_path():
    url = os.environ.get("DATABASE_URL", "")
    m = re.match(r"^sqlite:///([A-Za-z]:[/\\].+)$", url)
    if not m:
        sys.exit("REFUSED: DATABASE_URL must be sqlite:///C:/absolute/path.db (got %r)" % url)
    path = os.path.normpath(m.group(1))
    if not os.path.isfile(path):
        sys.exit("REFUSED: %s does not exist. Run: python -m src.migrate first." % path)
    return path


def show(email=None):
    path = db_path()
    print("database:", path, "(%d bytes)" % os.path.getsize(path))
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    sql = ("SELECT email, subscription_tier, has_paid_download, pro_expires_at, "
           "stripe_customer_id, stripe_subscription_id FROM api_users")
    rows = con.execute(sql + (" WHERE email = ?" if email else ""), (email,) if email else ()).fetchall()
    print("api_users:")
    for r in rows:
        print("  " + "  ".join("%s=%s" % (k, r[k]) for k in r.keys()))
    if not rows:
        print("  (none)")
    print("stripe_events (newest 8):")
    for r in con.execute("SELECT event_id, type, received_at FROM stripe_events "
                         "ORDER BY received_at DESC LIMIT 8"):
        print("  %s  %s  %s" % (r["event_id"], r["type"], r["received_at"]))
    con.close()
    if email:
        from src import accounts
        acct = accounts.by_email(email)
        print("tier in force (expiry applied): %s" % (acct.tier if acct else "NO ACCOUNT"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["where", "register", "seed", "point", "show"])
    ap.add_argument("email", nargs="?")
    ap.add_argument("--customer", default="")
    ap.add_argument("--sub", default="")
    ap.add_argument("--download", action="store_true")
    a = ap.parse_args()
    path = db_path()
    if a.cmd == "where":
        print(path)
        return
    if a.cmd == "show":
        show(a.email)
        return
    if not a.email:
        sys.exit("email required")
    from src import accounts, billing
    if a.cmd == "register":
        acct, key = accounts.register(a.email)
        print("registered", a.email, "| key (test database only):", key)
    elif a.cmd == "seed":
        # What a completed checkout does: grant Pro, then store Stripe's ids.
        accounts.apply_admin_action(a.email, "grant_pro", days=31)
        if a.download:
            accounts.apply_admin_action(a.email, "grant_download")
        billing._attach_ids(a.email, customer=a.customer, subscription=a.sub)
    elif a.cmd == "point":
        # What a second checkout's _attach_ids does: move the stored id to the newest.
        billing._attach_ids(a.email, subscription=a.sub)
    show(a.email)


if __name__ == "__main__":
    main()
