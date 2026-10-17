"""Database access for one-off maintenance scripts (scripts/fix_period_ends.py,
scripts/lookup_report_dates.py).

What these scripts share, and why it lives in one place:

* THE URL IS A SECRET. It comes from a file (`--url-file`) or a named environment
  variable, never from argv, and it is scrubbed from every message that could
  reach a terminal or a log. The app's own `DATABASE_URL` is deliberately NOT a
  fallback: a maintenance script pointed at production by an environment variable
  somebody forgot is exactly the accident this avoids.
* READ-ONLY MEANS THE DATABASE REFUSES, not that the script behaves. On Postgres
  the connection is opened with `default_transaction_read_only=on`, so a write
  fails in the server whatever the script does; on SQLite `PRAGMA query_only=ON`
  does the same. A cursor-level guard additionally rejects anything that is not a
  read before it is sent.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import Engine, create_engine, event

_READ_PREFIXES = ("select", "with", "show", "pragma", "explain")

PG_READONLY_OPTIONS = (
    "-c default_transaction_read_only=on -c statement_timeout=300000 "
    "-c idle_in_transaction_session_timeout=600000"
)


def normalise_url(url: str) -> str:
    """Railway hands out postgres://; name the driver explicitly (see settings.py)."""
    url = url.strip()
    for bare in ("postgres://", "postgresql://"):
        if url.startswith(bare):
            return "postgresql+psycopg2://" + url[len(bare):]
    return url


def read_url(url_file: str | os.PathLike[str] | None, env_names: Iterable[str]) -> str:
    """The URL from `url_file`, else the first non-empty variable in `env_names`."""
    if url_file:
        text = Path(url_file).read_text(encoding="utf-8").strip()
        if not text:
            raise ValueError("the --url-file is empty")
        return normalise_url(text)
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return normalise_url(value)
    raise ValueError(
        "no database URL: pass --url-file, or set " + " / ".join(env_names)
    )


_CRED = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@]*@")


def scrub(message: object, url: str | None = None) -> str:
    """`message` as text with the URL (and any user:password@ in any URL) removed."""
    out = str(message)
    if url:
        out = out.replace(url, "<url>")
        # SQLAlchemy sometimes shows the URL with the password masked or reordered.
        out = out.replace(normalise_url(url), "<url>")
    return _CRED.sub(lambda m: m.group("scheme") + "<credentials>@", out)


_WRITE_WORDS = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum)\b", re.I)


def _guard(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
    """A second line of defence behind the database's own read-only mode: the statement must
    start like a read AND contain no write keyword (a CTE can carry a DELETE; EXPLAIN ANALYZE
    runs what it explains)."""
    if not statement.lstrip().lower().startswith(_READ_PREFIXES) or _WRITE_WORDS.search(statement):
        raise RuntimeError("refused: read-only connection, statement is not a read")


def make_engine(url: str, *, readonly: bool) -> Engine:
    """An engine for `url`. With `readonly=True` the database itself refuses writes."""
    url = normalise_url(url)
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        engine = create_engine(url, **kwargs)
        if readonly:

            @event.listens_for(engine, "connect")
            def _query_only(dbapi_conn, _record):  # noqa: ANN001
                dbapi_conn.execute("PRAGMA query_only = ON")
    else:
        connect_args: dict = {"connect_timeout": 20}
        if readonly:
            connect_args["options"] = PG_READONLY_OPTIONS
        engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args, **kwargs)
    if readonly:
        event.listen(engine, "before_cursor_execute", _guard)
    return engine
