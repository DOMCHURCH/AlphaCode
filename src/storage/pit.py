"""Point-in-time accessors.

Every fundamental read in the system goes through here. The contract is:

    a fundamental datapoint is readable on date D only if
        filing_date + PIT_LAG_BUSINESS_DAYS <= D

Nothing else may query the `fundamentals` table directly. The lookahead-bias
unit test (tests/test_pit.py) asserts this invariant holds, and that test is
the difference between a real system and a toy.

Restatement handling: when two rows exist for the same (ticker, metric,
period_end), we take the one with the LATEST filing_date that is still visible
as of the query date. That reproduces what an observer actually knew -- an
original figure before the restatement was filed, the restated figure after.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from src.config.settings import get_settings
from src.storage.models import (
    DailyBar,
    EarningsEvent,
    EstimateSnapshot,
    FilingEvent,
    Fundamental,
    InsiderTransaction,
    UniverseSnapshot,
)


class LookaheadError(RuntimeError):
    """Raised when a caller tries to read data that was not yet public."""


def add_business_days(d: dt.date, n: int) -> dt.date:
    """Add n business days (Mon-Fri). Holidays are ignored deliberately -- the
    buffer models ingestion lag, not settlement, so it only needs to be safe."""
    if n == 0:
        return d
    step = 1 if n > 0 else -1
    remaining = abs(n)
    cur = d
    while remaining > 0:
        cur += dt.timedelta(days=step)
        if cur.weekday() < 5:
            remaining -= 1
    return cur


def visible_from(filing_date: dt.date, lag_days: int | None = None) -> dt.date:
    """The first date on which a record filed on `filing_date` may be read."""
    lag = get_settings().pit_lag_business_days if lag_days is None else lag_days
    return add_business_days(filing_date, lag)


def _pit_filter(stmt: Select, as_of: dt.date, lag_days: int | None) -> Select:
    """Restrict a fundamentals select to rows already public as of `as_of`.

    We push the comparison down to SQL as `filing_date <= as_of` (a necessary
    condition) and then apply the exact business-day lag in Python. Doing the
    business-day arithmetic in SQL portably is not worth it; the pre-filter
    already removes the overwhelming majority of rows.
    """
    return stmt.where(Fundamental.filing_date <= as_of)


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------
def get_fundamentals(
    session: Session,
    ticker: str | Sequence[str],
    as_of: dt.date,
    metrics: Iterable[str] | None = None,
    *,
    lookback_quarters: int = 12,
    lag_days: int | None = None,
    source_preference: Sequence[str] = ("sec", "fmp", "yahoo"),
) -> pd.DataFrame:
    """Return the fundamental record set knowable on `as_of`.

    Columns: ticker, metric, value, period_end, fiscal_period, filing_date,
             source, restated.

    One row per (ticker, metric, period_end) -- the latest visible filing wins.
    """
    tickers = [ticker] if isinstance(ticker, str) else list(ticker)
    if not tickers:
        return _empty_fundamentals()

    stmt = select(
        Fundamental.ticker,
        Fundamental.metric,
        Fundamental.value,
        Fundamental.period_end,
        Fundamental.fiscal_period,
        Fundamental.filing_date,
        Fundamental.source,
        Fundamental.restated,
    ).where(Fundamental.ticker.in_(tickers))
    if metrics is not None:
        metrics = list(metrics)
        if not metrics:
            return _empty_fundamentals()
        stmt = stmt.where(Fundamental.metric.in_(metrics))
    stmt = _pit_filter(stmt, as_of, lag_days)

    rows = session.execute(stmt).all()
    if not rows:
        return _empty_fundamentals()

    df = pd.DataFrame(rows, columns=_empty_fundamentals().columns)

    # Exact business-day lag, applied after the coarse SQL filter.
    lag = get_settings().pit_lag_business_days if lag_days is None else lag_days
    if lag:
        visible = df["filing_date"].map(lambda d: visible_from(d, lag))
        df = df[visible <= as_of]
    if df.empty:
        return _empty_fundamentals()

    # Restatement resolution: latest visible filing per (ticker, metric, period).
    # `.last()` takes the bottom row, so filing_date sorts ascending (latest
    # last) and the source rank sorts descending (most-preferred last) -- on a
    # filing-date tie, SEC as-reported beats a vendor's restated figure.
    rank = {s: i for i, s in enumerate(source_preference)}
    df = df.assign(_src_rank=df["source"].map(lambda s: rank.get(s, 99)))
    df = (
        df.sort_values(["filing_date", "_src_rank"], ascending=[True, False])
        .groupby(["ticker", "metric", "period_end"], as_index=False)
        .last()
        .drop(columns=["_src_rank"])
    )

    if lookback_quarters:
        df = (
            df.sort_values("period_end")
            .groupby(["ticker", "metric"], as_index=False)
            .tail(lookback_quarters)
        )
    return df.reset_index(drop=True)


def _empty_fundamentals() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ticker",
            "metric",
            "value",
            "period_end",
            "fiscal_period",
            "filing_date",
            "source",
            "restated",
        ]
    )


def get_latest_fundamentals_wide(
    session: Session,
    tickers: Sequence[str],
    as_of: dt.date,
    metrics: Iterable[str],
    *,
    lag_days: int | None = None,
) -> pd.DataFrame:
    """Most recent visible value per (ticker, metric), pivoted wide.

    Index: ticker. Columns: the requested metrics. Missing stays NaN -- we never
    forward-fill across a reporting gap and never impute the universe mean.
    """
    metrics = list(metrics)
    long = get_fundamentals(
        session, tickers, as_of, metrics, lookback_quarters=1, lag_days=lag_days
    )
    wide = pd.DataFrame(index=pd.Index(sorted(set(tickers)), name="ticker"))
    for m in metrics:
        wide[m] = np.nan
    if long.empty:
        return wide
    latest = (
        long.sort_values("period_end")
        .groupby(["ticker", "metric"], as_index=False)
        .last()
    )
    pivot = latest.pivot(index="ticker", columns="metric", values="value")
    for m in metrics:
        if m in pivot.columns:
            wide[m] = pivot[m].reindex(wide.index)
    return wide


def get_fundamental_series(
    session: Session,
    ticker: str,
    as_of: dt.date,
    metric: str,
    *,
    quarters: int = 8,
) -> pd.DataFrame:
    """Trailing quarterly series for one metric, PIT-safe. Used by charts."""
    df = get_fundamentals(
        session, ticker, as_of, [metric], lookback_quarters=quarters
    )
    if df.empty:
        return df
    return df.sort_values("period_end").reset_index(drop=True)


def assert_no_lookahead(
    session: Session, as_of: dt.date, *, lag_days: int | None = None
) -> None:
    """Belt-and-braces runtime check used by the pipeline before Stage 2.

    Scans what the PIT accessor would return and raises if anything sneaked
    through with a filing_date after `as_of`.
    """
    lag = get_settings().pit_lag_business_days if lag_days is None else lag_days
    stmt = select(Fundamental.ticker, Fundamental.filing_date).where(
        Fundamental.filing_date > as_of
    )
    leaked = session.execute(stmt.limit(1)).first()
    if leaked is not None:
        # Presence in the table is fine; readability is not. Verify the accessor
        # actually excludes it.
        df = get_fundamentals(session, leaked[0], as_of, lag_days=lag)
        if not df.empty and (df["filing_date"] > as_of).any():
            raise LookaheadError(
                f"PIT accessor leaked a future filing for {leaked[0]} as of {as_of}"
            )


# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
def get_bars(
    session: Session,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """Long-format OHLCV for a ticker set over [start, end]."""
    if not tickers:
        return pd.DataFrame(
            columns=["ticker", "date", "open", "high", "low", "close", "volume"]
        )
    stmt = (
        select(
            DailyBar.ticker,
            DailyBar.date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.volume,
        )
        .where(DailyBar.ticker.in_(list(tickers)))
        .where(DailyBar.date >= start)
        .where(DailyBar.date <= end)
        .order_by(DailyBar.ticker, DailyBar.date)
    )
    rows = session.execute(stmt).all()
    return pd.DataFrame(
        rows, columns=["ticker", "date", "open", "high", "low", "close", "volume"]
    )


def get_price_panel(
    session: Session,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    field: str = "close",
) -> pd.DataFrame:
    """Wide panel: index=date, columns=ticker. This is what Stage 1 vectorises."""
    long = get_bars(session, tickers, start, end)
    if long.empty:
        return pd.DataFrame()
    return long.pivot(index="date", columns="ticker", values=field).sort_index()


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
def get_universe(session: Session, as_of: dt.date) -> pd.DataFrame:
    """The universe as it existed on `as_of`. Falls back to the most recent
    prior snapshot -- never to today's live listing."""
    stmt = select(UniverseSnapshot).where(UniverseSnapshot.as_of_date == as_of)
    rows = session.execute(stmt).scalars().all()
    if not rows:
        prior = session.execute(
            select(UniverseSnapshot.as_of_date)
            .where(UniverseSnapshot.as_of_date <= as_of)
            .order_by(UniverseSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if prior is None:
            return pd.DataFrame()
        rows = (
            session.execute(
                select(UniverseSnapshot).where(UniverseSnapshot.as_of_date == prior)
            )
            .scalars()
            .all()
        )
    return pd.DataFrame(
        [
            {
                "ticker": r.ticker,
                "name": r.name,
                "exchange": r.exchange,
                "security_type": r.security_type,
                "sector": r.sector,
                "sector_source": r.sector_source,
                "industry": r.industry,
                "cik": r.cik,
                "market_cap": r.market_cap,
                "close": r.close,
                "adv_20d": r.adv_20d,
                "as_of_date": r.as_of_date,
            }
            for r in rows
        ]
    )


# ---------------------------------------------------------------------------
# Estimates / earnings / events -- all PIT by construction
# ---------------------------------------------------------------------------
def get_estimate_revisions(
    session: Session, tickers: Sequence[str], as_of: dt.date, weeks: int = 4
) -> pd.DataFrame:
    """Change in consensus over the trailing `weeks`, computed by differencing
    two snapshots. Returns one row per ticker."""
    if not tickers:
        return pd.DataFrame(columns=["ticker", "eps_rev_4w", "rev_rev_4w"])
    start = as_of - dt.timedelta(weeks=weeks + 2)
    stmt = (
        select(
            EstimateSnapshot.ticker,
            EstimateSnapshot.captured_on,
            EstimateSnapshot.eps_consensus,
            EstimateSnapshot.revenue_consensus,
        )
        .where(EstimateSnapshot.ticker.in_(list(tickers)))
        .where(EstimateSnapshot.captured_on <= as_of)
        .where(EstimateSnapshot.captured_on >= start)
        .order_by(EstimateSnapshot.ticker, EstimateSnapshot.captured_on)
    )
    rows = session.execute(stmt).all()
    if not rows:
        return pd.DataFrame(columns=["ticker", "eps_rev_4w", "rev_rev_4w"])
    df = pd.DataFrame(
        rows, columns=["ticker", "captured_on", "eps_consensus", "revenue_consensus"]
    )
    cutoff = as_of - dt.timedelta(weeks=weeks)
    out = []
    for tkr, g in df.groupby("ticker"):
        g = g.sort_values("captured_on")
        old = g[g["captured_on"] <= cutoff].tail(1)
        new = g.tail(1)
        if old.empty or new.empty:
            continue
        row = {"ticker": tkr}
        for col, name in (
            ("eps_consensus", "eps_rev_4w"),
            ("revenue_consensus", "rev_rev_4w"),
        ):
            o = old[col].iloc[0]
            n = new[col].iloc[0]
            row[name] = (
                (n - o) / abs(o) if o not in (None, 0) and pd.notna(o) and pd.notna(n)
                else np.nan
            )
        out.append(row)
    return pd.DataFrame(out) if out else pd.DataFrame(
        columns=["ticker", "eps_rev_4w", "rev_rev_4w"]
    )


def get_last_earnings(
    session: Session, tickers: Sequence[str], as_of: dt.date
) -> pd.DataFrame:
    """Most recent *reported* earnings on or before `as_of`, plus SUE."""
    if not tickers:
        return pd.DataFrame()
    stmt = (
        select(
            EarningsEvent.ticker,
            EarningsEvent.report_date,
            EarningsEvent.actual_eps,
            EarningsEvent.consensus_eps,
            EarningsEvent.surprise_pct,
            EarningsEvent.gap_pct,
        )
        .where(EarningsEvent.ticker.in_(list(tickers)))
        .where(EarningsEvent.report_date <= as_of)
        .order_by(EarningsEvent.ticker, EarningsEvent.report_date)
    )
    rows = session.execute(stmt).all()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        rows,
        columns=[
            "ticker",
            "report_date",
            "actual_eps",
            "consensus_eps",
            "surprise_pct",
            "gap_pct",
        ],
    )
    out = []
    for tkr, g in df.groupby("ticker"):
        g = g.sort_values("report_date")
        last = g.iloc[-1]
        surprises = (g["actual_eps"] - g["consensus_eps"]).dropna()
        sd = float(surprises.std(ddof=1)) if len(surprises) >= 3 else np.nan
        raw = last["actual_eps"] - last["consensus_eps"]
        sue = float(raw / sd) if sd and sd > 0 and pd.notna(raw) else np.nan
        days = (as_of - last["report_date"]).days
        out.append(
            {
                "ticker": tkr,
                "last_report_date": last["report_date"],
                "days_since_earnings": days,
                "sue": sue,
                "earnings_gap": last["gap_pct"],
                "pead_window": max(0.0, 1.0 - days / 60.0),
            }
        )
    return pd.DataFrame(out)


def get_next_earnings(
    session: Session, tickers: Sequence[str], as_of: dt.date
) -> dict[str, dt.date]:
    """Scheduled future earnings dates, for the binary-event reject filter."""
    if not tickers:
        return {}
    stmt = (
        select(EarningsEvent.ticker, EarningsEvent.report_date)
        .where(EarningsEvent.ticker.in_(list(tickers)))
        .where(EarningsEvent.report_date > as_of)
        .order_by(EarningsEvent.ticker, EarningsEvent.report_date)
    )
    out: dict[str, dt.date] = {}
    for tkr, d in session.execute(stmt).all():
        out.setdefault(tkr, d)
    return out


def get_filings(
    session: Session, tickers: Sequence[str], as_of: dt.date, days: int = 45
) -> dict[str, list[dict]]:
    if not tickers:
        return {}
    start = as_of - dt.timedelta(days=days)
    stmt = (
        select(FilingEvent)
        .where(FilingEvent.ticker.in_(list(tickers)))
        .where(FilingEvent.filing_date <= as_of)
        .where(FilingEvent.filing_date >= start)
        .order_by(FilingEvent.filing_date.desc())
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for r in session.execute(stmt).scalars().all():
        out[r.ticker].append(
            {
                "form": r.form,
                "items": r.items,
                "filing_date": r.filing_date,
                "accession": r.accession,
                "detail": r.detail or {},
            }
        )
    return dict(out)


def get_insider_transactions(
    session: Session, tickers: Sequence[str], as_of: dt.date, days: int = 90
) -> dict[str, list[dict]]:
    if not tickers:
        return {}
    start = as_of - dt.timedelta(days=days)
    stmt = (
        select(InsiderTransaction)
        .where(InsiderTransaction.ticker.in_(list(tickers)))
        .where(InsiderTransaction.transaction_date <= as_of)
        .where(InsiderTransaction.transaction_date >= start)
        .order_by(InsiderTransaction.transaction_date.desc())
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for r in session.execute(stmt).scalars().all():
        out[r.ticker].append(
            {
                "person": r.person,
                "role": r.role,
                "code": r.transaction_code,
                "shares": r.shares,
                "price": r.price,
                "value_usd": r.value_usd,
                "date": r.transaction_date,
            }
        )
    return dict(out)
