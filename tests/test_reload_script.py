"""scripts/reload_fundamentals.py replaces the table atomically; it has no wipe.

`--wipe` called `wipe_fundamentals()`, which commits its delete, and only then
began downloading. A SEC 429 on quarter four left the table empty and three
quarters loaded -- the exact loss `_replace_fundamentals` was written to make
impossible. The script now goes through `backfill.reload_fundamentals`, which
downloads first and deletes inside the same transaction as the reload.

No network and no database: the backfill entry points are replaced.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reload_fundamentals.py"


@pytest.fixture()
def script(monkeypatch):
    spec = importlib.util.spec_from_file_location("reload_fundamentals_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///unused-by-this-test.db")
    monkeypatch.setenv("SEC_USER_AGENT", "Test Suite test@example.com")
    return mod


@pytest.fixture()
def backfill_stubs(monkeypatch):
    """`reload_fundamentals` recorded; `wipe_fundamentals` fatal if touched."""
    import src.backfill as backfill

    calls: list[int] = []

    async def fake_reload(quarters=7):
        calls.append(quarters)
        return {
            "rows_deleted": 10, "rows_written": 7, "quarters_loaded": quarters,
            "unpublished": [], "staged": [], "extraction": {}, "verification": {},
        }

    def no_wipe(*a, **k):
        pytest.fail("the script must not delete before it has loaded anything")

    monkeypatch.setattr(backfill, "reload_fundamentals", fake_reload)
    monkeypatch.setattr(backfill, "wipe_fundamentals", no_wipe)
    monkeypatch.setattr(backfill, "get_extraction_reports", lambda: {})
    return calls


def test_wipe_is_no_longer_an_option(script, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["reload_fundamentals.py", "--wipe"])
    with pytest.raises(SystemExit) as exc:
        script.main()
    assert exc.value.code == 2
    assert "--wipe" in capsys.readouterr().err


def test_the_script_has_no_wipe_function(script):
    assert not hasattr(script, "wipe")


def test_a_reload_goes_through_the_atomic_replace(script, backfill_stubs, monkeypatch):
    monkeypatch.setattr(script, "coverage", lambda: None)
    monkeypatch.setattr(script, "verify", lambda: True)
    monkeypatch.setattr(sys, "argv", ["reload_fundamentals.py", "--quarters", "3"])

    assert script.main() == 0
    assert backfill_stubs == [3]


def test_a_failed_reload_does_not_continue_to_verification(script, monkeypatch):
    """If the replace rolled back, verifying the old table and exiting 0 would
    read as a successful reload."""
    import src.backfill as backfill

    async def boom(quarters=7):
        raise RuntimeError("rolled back")

    monkeypatch.setattr(backfill, "reload_fundamentals", boom)
    monkeypatch.setattr(script, "verify", lambda: pytest.fail("verified after a failed reload"))
    monkeypatch.setattr(sys, "argv", ["reload_fundamentals.py"])
    with pytest.raises(RuntimeError, match="rolled back"):
        script.main()


def test_verify_only_does_not_reload(script, backfill_stubs, monkeypatch):
    monkeypatch.setattr(script, "coverage", lambda: None)
    monkeypatch.setattr(script, "verify", lambda: True)
    monkeypatch.setattr(sys, "argv", ["reload_fundamentals.py", "--verify-only"])

    assert script.main() == 0
    assert backfill_stubs == []
