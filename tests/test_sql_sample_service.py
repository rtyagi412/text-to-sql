from sqlalchemy.exc import DataError, OperationalError, ProgrammingError

from app.services.sql_sample_service import run_sample, sample_sql
from tests.fakes import FakeResult, FakeSession

SQL = "SELECT a.x AS x FROM d.a AS a"


def test_sample_sql_adds_top():
    assert sample_sql(SQL, 10) == "SELECT TOP 10 a.x AS x FROM d.a AS a"


def test_sample_sql_keeps_the_smaller_top():
    assert sample_sql("SELECT TOP 3 a.x AS x FROM d.a AS a", 10).startswith("SELECT TOP 3 ")
    assert sample_sql("SELECT TOP 500 a.x AS x FROM d.a AS a ORDER BY a.x", 10).startswith("SELECT TOP 10 ")


def test_sample_sql_with_distinct_and_order_by():
    assert sample_sql("SELECT DISTINCT a.x AS x FROM d.a AS a ORDER BY a.x", 5) == (
        "SELECT DISTINCT TOP 5 a.x AS x FROM d.a AS a ORDER BY a.x"
    )


def test_sample_sql_leaves_percent_and_offset_alone():
    percent = "SELECT TOP 50 PERCENT a.x AS x FROM d.a AS a"
    assert "PERCENT" in sample_sql(percent, 10)
    offset = "SELECT a.x AS x FROM d.a AS a ORDER BY a.x OFFSET 0 ROWS FETCH NEXT 500 ROWS ONLY"
    assert "TOP" not in sample_sql(offset, 10)


def _error(cls, message):
    return cls("SELECT", None, Exception(message))


def test_success_returns_rows_and_restores_the_connection():
    session = FakeSession(lambda sql: None if sql.startswith("SET") else FakeResult(["x", "b"], [(1, b"\x01"), (2, None)]))
    outcome = run_sample(session, SQL, 10, 7)
    assert outcome.executed
    assert outcome.rows == [{"x": 1, "b": "0x01"}, {"x": 2, "b": None}]
    conn = session.conn
    assert conn.executed[1] == "SELECT TOP 10 a.x AS x FROM d.a AS a"
    assert conn.timeout_during_query == 7
    assert conn.driver.timeout == 0
    assert conn.executed[-1] == "SET LOCK_TIMEOUT -1"
    assert session.rollbacks == 1


def test_runtime_error_is_the_querys_fault():
    message = "[22018] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Conversion failed when converting the varchar value 'abc' to data type int. (245) (SQLExecDirectW)"
    session = FakeSession(lambda sql: None if sql.startswith("SET") else _error(DataError, message))
    outcome = run_sample(session, SQL, 10, 7)
    assert not outcome.executed
    assert outcome.error == "Conversion failed when converting the varchar value 'abc' to data type int."
    assert session.conn.executed[-1] == "SET LOCK_TIMEOUT -1"
    assert session.rollbacks == 1


def test_timeout_is_not_an_error():
    session = FakeSession(lambda sql: None if sql.startswith("SET") else _error(OperationalError, "('HYT00', '[HYT00] Query timeout expired (0)')"))
    outcome = run_sample(session, SQL, 10, 7)
    assert outcome.timed_out and outcome.error is None


def test_permission_denied_is_unavailable():
    message = "[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]The SELECT permission was denied on the object 'a'. (229)"
    session = FakeSession(lambda sql: None if sql.startswith("SET") else _error(ProgrammingError, message))
    outcome = run_sample(session, SQL, 10, 7)
    assert outcome.unavailable and outcome.error is None


def test_connection_that_cannot_be_restored_is_discarded():
    def handler(sql):
        if sql == "SET LOCK_TIMEOUT -1":
            return _error(OperationalError, "Communication link failure")
        return None if sql.startswith("SET") else FakeResult(["x"], [(1,)])

    session = FakeSession(handler)
    run_sample(session, SQL, 10, 7)
    assert session.conn.invalidated
