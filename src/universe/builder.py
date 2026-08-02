"""Stage 0 -- universe construction.

One Polygon grouped-daily call gives every US ticker's OHLCV. Join reference
data (security type, exchange) and the FMP screener (market cap, sector), then
apply the tradeable filters.

Survivorship bias: the snapshot is persisted every single day. Backtests
reconstruct the universe as it existed on that date, including names that have
since died. Screening today's live tickers against 2023 prices is a fantasy.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd
import structlog
from sqlalchemy import func, select

from src.config.settings import get_settings
from src.ingest import fmp, polygon, sec_edgar
from src.ingest.polygon import VALID_EXCHANGES
from src.storage import repository
from src.storage.models import DailyBar
from src.storage.pit import get_price_panel

log = structlog.get_logger(__name__)

# Warrants (W), units (U), rights (R) get suffixed on both exchanges. Also catch
# the dotted class forms and any share-class letter appended after a dot.
_SUFFIX_RE = re.compile(r"(\.(W[STI]?|U|R|P[A-Z]?)$)|([.-](WS|UN|RT)$)")

EXCLUDED_TYPES = {
    "ETF", "ETN", "ETV", "ETS", "FUND", "SP", "WARRANT", "RIGHT", "UNIT",
    "PFD", "BOND", "INDEX", "BASKET", "LT",
}


def is_common_stock(ticker: str, security_type: str | None) -> bool:
    """Common stock only. Excludes ETFs, ETNs, warrants, units, rights, preferreds."""
    if not ticker:
        return False
    if security_type is not None and security_type.upper() != "CS":
        return False
    if _SUFFIX_RE.search(ticker):
        return False
    # Five-character NASDAQ tickers ending W/U/R are warrants/units/rights.
    if len(ticker) == 5 and ticker[-1] in {"W", "U", "R"} and ticker.isalpha():
        return False
    return True


def compute_adv(panel_close: pd.DataFrame, panel_volume: pd.DataFrame,
                window: int = 20) -> pd.Series:
    """20-day average dollar volume, per ticker."""
    if panel_close.empty or panel_volume.empty:
        return pd.Series(dtype=float)
    dollar = panel_close * panel_volume
    return dollar.tail(window).mean(axis=0)


def apply_filters(
    df: pd.DataFrame,
    *,
    min_price: float,
    min_dollar_volume: float,
    min_market_cap: float,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Apply the tradeable-universe filters, returning survivors + reject counts."""
    rejects: dict[str, int] = {}
    n0 = len(df)

    def _cut(mask: pd.Series, label: str, frame: pd.DataFrame) -> pd.DataFrame:
        kept = frame[mask]
        rejects[label] = len(frame) - len(kept)
        return kept

    out = df
    out = _cut(out["ticker"].notna() & (out["ticker"].str.len() > 0), "no_ticker", out)
    out = _cut(
        out.apply(
            lambda r: is_common_stock(r["ticker"], r.get("security_type")), axis=1
        ),
        "not_common_stock",
        out,
    )
    out = _cut(
        out["exchange"].isin(VALID_EXCHANGES) | out["exchange"].isna(),
        "bad_exchange",
        out,
    )
    out = _cut(out["close"].fillna(0) >= min_price, "price_below_min", out)
    out = _cut(
        out["adv_20d"].fillna(0) >= min_dollar_volume, "dollar_volume_below_min", out
    )
    out = _cut(
        out["market_cap"].fillna(0) >= min_market_cap, "market_cap_below_min", out
    )
    out = _cut(~out["delisted"].fillna(False), "delisted_or_halted", out)

    log.info(
        "universe_filters", entry=n0, exit=len(out), rejects=rejects
    )
    return out.reset_index(drop=True), rejects


