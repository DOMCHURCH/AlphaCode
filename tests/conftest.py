from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENV", "dev")
os.environ.setdefault("SEC_USER_AGENT", "AlphaFunnel Test test@example.com")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.storage.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_identity_cache():
    """Start every test with no memory of another test's database.

    The home page's identity figure is cached in module state and warmed by a
    background thread the API starts at boot, so without this a count from one
    test's data can be served to the next -- which is exactly how
    test_the_identity_line_reports_the_real_pass_rate began failing at random
    in full-suite runs while passing on its own.
    """
    from src.company.stats import reset_identity_cache

    reset_identity_cache()
    yield
    reset_identity_cache()


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def price_panels():
    """Deterministic 420-day panel: 40 uptrend names, 40 downtrend names."""
    rng = np.random.default_rng(1234)
    dates = [d.date() for d in pd.bdate_range("2023-01-02", periods=420)]
    up = [f"UP{i:02d}" for i in range(40)]
    down = [f"DN{i:02d}" for i in range(40)]
    tickers = up + down

    # Drift dominates vol over 420 bars, so the two groups separate reliably.
    # A weaker drift lets individual random paths cross over and makes the
    # "no downtrend survives" assertion a coin flip rather than a test.
    drift = np.array([0.0018] * len(up) + [-0.0018] * len(down))
    rets = rng.normal(0, 0.008, (len(dates), len(tickers))) + drift
    close = pd.DataFrame(
        100 * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=tickers
    )
    high = close * 1.008
    low = close * 0.992
    volume = pd.DataFrame(
        rng.lognormal(13.5, 0.3, close.shape), index=dates, columns=tickers
    )
    sectors = pd.Series(
        ["Technology", "Healthcare", "Energy", "Financial Services"]
        * (len(tickers) // 4),
        index=tickers,
    )
    return {
        "close": close, "high": high, "low": low, "volume": volume,
        "sectors": sectors, "up": up, "down": down, "dates": dates,
    }


@pytest.fixture
def as_of() -> dt.date:
    return dt.date(2024, 8, 1)
