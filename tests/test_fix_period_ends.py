"""scripts/fix_period_ends.py -- DELETE_DUP: drop the month-end copy, keep the real date.

Every test runs on a throwaway SQLite file. Nothing here can reach a real database:
the script only learns a URL from a file/variable the test writes.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

D = dt.date
ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("fix_period_ends", ROOT / "scripts" / "fix_period_ends.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fix_period_ends"] = mod
    spec.loader.exec_module(mod)
    return mod


fix = _load()
METRICS = {"total_assets": 1000.0, "total_liabilities": 600.0, "total_equity": 400.0}


@pytest.fixture()
def env(tmp_path):
    from src.storage.models import Base

    path = tmp_path / "fund.db"
    url = f"sqlite:///{path}"
    engine = sa.create_engine(url, future=True)
    Base.metadata.create_all(engine)
    url_file = tmp_path / "url.txt"
    url_file.write_text(url, encoding="utf-8")
    return type("Env", (), {"engine": engine, "url": url, "url_file": str(url_file), "tmp": tmp_path})


def put(env, ticker, pe, filed, metrics=None, source="sec"):
    metrics = METRICS if metrics is None else metrics
    with env.engine.begin() as c:
        for metric, value in metrics.items():
            c.execute(sa.insert(fix.T).values(
                ticker=ticker, metric=metric, value=value, period_end=pe, fiscal_period="Q2",
                filing_date=filed, source=source, restated=False, source_tag=metric))


def twin(env, ticker, real, rounded, filed, **kw):
    put(env, ticker, rounded, filed, **kw)
    put(env, ticker, real, filed, **kw)


def snapshot(env):
    with env.engine.connect() as c:
        return sorted(tuple(r) for r in c.execute(sa.select(*[fix.T.c[n] for n in fix.COLUMNS])))


def plan_of(env, **kw):
    with env.engine.connect() as c:
        return fix.build_plan(c, **kw)


def ids_at(env, ticker, pe):
    with env.engine.connect() as c:
        return {r[0] for r in c.execute(sa.select(fix.T.c.id).where(fix.T.c.ticker == ticker, fix.T.c.period_end == pe))}


CASES = [  # real, rounded, filed -- the filings the rule was drawn from
    ("AAPL", D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)),
    ("NVDA", D(2026, 4, 26), D(2026, 4, 30), D(2026, 5, 20)),
    ("DE", D(2026, 5, 3), D(2026, 4, 30), D(2026, 5, 28)),
    ("COST", D(2026, 5, 10), D(2026, 4, 30), D(2026, 6, 3)),
]


# ---- the rule -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,real,rounded,filed", CASES, ids=[c[0] for c in CASES])
def test_the_rounded_rows_are_the_ones_deleted_and_the_real_rows_are_kept(env, ticker, real, rounded, filed):
    twin(env, ticker, real, rounded, filed)
    plan = plan_of(env)
    assert [(p.rounded_pe, p.real_pe) for p in plan.ok] == [(rounded, real)]
    assert plan.excluded == []
    assert plan.delete_ids == ids_at(env, ticker, rounded)
    assert not plan.delete_ids & ids_at(env, ticker, real)
    assert set(plan.twin_id.values()) == ids_at(env, ticker, real)


def test_calendar_aligned_filers_are_untouched(env):
    for pe, filed in ((D(2026, 3, 31), D(2026, 4, 30)), (D(2026, 6, 30), D(2026, 7, 30))):
        put(env, "MSFT", pe, filed)
    plan = plan_of(env)
    assert plan.ok == [] and plan.excluded == [] and plan.delete_rows == []


def test_two_real_quarters_91_days_apart_are_two_pairs_never_merged(env):
    twin(env, "AAPL", D(2025, 12, 27), D(2025, 12, 31), D(2026, 1, 30))
    twin(env, "AAPL", D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1))
    plan = plan_of(env)
    assert sorted((p.rounded_pe, p.real_pe) for p in plan.ok) == [
        (D(2025, 12, 31), D(2025, 12, 27)), (D(2026, 3, 31), D(2026, 3, 28))]


def test_a_month_end_whose_real_date_is_only_in_another_filing_is_left_alone(env):
    put(env, "AAPL", D(2026, 3, 31), D(2026, 5, 1))
    put(env, "AAPL", D(2026, 3, 28), D(2026, 9, 1))      # a later filing's comparative
    plan = plan_of(env)
    assert plan.delete_rows == [] and plan.excluded == []
    assert plan.lone_month_ends == 1


def test_a_lone_month_end_is_not_this_tools_job(env):
    put(env, "AAPL", D(2026, 3, 31), D(2026, 5, 1))
    put(env, "AAPL", D(2026, 6, 27), D(2026, 7, 31))
    assert plan_of(env).delete_rows == []


# ---- the all-metrics check ------------------------------------------------------------------------------------------

def test_one_metric_that_differs_excludes_the_whole_pair(env):
    real, rounded, filed = D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)
    put(env, "AAPL", rounded, filed, {**METRICS, "total_liabilities": 111.0})
    put(env, "AAPL", real, filed)
    plan = plan_of(env)
    assert plan.delete_rows == [] and plan.ok == []
    [e] = plan.excluded
    assert e.reason == "value_differs" and e.metrics == ("total_liabilities",)
    assert e.rows == 3                      # all three rounded rows are protected, not just the bad one


def test_a_metric_only_on_the_rounded_side_excludes_the_pair(env):
    real, rounded, filed = D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)
    put(env, "AAPL", rounded, filed, {**METRICS, "goodwill": 5.0})
    put(env, "AAPL", real, filed)
    plan = plan_of(env)
    assert plan.delete_rows == []
    [e] = plan.excluded
    assert e.reason == "only_on_rounded" and e.metrics == ("goodwill",)


def test_a_metric_only_on_the_real_side_loses_nothing_so_the_pair_is_deleted(env):
    real, rounded, filed = D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)
    put(env, "AAPL", rounded, filed)
    put(env, "AAPL", real, filed, {**METRICS, "goodwill": 5.0})
    plan = plan_of(env)
    assert plan.excluded == [] and len(plan.delete_rows) == 3


def test_the_check_is_per_source(env):
    """A metric the real side holds under another source does not cover the rounded one."""
    real, rounded, filed = D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)
    put(env, "AAPL", rounded, filed, {"total_assets": 1.0}, source="fmp")
    put(env, "AAPL", real, filed, {"total_assets": 1.0}, source="sec")
    [e] = plan_of(env).excluded
    assert e.reason == "only_on_rounded"


def test_values_equal_within_tolerance_and_none_equals_none(env):
    assert fix.values_equal(1_000_000_000.0, 1_000_000_000.5)
    assert not fix.values_equal(1000.0, 1002.0)
    assert fix.values_equal(None, None) and not fix.values_equal(None, 0.0)
    real, rounded, filed = D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1)
    put(env, "AAPL", rounded, filed, {"total_assets": 1_000_000_000.0, "cash": None})
    put(env, "AAPL", real, filed, {"total_assets": 1_000_000_000.5, "cash": None})
    assert plan_of(env).excluded == [] and len(plan_of(env).delete_rows) == 2


def test_an_ambiguous_twin_is_reported_not_silently_skipped(env):
    """03-31 with real dates 03-28 and 04-03, both three days away: which is 'the' twin is a guess."""
    filed = D(2026, 5, 1)
    put(env, "AAPL", D(2026, 3, 31), filed)
    put(env, "AAPL", D(2026, 3, 28), filed)
    put(env, "AAPL", D(2026, 4, 3), filed)
    plan = plan_of(env)
    assert plan.delete_rows == []
    [e] = plan.excluded
    assert e.reason == "ambiguous_twin" and e.pair.rounded_pe == D(2026, 3, 31)


def test_two_month_ends_claiming_one_real_date_are_both_excluded(env):
    filed = D(2026, 5, 1)
    for pe in (D(2026, 3, 31), D(2026, 4, 30), D(2026, 4, 15)):     # 04-15 is 15 days from both
        put(env, "AAPL", pe, filed)
    plan = plan_of(env)
    assert plan.delete_rows == []
    assert sorted(e.pair.rounded_pe for e in plan.excluded) == [D(2026, 3, 31), D(2026, 4, 30)]
    assert {e.reason for e in plan.excluded} == {"ambiguous_twin"}


# ---- invariants -----------------------------------------------------------------------------------------------------

def test_verify_plan_refuses_a_non_month_end_row_and_a_missing_twin():
    row = {"id": 1, "period_end": D(2026, 3, 28)}
    with pytest.raises(fix.PlanError, match="not a month-end"):
        fix.verify_plan(fix.Plan(delete_rows=[row], twin_id={1: 2}))
    with pytest.raises(fix.PlanError, match="no surviving"):
        fix.verify_plan(fix.Plan(delete_rows=[{"id": 1, "period_end": D(2026, 3, 31)}], twin_id={}))
    with pytest.raises(fix.PlanError, match="no surviving"):       # twin is itself being deleted
        fix.verify_plan(fix.Plan(delete_rows=[{"id": 1, "period_end": D(2026, 3, 31)},
                                              {"id": 2, "period_end": D(2026, 3, 31)}], twin_id={1: 2, 2: 1}))


# ---- dry run ---------------------------------------------------------------------------------------------------------

def seed_mixed(env):
    twin(env, "AAPL", D(2026, 3, 28), D(2026, 3, 31), D(2026, 5, 1))
    twin(env, "NVDA", D(2026, 4, 26), D(2026, 4, 30), D(2026, 5, 20))
    put(env, "BAD", D(2026, 3, 31), D(2026, 5, 1), {**METRICS, "total_liabilities": 1.0})
    put(env, "BAD", D(2026, 3, 28), D(2026, 5, 1))
    put(env, "MSFT", D(2026, 3, 31), D(2026, 4, 30))
    put(env, "AAPL", D(2025, 9, 30), D(2025, 10, 31))       # lone month end


def test_the_dry_run_changes_nothing_and_is_idempotent(env, capsys):
    seed_mixed(env)
    before = snapshot(env)
    assert fix.main(["--url-file", env.url_file, "--show", "AAPL"]) == 0
    first = capsys.readouterr().out
    assert fix.main(["--url-file", env.url_file, "--show", "AAPL"]) == 0
    second = capsys.readouterr().out
    assert snapshot(env) == before and first == second
    assert "dry run only: nothing was changed" in first


def test_the_dry_run_report_has_the_totals_the_reviewer_needs(env, capsys):
    seed_mixed(env)
    fix.main(["--url-file", env.url_file, "--show", "AAPL,NVDA", "--exclusions-csv", str(env.tmp / "ex.csv")])
    out = capsys.readouterr().out
    assert "deletable:  2 pairs" in out and "EXCLUDED:   1 pairs" in out
    assert "value_differs" in out and "DELETE_DUP set: 6 rows, 2 tickers, 2 filings" in out
    assert "month-end copy of 2026-03-28 -- DELETE (3 rows)" in out
    assert "real date -- kept" in out and "left alone" in out
    assert (env.tmp / "ex.csv").read_text().splitlines()[1].startswith("BAD,2026-05-01,2026-03-31,2026-03-28,value_differs")


def test_the_read_only_connection_is_refused_by_the_connection_not_the_script(env):
    from src.storage.script_db import make_engine

    ro = make_engine(env.url, readonly=True)
    with ro.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(fix.T)).scalar_one() == 0
    seed_mixed(env)
    with pytest.raises(Exception):
        with ro.begin() as c:
            c.execute(sa.delete(fix.T))                       # cursor guard refuses
    raw = ro.raw_connection()                                 # and the database itself refuses
    with pytest.raises(Exception, match="readonly|read-only|read only"):
        raw.cursor().execute("DELETE FROM fundamentals")
    raw.close()
    assert len(snapshot(env)) > 0


# ---- --apply: refusals -----------------------------------------------------------------------------------------------

def phrase(_prompt):
    return fix.CONFIRM_PHRASE


def apply_args(env, **over):
    a = {"--url-file": env.url_file, "--railway-backup-confirmed": "railway backup 2026-10-16 21:00",
         "--backup-path": str(env.tmp / "backup.csv")}
    a.update(over)
    return ["--apply"] + [x for k, v in a.items() if v is not None for x in (k, v)]


def test_apply_is_refused_without_the_backup_confirmation(env, capsys):
    seed_mixed(env)
    before = snapshot(env)
    assert fix.main(apply_args(env, **{"--railway-backup-confirmed": None}), input_fn=phrase) == 2
    assert fix.main(apply_args(env, **{"--railway-backup-confirmed": "  "}), input_fn=phrase) == 2
    assert "Railway backup" in capsys.readouterr().err
    assert snapshot(env) == before and not (env.tmp / "backup.csv").exists()


def test_apply_is_refused_without_a_backup_path_or_over_an_existing_one(env):
    seed_mixed(env)
    before = snapshot(env)
    assert fix.main(apply_args(env, **{"--backup-path": None}), input_fn=phrase) == 2
    existing = env.tmp / "old.csv"
    existing.write_text("keep me")
    assert fix.main(apply_args(env, **{"--backup-path": str(existing)}), input_fn=phrase) == 2
    assert existing.read_text() == "keep me" and snapshot(env) == before


def test_apply_is_refused_when_not_interactive(env, capsys):
    seed_mixed(env)
    before = snapshot(env)
    assert fix.main(apply_args(env), interactive=False) == 2
    assert "interactive" in capsys.readouterr().err
    assert snapshot(env) == before and not (env.tmp / "backup.csv").exists()


def test_a_wrong_confirmation_phrase_deletes_nothing_and_writes_no_backup(env):
    seed_mixed(env)
    before = snapshot(env)
    assert fix.main(apply_args(env), input_fn=lambda _p: "yes") == 1
    assert snapshot(env) == before and not (env.tmp / "backup.csv").exists()


# ---- --apply: the happy path and the rollbacks -----------------------------------------------------------------------

def test_apply_backs_up_then_deletes_exactly_the_rounded_rows(env, capsys):
    import csv

    seed_mixed(env)
    before = snapshot(env)
    deleted = ids_at(env, "AAPL", D(2026, 3, 31)) | ids_at(env, "NVDA", D(2026, 4, 30))
    twins = ids_at(env, "AAPL", D(2026, 3, 28)) | ids_at(env, "NVDA", D(2026, 4, 26))
    assert fix.main(apply_args(env), input_fn=phrase) == 0
    after = snapshot(env)
    assert {r[0] for r in before} - {r[0] for r in after} == deleted
    assert {r[0] for r in after} >= twins                      # every real-dated twin survived
    assert [r for r in before if r[0] not in deleted] == after # nothing else changed, not even the excluded pair
    with open(env.tmp / "backup.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {int(r["id"]) for r in rows} == deleted and len(rows) == 6
    assert all(r["period_end"] in ("2026-03-31", "2026-04-30") and r["surviving_twin_id"] for r in rows)
    assert "deleted 6 rows" in capsys.readouterr().out
    assert fix.main(["--url-file", env.url_file]) == 0         # a second dry run finds nothing left to do
    assert "deletable:  0 pairs" in capsys.readouterr().out


def test_a_count_mismatch_rolls_everything_back(env, monkeypatch):
    seed_mixed(env)
    before = snapshot(env)
    real_delete = sa.delete
    monkeypatch.setattr(fix, "delete", lambda t: real_delete(t).where(t.c.id % 2 == 0))   # deletes only half
    assert fix.main(apply_args(env), input_fn=phrase) == 2
    assert snapshot(env) == before


def test_a_failing_backup_stops_before_any_delete(env, monkeypatch):
    seed_mixed(env)
    before = snapshot(env)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(fix, "write_backup", boom)
    assert fix.main(apply_args(env), input_fn=phrase) == 2
    assert snapshot(env) == before


def test_apply_refuses_if_the_data_changed_since_the_dry_run(env):
    seed_mixed(env)
    with env.engine.connect() as c:
        shown = fix.build_plan(c)
    twin(env, "ADBE", D(2026, 5, 29), D(2026, 5, 31), D(2026, 6, 15))     # a new duplicate appears
    before = snapshot(env)
    with pytest.raises(fix.PlanError, match="data changed"):
        fix.apply_delete(fix.make_engine(env.url, readonly=False), shown, env.tmp / "b.csv")
    assert snapshot(env) == before


def test_the_database_url_never_appears_in_output_or_errors(env, tmp_path, capsys):
    secret = "hunter2-very-secret"
    f = tmp_path / "pg.txt"
    f.write_text(f"postgresql://someuser:{secret}@127.0.0.1:1/nodb")
    rc = fix.main(["--url-file", str(f)])
    cap = capsys.readouterr()
    assert rc == 2
    assert secret not in cap.out and secret not in cap.err and "someuser" not in cap.err


def test_the_apps_own_database_url_is_never_a_fallback(env, monkeypatch, capsys):
    monkeypatch.delenv("PERIOD_FIX_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", env.url)
    assert fix.main([]) == 2
    assert "no database URL" in capsys.readouterr().err


def test_the_cursor_guard_refuses_writes_hidden_inside_a_read():
    """The database's own read-only mode is the real protection; this is the second line behind it."""
    from src.storage.script_db import _guard

    ok = ["SELECT * FROM fundamentals WHERE restated = 0", "  with x as (select 1) select * from x",
          "SHOW transaction_read_only", "PRAGMA table_info(fundamentals)"]
    bad = ["DELETE FROM fundamentals", "WITH d AS (DELETE FROM fundamentals RETURNING id) SELECT * FROM d",
           "EXPLAIN ANALYZE DELETE FROM fundamentals", "select 1; drop table fundamentals",
           "INSERT INTO fundamentals VALUES (1)"]
    for stmt in ok:
        _guard(None, None, stmt, None, None, False)
    for stmt in bad:
        with pytest.raises(RuntimeError, match="not a read"):
            _guard(None, None, stmt, None, None, False)


