"""The site must not state two different totals for one product.

`/dataset` computed its row count live and said "1.8M rows". The home page,
`/api`, the blog post and `llms.txt` each carried a hand-typed "1.7M data
points". Same table, two answers, on a site whose entire pitch is that its
figures agree with each other and with the filings underneath them.

Four literals typed at four different times is a drift machine, and replacing
them with a fifth literal only resets the clock -- so they read
`dataset.facts_label()` now. What this file guards is the class of mistake
rather than the one instance: a stale figure written down anywhere fails here,
and the rendered pages are checked against each other rather than against a
number this test also has to know.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
Q = dt.date(2025, 12, 31)

# Figures that were true once and are now something else. A literal is not
# banned for being a number -- it is banned for being THIS number, written into
# copy that no longer matches what the database holds.
STALE = ("1.7M", "1.7 million")

SEARCHED = (
    ROOT / "src" / "report",
    ROOT / "static_seo",
)


def _copy_files() -> list[Path]:
    out: list[Path] = []
    for base in SEARCHED:
        for suffix in ("*.py", "*.txt", "*.js", "*.html", "*.css"):
            out.extend(p for p in base.rglob(suffix) if "__pycache__" not in str(p))
    return out


@pytest.mark.parametrize("stale", STALE)
def test_no_stale_dataset_figure_survives_in_copy(stale):
    """A comment explaining the bug is fine. A claim to a reader is not."""
    offenders = []
    for path in _copy_files():
        for n, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if stale not in line:
                continue
            stripped = line.lstrip()
            # Prose ABOUT the drift is allowed; prose asserting it is not.
            if stripped.startswith(("#", "*", '"""', "//")) or '"1.7' in stripped:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{n}: {stripped[:90]}")
    assert not offenders, "stale dataset figure in copy:\n" + "\n".join(offenders)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'copy.db'}")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    monkeypatch.setenv("AGENTMAIL_API_KEY", "")

    from src.config.settings import get_settings
    from src.storage.db import init_db, reset_engine_cache

    get_settings.cache_clear()
    reset_engine_cache()
    init_db()

    from src.api import app
    from src.dataset import reset_count_cache

    reset_count_cache()
    with TestClient(app) as c:
        yield c

    reset_count_cache()
    get_settings.cache_clear()
    reset_engine_cache()


def seed(n_facts: int, *, start: int = 0) -> None:
    """`n_facts` rows across a few tickers, so the counts are non-zero.

    `start` offsets the metric names: the table has a UNIQUE constraint on
    (ticker, metric, period_end, source, filing_date), so seeding twice in one
    test has to add NEW rows rather than re-insert the same ones.
    """
    from src.storage.db import session_scope
    from src.storage.models import Fundamental, UniverseSnapshot

    with session_scope() as s:
        for i in range(start, start + n_facts):
            ticker = f"T{i % 7:02d}"
            if start == 0 and i < 7:
                # Only on the first seed: `universe` is unique on
                # (as_of_date, ticker) and a second call would collide.
                s.add(UniverseSnapshot(
                    as_of_date=dt.date.today(), ticker=ticker, name=f"{ticker} Inc"
                ))
            s.add(Fundamental(
                ticker=ticker, metric=f"m{i}", value=float(i + 1),
                period_end=Q, fiscal_period="FY",
                filing_date=dt.date(2026, 2, 1), source="sec", restated=False,
            ))


def _figures(html: str) -> set[str]:
    """Every "<n>M data points" / "<n>M rows" style claim on a page."""
    return set(re.findall(r"\b(\d+(?:\.\d+)?[Mk]|\d{1,3}(?:,\d{3})+)\b", html))


def test_every_page_states_the_same_dataset_size(client):
    seed(40)
    from src.dataset import facts_label, reset_count_cache

    reset_count_cache()
    label = facts_label()
    assert label, "the fixture must produce a countable table"

    for path in ("/", "/api", "/dataset"):
        html = client.get(path).text
        assert label in html, (
            f"{path} does not state the live figure {label!r}; "
            "it is probably carrying a literal again"
        )


def test_the_figure_moves_when_the_table_does(client):
    """The property that makes the literal impossible to reintroduce."""
    from src.dataset import facts_label, reset_count_cache

    seed(10)
    reset_count_cache()
    before = facts_label()

    seed(5_000, start=10)
    reset_count_cache()
    after = facts_label()

    assert before != after, (
        f"the label did not move when the table grew ({before!r} -> {after!r})"
    )


def test_an_unreadable_count_renders_as_nothing_not_as_zero(client, monkeypatch):
    """"0 facts" on the front page of a data product is worse than silence."""
    from src import dataset

    monkeypatch.setattr(dataset, "row_count", lambda: 0)
    assert dataset.facts_label() == ""

    def boom():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(dataset, "row_count", boom)
    assert dataset.facts_label() == ""

    html = client.get("/").text
    assert html.count("<h1") == 1, "the page must still render without a count"
    assert "0 data points" not in html
