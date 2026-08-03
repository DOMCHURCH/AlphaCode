"""SEC Financial Statement Data Sets -- bulk XBRL facts, one download per quarter.

Replaces the per-CIK companyconcept crawl (~17 concepts x ~5,000 CIKs ~= 85,000
requests at SEC's 10/sec) with ONE ZIP per quarter. Each ZIP holds tab-separated:
  sub.txt  submission metadata: adsh, cik, name, form, period (period end),
           filed (filing date), fp (fiscal period)
  num.txt  every numeric XBRL fact: adsh, tag, ddate (period end), qtrs, uom, value
Joined on `adsh`, that yields as-reported fundamentals AND the filing dates +
period ends that the pead factors need -- so the same download fills the
Fundamental table and the (previously empty) EarningsEvent table.

URL + layout confirmed by reading edgartools
(github.com/dgunning/edgartools: edgar/reference/financials.py names the URL,
edgar/bdc/datasets.py shows the ZIP is read with pandas read_csv(sep='\\t')).

NOTE: this build environment cannot reach sec.gov (proxy-blocked), so the live
download is verified ON THE DEPLOY via the /diagnostics fundamentals panel and
/reconcile. The parser below is exercised against a synthetic num.txt/sub.txt
built to the documented schema.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from collections.abc import Mapping
from typing import Any

import httpx
import pandas as pd
import structlog

from src.config.settings import get_settings
from src.ingest.xbrl import DIMENSION_COLUMNS, is_consolidated

log = structlog.get_logger(__name__)

DATASET_URL = (
    "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{year}q{q}.zip"
)

_EPS_TAGS = frozenset({"EarningsPerShareDiluted"})
# Periodic reports carry an earnings event (filing date + period end).
_PERIODIC_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A"})


def _to_date(v: Any) -> dt.date | None:
    """SEC dataset dates are YYYYMMDD ints/strings."""
    s = str(v).strip()[:8]
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def dataset_url(year: int, quarter: int) -> str:
    tmpl = get_settings().sec_dataset_url or DATASET_URL
    return tmpl.format(year=year, q=quarter)


async def download_dataset(year: int, quarter: int, *, timeout: float = 300.0) -> bytes:
    """Download one quarter's Financial Statement Data Set ZIP (raw bytes).

    SEC requires a descriptive User-Agent; without it the request is 403'd.
    Raises loudly (never returns an empty/HTML body silently) so a moved URL or a
    throttle is a hard failure, not a silent zero-fundamentals load.
    """
    s = get_settings()
    url = dataset_url(year, quarter)
    headers = {"User-Agent": s.sec_user_agent, "Accept-Encoding": "gzip, deflate"}
    log.info("sec_dataset_download_start", url=url)
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
        resp = await c.get(url, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"SEC dataset {year}q{quarter}: HTTP {resp.status_code} for {url}")
    data = resp.content
    if len(data) < 1024 or not zipfile.is_zipfile(io.BytesIO(data)):
        raise RuntimeError(
            f"SEC dataset {year}q{quarter}: not a valid ZIP ({len(data)} bytes) -- "
            f"moved URL or throttled, not empty data."
        )
    log.info("sec_dataset_download_done", bytes=len(data), year=year, quarter=quarter)
    return data


def _read_member(
    zbytes: bytes, name: str, usecols: list[str], optional: tuple[str, ...] = ()
) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        if name not in z.namelist():
            raise RuntimeError(f"SEC dataset ZIP missing {name}; members={z.namelist()[:6]}")
        with z.open(name) as f:
            df = pd.read_csv(f, sep="\t", low_memory=False, dtype=str)
    # Keep only the columns we use, defensively (schema has ~30+ columns).
    have = [c for c in usecols if c in df.columns]
    missing = set(usecols) - set(have)
    if missing:
        raise RuntimeError(f"SEC {name} missing expected columns {sorted(missing)}")
    # Optional columns vary by dataset vintage (`segments` appears ~2021). Carry
    # them when present rather than failing the load.
    have += [c for c in optional if c in df.columns]
    return df[have]


def parse_dataset(zbytes: bytes) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (sub, num) DataFrames from a Financial Statement Data Set ZIP.

    num.txt MUST carry the dimensional columns. Reading only
    [adsh, tag, ddate, qtrs, uom, value] -- as this did until the rebuild -- makes
    it impossible to tell a consolidated fact from a segment or equity-component
    one, which is precisely how JPM's EMEA assets became JPM's total assets.
    """
    if not zipfile.is_zipfile(io.BytesIO(zbytes)):
        raise RuntimeError(
            f"SEC dataset is not a valid ZIP ({len(zbytes)} bytes) -- moved URL, "
            f"throttle, or an HTML error page."
        )
    sub = _read_member(zbytes, "sub.txt", ["adsh", "cik", "form", "period", "filed", "fp"])
    num = _read_member(
        zbytes,
        "num.txt",
        ["adsh", "tag", "ddate", "qtrs", "uom", "value"],
        optional=DIMENSION_COLUMNS,
    )
    if not any(c in num.columns for c in DIMENSION_COLUMNS):
        raise RuntimeError(
            f"SEC num.txt carries none of {list(DIMENSION_COLUMNS)}; dimensional "
            f"facts cannot be distinguished from consolidated ones."
        )
    return sub, num


