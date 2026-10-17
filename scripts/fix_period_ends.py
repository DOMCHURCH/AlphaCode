"""DELETE_DUP: remove the month-end-rounded copy of a quarter that is also stored under its real date.

SEC's bulk datasets round every `ddate` to the nearest month end (Apple's quarter
that ended 2026-03-28 arrives as 2026-03-31); the XBRL frames carry the real date.
A filing loaded by both routes is stored twice in `fundamentals` -- `uq_fundamental_point`
includes `period_end` -- so the quarter exists under two dates. The read side
already treats the real date as canonical (src/company/periods.py). This script
removes the rounded rows so the table agrees with the read side.

THE RULE. A PAIR is (ticker, filing_date, rounded_pe, real_pe): rounded_pe is a month
end and real_pe a non-month-end date 1 to 15 days away, in the SAME filing. The
rounded row is ALWAYS the one deleted and the real one ALWAYS the one kept. Nothing
else is ever deleted: a lone month-end date with no twin in its filing is a different
problem (it needs EDGAR to say whether it is rounded; see lookup_report_dates.py).

THE ALL-METRICS CHECK. The duplicate was first counted on total_assets alone, but a
pair owns every metric of the filing. A pair is deleted only if EVERY (source, metric)
on the rounded side exists on the real side with an equal value. Otherwise the whole
pair is EXCLUDED -- no row of it is touched -- and reported:

    value_differs     a (source, metric) has different values on the two sides
    only_on_rounded   a (source, metric) exists on the rounded side only: deleting
                      it would lose a figure the real side does not have
    ambiguous_twin    two real dates are equally near the month end, or two month
                      ends claim the same real date: which one is "the" twin is a
                      guess, so none is

Metrics that exist only on the real side are fine (nothing is lost).

SAFETY MODEL.
  * The default is a DRY RUN. It opens the database read-only (the server refuses
    writes: default_transaction_read_only on Postgres, query_only on SQLite), runs
    SELECTs only, and changes nothing. It is idempotent.
  * --apply is refused unless ALL of these hold, checked before any write connection
    is opened: --railway-backup-confirmed names the backup (id/date/time) the
    operator has verified exists in Railway; --backup-path names a CSV that does not
    exist yet; the session is interactive and the operator types the exact phrase
    DELETE ROUNDED ROWS after seeing the dry-run summary.
  * Then, in ONE transaction: the table is locked against concurrent writers
    (Postgres), the plan is rebuilt INSIDE the transaction and must equal the plan
    the operator saw, every row to delete is backed up to the CSV (fsynced and read
    back) BEFORE the first DELETE, the deletes run by primary key, and the number of
    rows deleted, the number remaining, and the number of surviving twins are all
    checked BEFORE commit. Any mismatch or error rolls everything back.
  * The URL comes from --url-file or PERIOD_FIX_DATABASE_URL (never argv, never the
    app's DATABASE_URL) and is scrubbed from every message.

Usage:
    python scripts/fix_period_ends.py --url-file URLFILE [--show AAPL,NVDA] [--tickers A,B]
                                      [--exclusions-csv excluded.csv]            # dry run
    python scripts/fix_period_ends.py --url-file URLFILE --apply \\
        --railway-backup-confirmed "backup 2026-10-16 21:00" --backup-path backup.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Connection, delete, func, select, text

# `python scripts/fix_period_ends.py` puts scripts/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.company.periods import filing_twins, is_month_end  # noqa: E402
from src.storage.models import Fundamental  # noqa: E402
from src.storage.script_db import make_engine, read_url, scrub  # noqa: E402

T = Fundamental.__table__
COLUMNS = ["id", "ticker", "metric", "value", "period_end", "fiscal_period",
           "filing_date", "source", "restated", "source_tag", "ingested_at"]
CONFIRM_PHRASE = "DELETE ROUNDED ROWS"
REASONS = ("ambiguous_twin", "value_differs", "only_on_rounded")
CHUNK = 400  # ids per statement: under SQLite's 999-parameter limit


class PlanError(RuntimeError):
    """A safety invariant failed. Nothing was deleted."""


@dataclass(frozen=True)
class Pair:
    ticker: str
    filing_date: dt.date
    rounded_pe: dt.date
    real_pe: dt.date


@dataclass(frozen=True)
class Excluded:
    pair: Pair
    reason: str
    metrics: tuple[str, ...]
    rows: int                 # rounded-side rows this exclusion protects
    details: str = ""
    matched: int = 0          # of those rows, how many have an equal-valued real-dated twin (true duplicates)
    orphans: int = 0          # rows with no real-dated counterpart at all
    differing: int = 0        # rows whose twin holds a different value


@dataclass
class Plan:
    filings: int = 0                                     # (ticker, filing_date) groups scanned
    tickers: int = 0
    ok: list[Pair] = field(default_factory=list)
    excluded: list[Excluded] = field(default_factory=list)
    delete_rows: list[dict] = field(default_factory=list)   # full rows, rounded side
    twin_id: dict[int, int] = field(default_factory=dict)   # deleted id -> surviving twin id
    lone_month_ends: int = 0                             # month ends with no twin (left alone)
    triples: list[tuple[str, dt.date, dt.date]] = field(default_factory=list)

    @property
    def delete_ids(self) -> set[int]:
        return {r["id"] for r in self.delete_rows}


def values_equal(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(1.0, abs(a) * 1e-9)


# ---- planning (pure reads) ------------------------------------------------------------------------------------------

def load_triples(conn: Connection, tickers: set[str] | None = None) -> list[tuple[str, dt.date, dt.date]]:
    q = select(T.c.ticker, T.c.filing_date, T.c.period_end).distinct()
    if tickers:
        q = q.where(T.c.ticker.in_(sorted(tickers)))
    return [(r[0], r[1], r[2]) for r in conn.execute(q)]


def find_pairs(triples) -> tuple[list[Pair], list[Excluded], int]:
    """(pairs, ambiguous exclusions, lone month ends) from distinct (ticker, filing, period) triples."""
    groups: dict[tuple[str, dt.date], set[dt.date]] = defaultdict(set)
    for ticker, filing_date, period_end in triples:
        groups[(ticker, filing_date)].add(period_end)
    pairs: list[Pair] = []
    ambiguous: list[Excluded] = []
    lone = 0
    for (ticker, filing_date), dates in sorted(groups.items()):
        twins = filing_twins(dates)
        claimed: dict[dt.date, list[dt.date]] = defaultdict(list)
        for m, reals in twins.items():
            if len(reals) == 1:
                claimed[reals[0]].append(m)
        for m in sorted(d for d in dates if is_month_end(d)):
            reals = twins.get(m)
            if not reals:
                lone += 1
                continue
            if len(reals) > 1:
                ambiguous.append(Excluded(
                    Pair(ticker, filing_date, m, reals[0]), "ambiguous_twin", (), 0,
                    "equally near real dates: " + ", ".join(d.isoformat() for d in reals)))
            elif len(claimed[reals[0]]) > 1:
                ambiguous.append(Excluded(
                    Pair(ticker, filing_date, m, reals[0]), "ambiguous_twin", (), 0,
                    "real date also claimed by " + ", ".join(
                        d.isoformat() for d in claimed[reals[0]] if d != m)))
            else:
                pairs.append(Pair(ticker, filing_date, m, reals[0]))
    return pairs, ambiguous, lone


def _rows_for(conn: Connection, pairs: list[Pair]) -> dict[tuple[str, dt.date, dt.date], list[dict]]:
    """Every row at the pairs' two dates, keyed (ticker, filing_date, period_end)."""
    wanted: dict[str, set[dt.date]] = defaultdict(set)
    for p in pairs:
        wanted[p.ticker].add(p.filing_date)
    out: dict[tuple[str, dt.date, dt.date], list[dict]] = defaultdict(list)
    for ticker, filings in wanted.items():
        fl = sorted(filings)
        for i in range(0, len(fl), CHUNK):
            q = select(*[T.c[c] for c in COLUMNS]).where(
                T.c.ticker == ticker, T.c.filing_date.in_(fl[i:i + CHUNK]))
            for r in conn.execute(q):
                row = dict(zip(COLUMNS, r))
                out[(row["ticker"], row["filing_date"], row["period_end"])].append(row)
    return out


