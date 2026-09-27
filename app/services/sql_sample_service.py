"""Runs a query for a handful of rows on the source database, to prove it executes.

Compiling (sql_compile_service) catches what SQL Server can see without data: bad columns, GROUP BY mistakes, type
errors in the text. Some errors only appear once rows flow: a string that will not convert to a number, a divide by
zero, a subquery that returns more than one value, an arithmetic overflow. Running the query capped at a few rows,
under a query timeout and a lock timeout, and rolling back afterwards, surfaces those without reading the table."""

import time
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlalchemy import Connection
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session
from sqlglot import exp

from app.services.sql_compile_service import server_message

_DIALECT = "tsql"
_LOCK_TIMEOUT_MS = 5000  # a sample that would wait on another session's locks gives up instead of blocking
_TIMEOUT_MARKERS = ("HYT00", "timeout expired", "Query timeout", "Lock request time out", "(1222)")
_NOT_THE_QUERYS_FAULT = ("permission was denied", "Login failed", "Communication link failure")


@dataclass(frozen=True)
class SampleOutcome:
    row_limit: int
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: int = 0
    error: str | None = None  # SQL Server failed the query while running it: the query's fault
    timed_out: bool = False  # the capped query did not finish within the timeout (or waited too long on a lock)
    unavailable: str | None = None  # the server could not be asked: not the query's fault

    @property
    def executed(self) -> bool:
        return self.error is None and not self.timed_out and self.unavailable is None


def sample_sql(sql: str, row_limit: int) -> str:
    """`sql` with its output capped at `row_limit` rows by a TOP on the outer SELECT. An existing plain TOP n keeps the
    smaller of the two. A TOP ... PERCENT / WITH TIES or an OFFSET ... FETCH is left as written: the caller fetches at
    most `row_limit` rows anyway."""
    tree = sqlglot.parse_one(sql, read=_DIALECT)
    limit = tree.args.get("limit")
    if limit is None and tree.args.get("offset") is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
    elif isinstance(limit, exp.Limit) and not limit.args.get("limit_options"):
        current = limit.expression
        if isinstance(current, exp.Paren):
            current = current.this
        if isinstance(current, exp.Literal) and not current.is_string and int(current.name) > row_limit:
            limit.set("expression", exp.Literal.number(row_limit))
    return tree.sql(dialect=_DIALECT)


def _json_safe(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex().upper()
    return value


def _dbapi_connection(source_db: Session) -> Any:
    fairy = source_db.connection().connection
    return getattr(fairy, "driver_connection", None) or getattr(fairy, "dbapi_connection", None)


def run_sample(source_db: Session, sql: str, row_limit: int, timeout_seconds: int) -> SampleOutcome:
    """Executes `sql` (already checked to be one plain, read-only SELECT) capped at `row_limit` rows, then rolls back.
    The pyodbc query timeout and SQL Server's LOCK_TIMEOUT are set for the run and restored afterwards, since the
    connection goes back to the pool."""
    limited = sample_sql(sql, row_limit)
    connection = source_db.connection()
    driver = _dbapi_connection(source_db)
    previous_timeout = getattr(driver, "timeout", None)
    started = time.perf_counter()
    try:
        if previous_timeout is not None:
            driver.timeout = timeout_seconds
        connection.exec_driver_sql(f"SET LOCK_TIMEOUT {_LOCK_TIMEOUT_MS}")
        result = connection.exec_driver_sql(limited)
        columns = list(result.keys())
        fetched = result.fetchmany(row_limit)
        result.close()
        outcome = SampleOutcome(
            row_limit=row_limit,
            columns=columns,
            rows=[{name: _json_safe(value) for name, value in zip(columns, row, strict=False)} for row in fetched],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    except DBAPIError as exc:
        outcome = _failed(exc, row_limit, int((time.perf_counter() - started) * 1000))
    finally:
        _restore(connection, driver, previous_timeout)
        source_db.rollback()
    return outcome


def _failed(exc: DBAPIError, row_limit: int, elapsed_ms: int) -> SampleOutcome:
    """Sorts a failure into the query's fault (`error`), too slow (`timed_out`) or the server's (`unavailable`)."""
    raw = str(exc.orig) if exc.orig is not None else str(exc)
    if any(marker in raw for marker in _TIMEOUT_MARKERS):
        return SampleOutcome(row_limit=row_limit, elapsed_ms=elapsed_ms, timed_out=True)
    if isinstance(exc, OperationalError) or any(marker in raw for marker in _NOT_THE_QUERYS_FAULT):
        return SampleOutcome(row_limit=row_limit, elapsed_ms=elapsed_ms, unavailable=server_message(exc))
    return SampleOutcome(row_limit=row_limit, elapsed_ms=elapsed_ms, error=server_message(exc))


def _restore(connection: Connection, driver: Any, previous_timeout: Any) -> None:
    """Puts the pooled connection back as it was. One whose settings cannot be restored is discarded from the pool
    rather than handed, with a short lock timeout, to the next request."""
    try:
        if previous_timeout is not None:
            driver.timeout = previous_timeout
        connection.exec_driver_sql("SET LOCK_TIMEOUT -1")
    except Exception:
        connection.invalidate()