def extract_earnings(
    sub: pd.DataFrame, num: pd.DataFrame, cik_to_ticker: Mapping[str, str]
) -> list[dict[str, Any]]:
    """One EarningsEvent per periodic filing: report_date = filed, period_end =
    period, actual_eps = the diluted-EPS fact for that filing (if present).

    Populating this is what makes pead_window computable (days since the last
    report). consensus_eps / surprise stay None -- consensus needs a paid estimates
    feed; we never fabricate it. gap_pct is filled later from stored bars.
    """
    if sub.empty:
        return []
    periodic = sub[sub["form"].isin(_PERIODIC_FORMS)].copy()
    if periodic.empty:
        return []
    # Diluted EPS fact per filing. Same dimensional discipline as the fundamentals
    # extractor: EPS is also reported per segment and per class of stock, so
    # taking the first row would pick an arbitrary one of those.
    eps_by_adsh: dict[str, float] = {}
    if not num.empty:
        eps = num[num["tag"].isin(_EPS_TAGS)]
        for r in eps.itertuples(index=False):
            if not is_consolidated(r):
                continue
            try:
                eps_by_adsh.setdefault(r.adsh, float(r.value))
            except (TypeError, ValueError):
                continue
    out: list[dict[str, Any]] = []
    for r in periodic.itertuples(index=False):
        cik = str(r.cik).lstrip("0") or "0"
        ticker = cik_to_ticker.get(cik) or cik_to_ticker.get(str(r.cik))
        if not ticker:
            continue
        report_date = _to_date(r.filed)
        period_end = _to_date(r.period)
        if report_date is None:
            continue
        out.append(
            {
                "ticker": ticker,
                "report_date": report_date,
                "period_end": period_end,
                "actual_eps": eps_by_adsh.get(r.adsh),
                "consensus_eps": None,  # needs a paid estimates feed; never faked
                "surprise_pct": None,
                "gap_pct": None,  # filled from bars in the backfill
                "is_future": False,
            }
        )
    return out


def recent_quarters(as_of: dt.date | None = None, n: int = 8) -> list[tuple[int, int]]:
    """The last `n` published quarters (year, quarter), newest first. A quarter's
    dataset publishes a few weeks after quarter-end, so we skip the current one."""
    as_of = as_of or dt.date.today()
    q = (as_of.month - 1) // 3 + 1
    year = as_of.year
    # step back one quarter (current quarter isn't published yet)
    out: list[tuple[int, int]] = []
    q -= 1
    if q == 0:
        q, year = 4, year - 1
    for _ in range(n):
        out.append((year, q))
        q -= 1
        if q == 0:
            q, year = 4, year - 1
    return out