def classify(pairs: list[Pair], rows) -> tuple[list[Pair], list[Excluded], list[dict], dict[int, int]]:
    ok: list[Pair] = []
    excluded: list[Excluded] = []
    delete_rows: list[dict] = []
    twin_id: dict[int, int] = {}
    for p in pairs:
        rounded = {(r["source"], r["metric"]): r for r in rows.get((p.ticker, p.filing_date, p.rounded_pe), [])}
        real = {(r["source"], r["metric"]): r for r in rows.get((p.ticker, p.filing_date, p.real_pe), [])}
        n_orphan = sum(1 for k in rounded if k not in real)
        n_diff = sum(1 for k, r in rounded.items() if k in real and not values_equal(r["value"], real[k]["value"]))
        n_match = len(rounded) - n_orphan - n_diff
        differs = sorted({k[1] for k, r in rounded.items()
                          if k in real and not values_equal(r["value"], real[k]["value"])})
        only = sorted({k[1] for k in rounded if k not in real})
        if differs:
            detail = f"{len(differs)} metric(s) differ" + (f"; {len(only)} only on rounded" if only else "")
            excluded.append(Excluded(p, "value_differs", tuple(differs), len(rounded), detail,
                                     matched=n_match, orphans=n_orphan, differing=n_diff))
        elif only:
            excluded.append(Excluded(p, "only_on_rounded", tuple(only), len(rounded),
                                     f"{len(only)} metric(s) have no real-dated counterpart",
                                     matched=n_match, orphans=n_orphan))
        elif rounded:
            ok.append(p)
            for key, r in rounded.items():
                delete_rows.append(r)
                twin_id[r["id"]] = real[key]["id"]
    return ok, excluded, delete_rows, twin_id


