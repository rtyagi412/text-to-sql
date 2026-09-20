from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

_DIALECT = "tsql"

_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like)
_OPERAND_KEYS = ("this", "expression", "low", "high")


class SqlRejectedError(ValueError):
    """The SQL is not a single, plain, read-only SELECT over tables and columns that exist."""


@dataclass(frozen=True)
class CheckedSql:
    sql: str  # formatted
    tables: list[str]  # "schema.table", in order of first appearance
    select_aliases: list[str]  # the report's column headers, in order
    filter_columns: set[tuple[str, str]]  # (lower-case "schema.table", column) used in a WHERE or JOIN condition
    warnings: list[str]  # performance smells; they do not make the SQL wrong


def _parse(sql: str) -> exp.Select:
    try:
        # A trailing semicolon parses as an extra empty statement.
        statements = [s for s in sqlglot.parse(sql, read=_DIALECT) if s is not None]
    except ParseError as exc:
        raise SqlRejectedError(f"not valid T-SQL: {str(exc).splitlines()[0]}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise SqlRejectedError("must be exactly one SELECT statement")
    tree = statements[0]

    if tree.find(exp.With):
        raise SqlRejectedError("CTEs are not allowed; write one flat SELECT")
    if tree.find(exp.Into):
        raise SqlRejectedError("SELECT ... INTO writes data and is not allowed")
    for select in tree.find_all(exp.Select):
        for item in select.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                raise SqlRejectedError("SELECT * is not allowed; list the columns")
    return tree


def _tables(tree: exp.Select) -> list[str]:
    keys: list[str] = []
    for table in tree.find_all(exp.Table):
        if not table.db:
            raise SqlRejectedError(f"table '{table.name}' is not schema-qualified")
        key = f"{table.db}.{table.name}"
        if key not in keys:
            keys.append(key)
    return keys


def referenced_tables(sql: str) -> list[str]:
    """The "schema.table" tables the SQL reads. Raises SqlRejectedError when the SQL isn't a plain SELECT."""
    return _tables(_parse(sql))


def _sargability_warnings(tree: exp.Select) -> list[str]:
    """A predicate that wraps a column in a function or arithmetic makes an index on that column unusable."""
    warnings = []
    for comparison in tree.find_all(*_COMPARISONS):
        for key in _OPERAND_KEYS:
            operand = comparison.args.get(key)
            while isinstance(operand, exp.Paren):
                operand = operand.this
            if not isinstance(operand, exp.Expression) or isinstance(operand, (exp.Column, exp.Literal, exp.Subquery)):
                continue
            if operand.find(exp.Column) and not operand.find(exp.Select):
                snippet = comparison.sql(dialect=_DIALECT)
                warnings.append(
                    f"predicate `{snippet[:90]}` applies an expression to a column, so an index on it cannot be "
                    "used for a seek; compare the bare column to a computed constant instead"
                )
                break
    return warnings


def _warnings(tree: exp.Select) -> list[str]:
    warnings = _sargability_warnings(tree)
    for like in tree.find_all(exp.Like):
        pattern = like.args.get("expression")
        if isinstance(pattern, exp.Literal) and pattern.is_string and pattern.name.startswith("%"):
            warnings.append(
                f"`{like.sql(dialect=_DIALECT)[:90]}` starts with a wildcard, so it scans instead of seeking; "
                "unavoidable for a 'contains' / 'ends with' condition"
            )
    if any(select.args.get("distinct") for select in tree.find_all(exp.Select)):
        warnings.append(
            "SELECT DISTINCT adds a sort or hash step; check that a join is not duplicating rows and that an "
            "EXISTS would not do"
        )
    return warnings


def check_sql(sql: str, columns: dict[str, dict[str, str]]) -> CheckedSql:
    """Parses, validates against the schema and formats. `columns` maps "schema.table" to {column: data type}
    for every table the SQL may use. Rejects SQL that isn't one plain SELECT, reads a table or column that is not
    in `columns`, or would need a guess to resolve (an ambiguous column)."""
    tree = _parse(sql)
    tables = _tables(tree)
    for key in tables:
        if key not in columns:
            raise SqlRejectedError(f"table '{key}' is not in the schema")

    nested: dict[str, dict[str, dict[str, str]]] = {}
    for key, cols in columns.items():
        schema_name, table_name = key.split(".", 1)
        nested.setdefault(schema_name, {})[table_name] = cols
    try:
        # Qualification resolves every column to the alias of the table it belongs to. The formatted SQL is
        # taken from the original tree, so it stays as written.
        qualified = qualify(tree.copy(), schema=nested, dialect=_DIALECT, validate_qualify_columns=True)
    except OptimizeError as exc:
        raise SqlRejectedError(str(exc)) from exc

    # An alias reused for different tables in different scopes maps to all of them: lenient, never wrong.
    tables_by_alias: dict[str, set[str]] = {}
    for table in qualified.find_all(exp.Table):
        tables_by_alias.setdefault(table.alias_or_name.lower(), set()).add(f"{table.db}.{table.name}".lower())
    filter_columns = {
        (table_key, column.name.lower())
        for node in (*qualified.find_all(exp.Where), *qualified.find_all(exp.Join))
        for column in node.find_all(exp.Column)
        for table_key in tables_by_alias.get(column.table.lower(), ())
    }
    return CheckedSql(
        sql=tree.sql(dialect=_DIALECT, pretty=True, identify=True),
        tables=tables,
        select_aliases=[item.alias_or_name for item in tree.expressions],
        filter_columns=filter_columns,
        warnings=_warnings(tree),
    )