async def build_universe(
    as_of: dt.date,
    session,
    *,
    persist_bars: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Stage 0. Returns (universe_df, diagnostics).

    Two sourcing modes. With a Polygon key, one grouped-daily call gives the
    whole market's OHLCV plus reference/screener data. Without one, the funnel
    runs on free data: the universe is seeded from SEC's company list and the
    latest bars come from Yahoo (on top of whatever the backfill already stored).
    """
    s = get_settings()

    if s.polygon_api_key:
        bars = await polygon.fetch_grouped_daily(as_of)
        if not bars:
            raise RuntimeError(
                f"Polygon grouped-daily returned no rows for {as_of} -- market holiday?"
            )
        if persist_bars:
            repository.save_bars(session, bars)
            session.flush()
        reference = await polygon.fetch_ticker_reference()
        screener = await fmp.fetch_screener() if s.fmp_api_key else []
        return build_universe_from_frames(
            as_of, session, bars, reference, screener, settings=s
        )

    # ---- free-data mode: SEC seed + stored (Stooq-bulk) bars, no keys -----
    # The wide-end price load is the keyless backfill's job (Stooq bulk daily,
    # one download). We do NOT per-ticker loop here -- we read the latest stored
    # session. If it's stale, the fix is to re-run the bulk backfill, not to
    # hammer a per-ticker API from a datacenter IP.
    reference = await sec_edgar.fetch_company_tickers()
    bars = _latest_stored_bars(session, as_of)
    if not bars:
        raise RuntimeError(
            "No stored bars to build a universe from. Run a backfill first "
            "(the site's button does this automatically)."
        )
    # No free market-cap source -> screener carries no cap (the gate relaxes),
    # but sectors come from the cached SIC->GICS map so Stage-2 stays
    # sector-neutral instead of universe-neutral. Names not yet mapped keep a
    # null sector (honest) until the sector backfill reaches them.
    sector_by_ticker = repository.get_sector_map(session)
    screener = [
        {"ticker": t, "market_cap": np.nan, "sector": sector_by_ticker.get(t),
         "industry": None}
        for t in sector_by_ticker
    ]
    return build_universe_from_frames(
        as_of, session, bars, reference, screener, settings=s
    )


def _latest_stored_bars(session, as_of: dt.date) -> list[dict[str, Any]]:
    """The most recent stored session on/before as_of, in grouped-daily shape.

    Free-data mode has no single "whole market for today" call, so we synthesise
    it from storage: take the latest available trading day and return its bars.
    """
    latest = session.execute(
        select(func.max(DailyBar.date)).where(DailyBar.date <= as_of)
    ).scalar_one_or_none()
    if latest is None:
        return []
    rows = session.execute(
        select(DailyBar).where(DailyBar.date == latest)
    ).scalars()
    return [
        {
            "ticker": b.ticker, "date": b.date, "open": b.open, "high": b.high,
            "low": b.low, "close": b.close, "volume": b.volume, "vwap": b.vwap,
        }
        for b in rows
    ]


def build_universe_from_frames(
    as_of: dt.date,
    session,
    bars: Sequence[dict[str, Any]],
    reference: Sequence[dict[str, Any]],
    screener: Sequence[dict[str, Any]],
    *,
    settings=None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Pure join + filter step, separated so it is testable without network."""
    s = settings or get_settings()

    bars_df = pd.DataFrame(list(bars))
    ref_df = pd.DataFrame(list(reference))
    scr_df = pd.DataFrame(list(screener))

    if bars_df.empty:
        return pd.DataFrame(), {"error": "no bars"}

    df = bars_df[["ticker", "close", "volume"]].copy()

    if not ref_df.empty:
        df = df.merge(
            ref_df[["ticker", "name", "security_type", "exchange", "cik", "active"]],
            on="ticker",
            how="left",
        )
    else:
        for col in ("name", "security_type", "exchange", "cik"):
            df[col] = None
        df["active"] = True

    if not scr_df.empty:
        df = df.merge(
            scr_df[["ticker", "market_cap", "sector", "industry"]].drop_duplicates(
                "ticker"
            ),
            on="ticker",
            how="left",
        )
    else:
        for col in ("market_cap", "sector", "industry"):
            df[col] = np.nan

    df["delisted"] = ~df.get("active", pd.Series(True, index=df.index)).fillna(True)

    # 20-day ADV from stored history; fall back to today's dollar volume if the
    # backfill has not run yet.
    start = as_of - dt.timedelta(days=45)
    close_panel = get_price_panel(session, df["ticker"].tolist(), start, as_of, "close")
    vol_panel = get_price_panel(session, df["ticker"].tolist(), start, as_of, "volume")
    adv = compute_adv(close_panel, vol_panel)
    df["adv_20d"] = df["ticker"].map(adv).astype(float)
    fallback = df["close"].fillna(0) * df["volume"].fillna(0)
    df["adv_20d"] = df["adv_20d"].fillna(fallback)

    # Without a screener (free-data mode) there is no market cap to gate on, so
    # relax that one filter and lean on price + dollar-volume liquidity instead.
    has_caps = "market_cap" in df.columns and df["market_cap"].notna().any()
    survivors, rejects = apply_filters(
        df,
        min_price=s.min_price,
        min_dollar_volume=s.min_dollar_volume,
        min_market_cap=s.min_market_cap if has_caps else 0.0,
    )

    rows = [
        {
            "ticker": r["ticker"],
            "name": r.get("name"),
            "exchange": r.get("exchange"),
            "security_type": r.get("security_type"),
            "sector": r.get("sector") if pd.notna(r.get("sector")) else None,
            "industry": r.get("industry") if pd.notna(r.get("industry")) else None,
            "cik": str(r["cik"]) if pd.notna(r.get("cik")) else None,
            "market_cap": _f(r.get("market_cap")),
            "close": _f(r.get("close")),
            "adv_20d": _f(r.get("adv_20d")),
        }
        for _, r in survivors.iterrows()
    ]
    repository.save_universe(session, as_of, rows)

    diagnostics = {
        "raw_tickers": len(bars_df),
        "universe_size": len(survivors),
        "rejects": rejects,
        "sector_coverage": float(survivors["sector"].notna().mean())
        if len(survivors)
        else 0.0,
    }
    log.info("universe_built", as_of=str(as_of), **{
        k: v for k, v in diagnostics.items() if k != "rejects"
    })
    return survivors, diagnostics


def _f(v: Any) -> float | None:
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None