def verify_plan(plan: Plan) -> None:
    """Hard invariants. The rounded row is the one deleted; the real one survives."""
    ids = plan.delete_ids
    if len(ids) != len(plan.delete_rows):
        raise PlanError("duplicate row in the delete set")
    for r in plan.delete_rows:
        if not is_month_end(r["period_end"]):
            raise PlanError(f"row {r['id']} is not a month-end row ({r['period_end']})")
        t = plan.twin_id.get(r["id"])
        if t is None or t in ids:
            raise PlanError(f"row {r['id']} has no surviving real-dated twin")


def build_plan(conn: Connection, tickers: set[str] | None = None) -> Plan:
    triples = load_triples(conn, tickers)
    pairs, ambiguous, lone = find_pairs(triples)
    ok, excluded, delete_rows, twin_id = classify(pairs, _rows_for(conn, pairs))
    plan = Plan(
        filings=len({(t, f) for t, f, _ in triples}), tickers=len({t for t, _, _ in triples}),
        ok=ok, excluded=ambiguous + excluded, delete_rows=delete_rows, twin_id=twin_id,
        lone_month_ends=lone, triples=triples)
    verify_plan(plan)
    return plan


# ---- reporting ------------------------------------------------------------------------------------------------------

REASON_TEXT = {
    "ambiguous_twin": "two real dates equally near, or two month ends claim one real date",
    "value_differs": "a metric has a different value on the rounded and the real side",
    "only_on_rounded": "a metric exists only on the rounded side (deleting it would lose it)",
}


