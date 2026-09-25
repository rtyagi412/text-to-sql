import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

# Compiles the batch and describes the result set it would return: nothing is executed and no rows are read, so
# it says what SQL Server makes of the query (invalid columns, GROUP BY mistakes, type and function errors)
# without touching the data. The SQL travels as a bound parameter, never spliced into the statement.
_DESCRIBE = text("EXEC sp_describe_first_result_set @tsql = :sql, @params = NULL, @browse_information_mode = 0")


@dataclass(frozen=True)
class CompileOutcome:
    columns: list[str] | None = None  # the query's output column names, in order, when SQL Server compiled it
    error: str | None = None  # SQL Server's reason, when it refused the query
    unavailable: str | None = None  # why no answer could be had (server unreachable, no permission, ...)


_SERVER_MESSAGE = re.compile(r"\[SQL Server\](.+?) \(\d+\)")
_DRIVER_TAGS = re.compile(r"\[(?:Microsoft|SQL Server|ODBC Driver[^\]]*|[0-9A-Z]{5})\]")
_BOILERPLATE = "The batch could not be analyzed"
_STATE_PREFIX = re.compile(r"^[\s(]*'?[0-9A-Z]{5}'?\s*,\s*'?")


def _message(exc: DBAPIError) -> str:
    """SQL Server's own sentence(s), without the driver prefix, state codes, the SQL echo or SQLAlchemy's link."""
    raw = str(exc.orig) if exc.orig is not None else str(exc)
    sentences = dict.fromkeys(m.strip() for m in _SERVER_MESSAGE.findall(raw) if not m.startswith(_BOILERPLATE))
    if sentences:
        return "; ".join(sentences)[:500]
    cleaned = _STATE_PREFIX.sub("", _DRIVER_TAGS.sub("", raw.split("(Background on this error")[0]).strip())
    return re.sub(r"\s+", " ", re.sub(r"^\(?'?,?\s*'", "", cleaned)).strip(" ()',\"")[:300]


def describe(source_db: Session, sql: str) -> CompileOutcome:
    """Asks the source database to compile `sql` (already checked to be one plain SELECT). A query the server
    refuses comes back as `error`; a server that cannot be asked comes back as `unavailable`, which is not the
    query's fault."""
    try:
        rows = source_db.execute(_DESCRIBE, {"sql": sql}).mappings().all()
    except ProgrammingError as exc:  # SQLSTATE 42xxx: the query is wrong
        source_db.rollback()
        return CompileOutcome(error=_message(exc))
    except DBAPIError as exc:  # connection, login, timeout
        source_db.rollback()
        return CompileOutcome(unavailable=_message(exc))

    for row in rows:
        if row.get("error_message"):
            return CompileOutcome(error=str(row["error_message"])[:500])
    return CompileOutcome(columns=[row["name"] for row in rows if not row.get("is_hidden")])