# ---------------------------------------------------------------------------
# pair_reports: which copy is bulk, whether values differ, what DELETE_DUP must keep
# ---------------------------------------------------------------------------

def _prow(i, metric, value, pe, fp, tag, filed=dt.date(2026, 5, 1)):
    return {"id": i, "ticker": "T", "metric": metric, "value": value, "period_end": pe,
            "fiscal_period": fp, "filing_date": filed, "source": "sec", "restated": False,
            "source_tag": tag, "ingested_at": None}


def _reports(rounded_rows, real_rows):
    pair = fix.Pair("T", dt.date(2026, 5, 1), dt.date(2026, 3, 31), dt.date(2026, 3, 28))
    rows = {("T", pair.filing_date, pair.rounded_pe): rounded_rows,
            ("T", pair.filing_date, pair.real_pe): real_rows}
    ok, _excluded, _delete, _twin = fix.classify([pair], rows)
    return fix.pair_reports([pair], rows, ok)[0]


def test_the_month_end_copy_with_a_fiscal_period_is_the_bulk_copy():
    r = _reports([_prow(1, "total_assets", 100.0, dt.date(2026, 3, 31), "Q2", "Assets")],
                 [_prow(2, "total_assets", 100.0, dt.date(2026, 3, 28), None, "Assets")])
    assert r.bulk_copy == "rounded" and r.deletable and r.values_differ == 0
    assert r.keep.startswith("real-dated copy")          # equal value and tag: lossless