def render_report(plan: Plan, show: list[str] | None = None) -> str:
    rows = plan.delete_rows
    out = ["DELETE_DUP dry run -- nothing is changed",
           f"scanned: {plan.filings:,} filings, {plan.tickers:,} tickers",
           f"pairs found (month-end row + real-dated twin, same filing): {len(plan.ok) + len(plan.excluded):,}",
           f"  deletable:  {len(plan.ok):,} pairs",
           f"  EXCLUDED:   {len(plan.excluded):,} pairs"]
    for reason in REASONS:
        ex = [e for e in plan.excluded if e.reason == reason]
        if ex:
            out.append(f"    {reason:16} {len(ex):>6,} pairs, {sum(e.rows for e in ex):>7,} rows kept  -- {REASON_TEXT[reason]}")
            if reason != "ambiguous_twin":
                out.append(f"      of those rows: {sum(e.matched for e in ex):,} are true duplicates (equal twin), "
                           f"{sum(e.orphans for e in ex):,} exist ONLY on the rounded side, "
                           f"{sum(e.differing for e in ex):,} have a twin with a different value")
    out += ["",
            f"DELETE_DUP set: {len(rows):,} rows, {len({r['ticker'] for r in rows}):,} tickers, "
            f"{len({(r['ticker'], r['filing_date']) for r in rows}):,} filings",
            f"rows kept (their real-dated twins): {len(set(plan.twin_id.values())):,}",
            f"month-end dates with no twin in their filing (left alone, not this tool's job): {plan.lone_month_ends:,}"]
    tally: dict[str, int] = defaultdict(int)
    for e in plan.excluded:
        if e.reason == "only_on_rounded":
            for m in e.metrics:
                tally[m] += 1
    if tally:
        top = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:12]
        out += ["", "metrics that exist only on the rounded side (pairs affected): "
                + ", ".join(f"{m} {n}" for m, n in top)]
    if plan.excluded:
        out += ["", "excluded pairs (first 15; --exclusions-csv writes all):"]
        for e in plan.excluded[:15]:
            p = e.pair
            out.append(f"  {p.ticker:6} filed {p.filing_date}  {p.rounded_pe} -> {p.real_pe}  {e.reason}"
                       f"  {', '.join(e.metrics[:6])}{' ...' if len(e.metrics) > 6 else ''}  {e.details}")
    for ticker in show or []:
        out += ["", f"{ticker}: latest two filings"]
        filings = sorted({f for t, f, _ in plan.triples if t == ticker}, reverse=True)[:2]
        if not filings:
            out.append("  (no rows)")
        for f in filings:
            dates = sorted({pe for t, ff, pe in plan.triples if t == ticker and ff == f}, reverse=True)
            for d in dates:
                out.append(f"  filed {f}  period_end {d}  {_status(plan, ticker, f, d)}")
    return "\n".join(out)


def _status(plan: Plan, ticker: str, filing: dt.date, d: dt.date) -> str:
    if not is_month_end(d):
        kept = any(p.ticker == ticker and p.filing_date == filing and p.real_pe == d for p in plan.ok)
        held = any(e.pair.ticker == ticker and e.pair.filing_date == filing and e.pair.real_pe == d
                   for e in plan.excluded)
        return "real date -- kept" + ("" if kept or held else " (no month-end copy)")
    for p in plan.ok:
        if p.ticker == ticker and p.filing_date == filing and p.rounded_pe == d:
            n = sum(1 for r in plan.delete_rows if r["ticker"] == ticker and r["filing_date"] == filing
                    and r["period_end"] == d)
            return f"month-end copy of {p.real_pe} -- DELETE ({n} rows)"
    for e in plan.excluded:
        if e.pair.ticker == ticker and e.pair.filing_date == filing and e.pair.rounded_pe == d:
            return f"month-end copy of {e.pair.real_pe} -- EXCLUDED ({e.reason})"
    return "month end, no twin in this filing -- left alone"


def write_exclusions_csv(plan: Plan, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "filing_date", "rounded_pe", "real_pe", "reason", "metrics", "rows_kept",
                    "duplicate_rows", "orphan_rows", "differing_rows", "details"])
        for e in plan.excluded:
            p = e.pair
            w.writerow([p.ticker, p.filing_date, p.rounded_pe, p.real_pe, e.reason,
                        ";".join(e.metrics), e.rows, e.matched, e.orphans, e.differing, e.details])


# ---- --apply --------------------------------------------------------------------------------------------------------

