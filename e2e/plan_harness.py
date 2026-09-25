"""Local harness for checking the dashboard as each plan's user.

    python e2e/plan_harness.py PORT

Starts uvicorn on 127.0.0.1:PORT against a fresh SQLite file, seeds three
companies, creates one password account per plan, and prints the logins.
Leaves the server running (Ctrl+C / kill the port to stop). Nothing here
touches production, Stripe, or email.

Companies:
  AAA  four periods over ~4.5 years; the 0.7y period's liabilities were
       RESTATED (540 -> 560) by the newest filing; newest filing has an
       SEC filing event (provenance link).
  BBB  two periods, reconciles.
  CCC  one period that does NOT reconcile (assets 1000, L 500, E 300).

Accounts (password "correct horse battery" for all):
  free@plans.test, starter@..., pro@..., business@..., enterprise@...
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery"
ADMIN = "harness-admin-secret"
TIERS = ("free", "starter", "pro", "business", "enterprise")


def _req(base: str, method: str, path: str, body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def seed(db_url: str) -> None:
    os.environ["DATABASE_URL"] = db_url
    sys.path.insert(0, str(ROOT))
    from src.storage.db import init_db, session_scope
    from src.storage.models import FilingEvent, Fundamental, UniverseSnapshot

    init_db()
    today = dt.date.today()

    def q(years: float) -> dt.date:
        return today - dt.timedelta(days=int(years * 365.25))

    def f(t, m, v, p, filed):
        return Fundamental(ticker=t, metric=m, value=v, period_end=p,
                           fiscal_period="Q", filing_date=filed, source="sec")

    with session_scope() as s:
        for t, name in (("AAA", "Alpha Holdings"), ("BBB", "Beta Corp"), ("CCC", "Gamma Inc")):
            s.add(UniverseSnapshot(as_of_date=today, ticker=t, name=name))
        for years, a in ((0.2, 1000.0), (0.7, 900.0), (2.5, 700.0), (4.5, 500.0)):
            p = q(years); filed = p + dt.timedelta(days=40)
            s.add(f("AAA", "total_assets", a, p, filed))
            s.add(f("AAA", "total_liabilities", a * .6, p, filed))
            s.add(f("AAA", "total_equity", a * .4, p, filed))
            s.add(f("AAA", "cash", a * .1, p, filed))
        newest = q(0.2) + dt.timedelta(days=40)
        s.add(f("AAA", "total_liabilities", 560.0, q(0.7), newest))
        s.add(f("AAA", "total_equity", 340.0, q(0.7), newest))
        s.add(FilingEvent(ticker="AAA", cik="0000999001", form="10-Q", filing_date=newest,
                          accession="0000999001-26-000007", primary_doc="aaa-10q.htm"))
        for years, a in ((0.3, 2000.0), (1.3, 1800.0)):
            p = q(years); filed = p + dt.timedelta(days=35)
            s.add(f("BBB", "total_assets", a, p, filed))
            s.add(f("BBB", "total_liabilities", a * .5, p, filed))
            s.add(f("BBB", "total_equity", a * .5, p, filed))
        # AAA income and cash flow: one fiscal year ending ~0.45y ago, in $M.
        # The 10-K's change in cash is 20M off its parts, so a check fails.
        M = 1e6
        fye = q(0.45)
        fy_filed = fye + dt.timedelta(days=45)
        quarters = [(fye - dt.timedelta(days=int(91.3 * k)), fp) for k, fp in ((3, "Q1"), (2, "Q2"), (1, "Q3"))]
        for (pq, fp), rev, ytd in zip(quarters, (100.0, 110.0, 120.0), (30.0, 65.0, 95.0)):
            fq = pq + dt.timedelta(days=40)
            for m, v in (("revenue", rev), ("cogs", rev * .6), ("gross_profit", rev * .4), ("net_income", rev * .1)):
                s.add(Fundamental(ticker="AAA", metric=m, value=v * M, period_end=pq, fiscal_period=fp,
                                  filing_date=fq, source="sec"))
            m = "operating_cash_flow" if fp == "Q1" else "operating_cash_flow_ytd"
            s.add(Fundamental(ticker="AAA", metric=m, value=ytd * M, period_end=pq, fiscal_period=fp,
                              filing_date=fq, source="sec"))
        for m, v in (("revenue", 460.0), ("cogs", 276.0), ("gross_profit", 184.0), ("net_income", 46.0),
                     ("operating_cash_flow", 130.0), ("investing_cash_flow", -40.0),
                     ("financing_cash_flow", -50.0), ("cash_change", 60.0), ("capex", 35.0)):
            s.add(Fundamental(ticker="AAA", metric=m, value=v * M, period_end=fye, fiscal_period="FY",
                              filing_date=fy_filed, source="sec"))
        p = q(0.25)
        s.add(f("CCC", "total_assets", 1000.0, p, p + dt.timedelta(days=30)))
        s.add(f("CCC", "total_liabilities", 500.0, p, p + dt.timedelta(days=30)))
        s.add(f("CCC", "total_equity", 300.0, p, p + dt.timedelta(days=30)))


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8190
    base = f"http://127.0.0.1:{port}"
    data = Path(os.environ.get("TEMP", "/tmp")) / f"bp_plans_{port}"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "plans.db"
    if db.exists():
        db.unlink()
    db_url = "sqlite:///" + db.as_posix()
    seed(db_url)

    env = {**os.environ,
           "DATABASE_URL": db_url, "ENV": "dev", "ADMIN_SECRET": ADMIN,
           "ADMIN_EMAIL": "owner@plans.test", "SESSION_SECRET": "harness-session-secret-xxxxxxxx",
           "AGENTMAIL_API_KEY": "", "DEMO_API_KEY": "", "REGISTER_RATE_PER_HOUR": "1000",
           "LOGIN_RATE_PER_HOUR": "1000", "AUTO_UPDATE": "off",
           # Plans on sale, so the Billing tab shows every card. No Stripe key:
           # clicking buy reports "not configured", which is expected here.
           "STRIPE_PRICE_STARTER": "price_h_s", "STRIPE_PRICE_STARTER_ANNUAL": "price_h_sy",
           "STRIPE_PRICE_BUSINESS": "price_h_b", "STRIPE_PRICE_BUSINESS_ANNUAL": "price_h_by"}
    log = open(data / "server.log", "w")
    subprocess.Popen([sys.executable, "-m", "uvicorn", "src.api:app", "--host", "127.0.0.1",
                      "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
    for _ in range(90):
        try:
            urllib.request.urlopen(base + "/health", timeout=2)
            break
        except Exception:
            time.sleep(1)
    else:
        sys.exit("server did not start; see " + str(data / "server.log"))

    for t in TIERS:
        email = f"{t}@plans.test"
        _req(base, "POST", "/api/auth/register-password",
             {"email": email, "password": PASSWORD, "accept_terms": True})
        if t == "enterprise":
            _req(base, "POST", "/admin/enterprise", {"email": email, "monthly_calls": 250000},
                 {"X-Admin-Secret": ADMIN})
        elif t != "free":
            _req(base, "POST", "/admin/grant-access", {"email": email, "action": f"grant_{t}"},
                 {"X-Admin-Secret": ADMIN})
    print(json.dumps({"base": base, "password": PASSWORD,
                      "accounts": {t: f"{t}@plans.test" for t in TIERS},
                      "log": str(data / "server.log")}, indent=2))


if __name__ == "__main__":
    main()