def test_equal_values_with_different_tags_must_keep_the_bulk_copy_not_the_frames_tag():
    r = _reports([_prow(1, "revenue", 100.0, dt.date(2026, 3, 31), "Q2", "Revenues")],
                 [_prow(2, "revenue", 100.0, dt.date(2026, 3, 28), None, "RevenueFromContractWithCustomerExcludingAssessedTax")])
    assert r.deletable and r.tags_differ_equal == 1
    assert r.keep.startswith("bulk copy (rounded)") and "re-date" in r.keep


def test_a_pair_whose_values_differ_is_not_deletable_and_keeps_both():
    r = _reports([_prow(1, "cogs", 43_151_000.0, dt.date(2026, 3, 31), "Q2", "CostOfGoodsAndServicesSold")],
                 [_prow(2, "cogs", 4_269_000.0, dt.date(2026, 3, 28), None, "CostOfRevenue")])
    assert not r.deletable and r.values_differ == 1 and r.bulk_copy == "rounded"
    assert r.keep.startswith("both")


def test_mixed_fiscal_periods_are_ambiguous_not_guessed():
    r = _reports([_prow(1, "a", 1.0, dt.date(2026, 3, 31), "Q2", "A"), _prow(3, "b", 1.0, dt.date(2026, 3, 31), None, "B")],
                 [_prow(2, "a", 1.0, dt.date(2026, 3, 28), None, "A"), _prow(4, "b", 1.0, dt.date(2026, 3, 28), None, "B")])
    assert r.bulk_copy == "ambiguous"