def write_backup(path: Path, plan: Plan) -> None:
    """Every row about to be deleted, plus the id of the real-dated twin that survives it.
    Written, fsynced and read back BEFORE the first DELETE."""
    if path.exists():
        raise FileExistsError(f"backup file already exists: {path}")
    rows = sorted(plan.delete_rows, key=lambda r: r["id"])
    with open(path, "x", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([*COLUMNS, "surviving_twin_id"])
        for r in rows:
            w.writerow([("" if r[c] is None else r[c]) for c in COLUMNS] + [plan.twin_id[r["id"]]])
        f.flush()
        os.fsync(f.fileno())
    with open(path, newline="", encoding="utf-8") as f:
        back = list(csv.DictReader(f))
    if [int(b["id"]) for b in back] != [r["id"] for r in rows]:
        raise PlanError("backup read-back does not match the rows to delete")


def apply_delete(engine, shown: Plan, backup_path: Path, tickers: set[str] | None = None) -> int:
    """Delete the plan's rounded rows in ONE transaction. Returns the number deleted."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("LOCK TABLE fundamentals IN SHARE ROW EXCLUSIVE MODE"))
        plan = build_plan(conn, tickers)          # rebuilt inside the transaction, under the lock
        if plan.delete_ids != shown.delete_ids:
            raise PlanError("the data changed since the dry run: the delete set is different; nothing deleted")
        write_backup(backup_path, plan)
        ids = sorted(plan.delete_ids)
        deleted = 0
        for i in range(0, len(ids), CHUNK):
            deleted += conn.execute(delete(T).where(T.c.id.in_(ids[i:i + CHUNK]))).rowcount
        left = sum(conn.execute(select(func.count()).select_from(T).where(T.c.id.in_(ids[i:i + CHUNK]))).scalar_one()
                   for i in range(0, len(ids), CHUNK))
        twins = sorted(set(plan.twin_id.values()))
        kept = sum(conn.execute(select(func.count()).select_from(T).where(T.c.id.in_(twins[i:i + CHUNK]))).scalar_one()
                   for i in range(0, len(twins), CHUNK))
        if deleted != len(ids) or left != 0 or kept != len(twins):
            raise PlanError(f"count check failed (deleted {deleted}/{len(ids)}, still present {left}, "
                            f"twins kept {kept}/{len(twins)}); rolled back")
        return deleted


# ---- CLI ------------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None, *, input_fn=None, interactive: bool | None = None, out=None) -> int:
    out = out or sys.stdout
    ap = argparse.ArgumentParser(description="Delete month-end-rounded duplicate rows (dry run by default).")
    ap.add_argument("--url-file", help="file whose single line is the database URL")
    ap.add_argument("--tickers", help="comma-separated; restrict the scan")
    ap.add_argument("--show", help="comma-separated tickers: show their latest two filings")
    ap.add_argument("--exclusions-csv", help="write every excluded pair here")
    ap.add_argument("--apply", action="store_true", help="WRITES. Needs the confirmations described in the docstring.")
    ap.add_argument("--railway-backup-confirmed", help="id/date of the Railway backup you verified exists")
    ap.add_argument("--backup-path", help="CSV to write the deleted rows to (must not exist)")
    args = ap.parse_args(argv)
    url = None
    try:
        tickers = {t.strip().upper() for t in args.tickers.split(",") if t.strip()} if args.tickers else None
        show = [t.strip().upper() for t in args.show.split(",") if t.strip()] if args.show else None

        if args.apply:  # every precondition BEFORE any database connection
            if not (args.railway_backup_confirmed or "").strip():
                print("refused: --apply needs --railway-backup-confirmed \"<backup id/date>\" "
                      "(confirm a Railway backup exists first)", file=sys.stderr)
                return 2
            if not args.backup_path:
                print("refused: --apply needs --backup-path", file=sys.stderr)
                return 2
            backup = Path(args.backup_path)
            if backup.exists():
                print(f"refused: {backup} already exists", file=sys.stderr)
                return 2
            if input_fn is None:
                if not (sys.stdin.isatty() if interactive is None else interactive):
                    print("refused: --apply needs an interactive terminal", file=sys.stderr)
                    return 2
                input_fn = input

        url = read_url(args.url_file, ("PERIOD_FIX_DATABASE_URL",))
        ro = make_engine(url, readonly=True)
        with ro.connect() as conn:
            plan = build_plan(conn, tickers)
            conn.rollback()
        print(render_report(plan, show), file=out)
        if args.exclusions_csv:
            write_exclusions_csv(plan, Path(args.exclusions_csv))
            print(f"\nexcluded pairs written to {args.exclusions_csv}", file=out)
        if not args.apply:
            print("\ndry run only: nothing was changed.", file=out)
            return 0

        if not plan.delete_rows:
            print("\nnothing to delete.", file=out)
            return 0
        print(f"\nAbout to DELETE {len(plan.delete_rows):,} rows (backed up to {backup} first, one transaction).", file=out)
        if input_fn(f"Type {CONFIRM_PHRASE} to continue: ").strip() != CONFIRM_PHRASE:
            print("aborted: confirmation phrase did not match. Nothing deleted.", file=sys.stderr)
            return 1
        n = apply_delete(make_engine(url, readonly=False), plan, backup, tickers)
        print(f"deleted {n:,} rows; backup at {backup}", file=out)
        return 0
    except Exception as exc:  # noqa: BLE001 -- scrubbed, then reported
        print(f"error: {type(exc).__name__}: {scrub(exc, url)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
