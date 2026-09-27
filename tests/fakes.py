"""Stand-ins for a SQLAlchemy Session on SQL Server, recording what is executed on the connection."""

from collections.abc import Callable
from typing import Any


class FakeDriver:
    def __init__(self) -> None:
        self.timeout = 0


class FakeResult:
    def __init__(self, columns: list[str], rows: list[tuple]) -> None:
        self._columns, self._rows = columns, rows

    def keys(self) -> list[str]:
        return self._columns

    def fetchmany(self, n: int) -> list[tuple]:
        return self._rows[:n]

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


class FakeConnection:
    def __init__(self, handler: Callable[[str], Any]) -> None:
        self.handler = handler
        self.executed: list[str] = []
        self.driver = FakeDriver()
        self.connection = type("Fairy", (), {"driver_connection": self.driver})()
        self.invalidated = False
        self.timeout_during_query: int | None = None

    def exec_driver_sql(self, sql: str) -> Any:
        self.executed.append(sql)
        if not sql.startswith("SET "):
            self.timeout_during_query = self.driver.timeout
        result = self.handler(sql)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else FakeResult([], [])

    def invalidate(self) -> None:
        self.invalidated = True


class FakeSession:
    def __init__(self, handler: Callable[[str], Any]) -> None:
        self.conn = FakeConnection(handler)
        self.rollbacks = 0

    def connection(self) -> FakeConnection:
        return self.conn

    def rollback(self) -> None:
        self.rollbacks += 1
