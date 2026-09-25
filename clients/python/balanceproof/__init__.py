"""BalanceProof: SEC fundamentals that prove they add up.

    >>> import balanceproof as bp
    >>> client = bp.Client("YOUR_KEY")            # or set BALANCEPROOF_API_KEY
    >>> client.balance_sheet("AAPL")
    >>> client.statements("AAPL", period="quarterly")
    >>> client.balance_sheet("AAPL", as_of="2025-03-01")   # point-in-time (Pro+)
    >>> client.panel(["AAPL", "MSFT"], ["revenue", "net_income"])  # pandas

Standard library only; pandas is needed just for `panel()` and `to_frame()`.
A free key is at https://balanceproof.dev/dashboard.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

__version__ = "0.1.0"
__all__ = ["Client", "BalanceProofError", "PlanRequired"]

DEFAULT_BASE_URL = "https://balanceproof.dev"


class BalanceProofError(Exception):
    """The API answered with an error. `status` is the HTTP code."""

    def __init__(self, status: int, detail: Any):
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")


class PlanRequired(BalanceProofError):
    """The feature needs a higher plan; `required_plan` names it."""

    @property
    def required_plan(self) -> str | None:
        return self.detail.get("required_plan") if isinstance(self.detail, dict) else None


def _date(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()[:10]
    return str(v)[:10]


class Client:
    """One API key against one base URL. Every method is one metered call."""

    def __init__(self, api_key: str | None = None, *, base_url: str | None = None,
                 timeout: float = 60.0):
        self.api_key = api_key or os.environ.get("BALANCEPROOF_API_KEY", "")
        self.base_url = (base_url or os.environ.get("BALANCEPROOF_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout

    # -- transport ------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                 body: Any = None, raw: bool = False) -> Any:
        query = {k: v for k, v in (params or {}).items() if v is not None}
        url = self.base_url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Accept": "application/json",
                   "User-Agent": f"balanceproof-python/{__version__}"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                payload = res.read()
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(text).get("detail", text)
            except ValueError:
                detail = text
            if exc.code == 403 and isinstance(detail, dict) and detail.get("error") == "plan_required":
                raise PlanRequired(exc.code, detail) from None
            raise BalanceProofError(exc.code, detail) from None
        if raw:
            return payload.decode("utf-8")
        return json.loads(payload)

    # -- endpoints ------------------------------------------------------------

    def balance_sheet(self, ticker: str, as_of: Any = None) -> dict[str, Any]:
        """The latest filed balance sheet, or as public on `as_of` (Pro+)."""
        return self._request("GET", f"/api/company/{urllib.parse.quote(ticker)}",
                             {"as_of": _date(as_of)})

    def history(self, ticker: str, years: int | None = None, as_of: Any = None) -> dict[str, Any]:
        """Every filed balance sheet, newest first. Depth is capped by plan."""
        return self._request("GET", f"/api/company/{urllib.parse.quote(ticker)}/history",
                             {"years": years, "as_of": _date(as_of)})

    def statements(self, ticker: str, period: str = "annual", years: int | None = None,
                   as_of: Any = None) -> dict[str, Any]:
        """Income statement and cash flow per period, each with its checks."""
        return self._request("GET", f"/api/company/{urllib.parse.quote(ticker)}/statements",
                             {"period": period, "years": years, "as_of": _date(as_of)})

    def changes(self, ticker: str) -> dict[str, Any]:
        """What moved since the previous period, and what was restated (Starter+)."""
        return self._request("GET", f"/api/company/{urllib.parse.quote(ticker)}/changes")

    def verify(self, tickers: Iterable[str]) -> dict[str, Any]:
        """Check many balance sheets at once (Pro: 50, Business: 500)."""
        return self._request("POST", "/api/verify", body={"tickers": list(tickers)})

    def exceptions(self, since: Any = None, until: Any = None, type: str | None = None,  # noqa: A002
                   ticker: str | None = None, filer_only: bool = False,
                   limit: int = 500, offset: int = 0) -> dict[str, Any]:
        """Failed checks and restatements across all companies (Pro: 90 days)."""
        params = {"since": _date(since), "until": _date(until), "type": type,
                  "ticker": ticker, "limit": limit, "offset": offset}
        if filer_only:
            params.update(type="failed_check", attribution="filer")
        return self._request("GET", "/api/exceptions", params)

    def exceptions_csv(self, **kwargs: Any) -> str:
        """The same feed as CSV text."""
        params = {"since": _date(kwargs.get("since")), "until": _date(kwargs.get("until")),
                  "type": kwargs.get("type"), "ticker": kwargs.get("ticker"),
                  "limit": kwargs.get("limit", 5000), "format": "csv"}
        return self._request("GET", "/api/exceptions", params, raw=True)

    def status(self) -> dict[str, Any]:
        """Your plan and how many calls are left this month. Free of charge."""
        return self._request("GET", "/api/user/status")

    # -- pandas ---------------------------------------------------------------

    def panel(self, tickers: Iterable[str], fields: Iterable[str] | None = None,
              period: str = "annual", years: int | None = None, as_of: Any = None):
        """A long pandas DataFrame: one row per (ticker, period), one column per field.

        `fields` are statement metrics ("revenue", "net_income",
        "operating_cash_flow", ...); None keeps them all. Each row also carries
        `filing_date`, `fiscal_period`, `checks_failed` and `derived`, so a
        backtest can drop derived or failed rows. One call per ticker.
        """
        pd = _pandas()
        wanted = list(fields) if fields is not None else None
        rows: list[dict[str, Any]] = []
        for t in tickers:
            try:
                body = self.statements(t, period=period, years=years, as_of=as_of)
            except BalanceProofError as exc:
                if exc.status == 404:
                    continue
                raise
            for p in body["periods"]:
                values = {**p["income_statement"], **p["cash_flow"]}
                row = {"ticker": body["ticker"], "period_end": p["period_end"],
                       "fiscal_period": p["fiscal_period"], "filing_date": p["filing_date"]}
                row.update({k: v for k, v in values.items() if wanted is None or k in wanted})
                row["checks_failed"] = [c["check"] for c in p["checks"] if c["status"] == "failed"]
                row["derived"] = p["derived"]
                rows.append(row)
        df = pd.DataFrame(rows)
        for col in ("period_end", "filing_date"):
            if col in df:
                df[col] = pd.to_datetime(df[col])
        return df

    @staticmethod
    def to_frame(payload: dict[str, Any]):
        """Any list-bearing response (history, statements, exceptions) as a DataFrame."""
        pd = _pandas()
        for key in ("events", "periods", "balance_sheets", "results"):
            if key in payload:
                return pd.json_normalize(payload[key])
        return pd.json_normalize(payload)


def _pandas():
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("panel() and to_frame() need pandas: pip install pandas") from exc
    return pd
