from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

_DIALECT = "tsql"

# Functions that read from another server or a file, beyond the tables the caller named.
_EXTERNAL_DATA_FUNCTIONS = {"OPENROWSET", "OPENQUERY", "OPENDATASOURCE", "OPENXML"}

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
    unjoined: list[str]  # tables joined with no join condition (comma or CROSS JOIN)


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
    for function in tree.find_all(exp.Anonymous):
        if function.name.upper() in _EXTERNAL_DATA_FUNCTIONS:
            raise SqlRejectedError(f"{function.name.upper()} reads outside the database and is not allowed")
    for select in tree.find_all(exp.Select):
        for item in select.expressions:
            if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star)):
                raise SqlRejectedError("SELECT * is not allowed; list the columns")
    return tree


def _canonical_table_names(tree: exp.Select, columns: dict[str, dict[str, str]]) -> None:
    """Rewrites each table reference to the catalog's spelling. SQL Server compares object names without regard to
    case by default, so [core].[account] is the same table as core.Account; the formatted SQL then uses one spelling."""
    canonical = {key.lower(): key for key in columns}
    for table in tree.find_all(exp.Table):
        key = canonical.get(f"{table.db}.{table.name}".lower()) if table.db else None
        if key:
            schema_name, table_name = key.split(".", 1)
            table.this.set("this", table_name)
            table.args["db"].set("this", schema_name)


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


def _output_names(tree: exp.Select) -> list[str]:
    # Only an alias or a bare column names its output; sqlglot would call COUNT(*) "*", but SQL Server leaves it unnamed.
    names = [item.alias if isinstance(item, exp.Alias) else item.name if isinstance(item, exp.Column) else "" for item in tree.expressions]
    for position, name in enumerate(names, start=1):
        if not name:
            raise SqlRejectedError(f"output column {position} has no name; give it an alias")
    duplicates = sorted({n for n in names if [m.lower() for m in names].count(n.lower()) > 1})
    if duplicates:
        raise SqlRejectedError(f"output column names must be unique: {', '.join(duplicates)}")
    return names


def output_columns(sql: str) -> list[str]:
    """The names of the SELECT's output columns, in order: what a report built on it shows. Raises
    SqlRejectedError when the SQL isn't a plain SELECT, or a column has no name or two share one."""
    return _output_names(_parse(sql))


def query_parameters(sql: str) -> list[str]:
    """The @parameters the SELECT reads, without the @, in order of first appearance. System variables such as
    @@ROWCOUNT are not parameters. Raises SqlRejectedError when the SQL isn't a plain SELECT."""
    found = (
        p.name
        for p in _parse(sql).find_all(exp.Parameter)
        if p.name and not isinstance(p.parent, exp.Parameter)  # @@x parses as a Parameter inside a Parameter
    )
    return list(dict.fromkeys(found))


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


def unjoined_tables(tree: exp.Select) -> list[str]:
    """Tables joined with no join condition (a comma or CROSS JOIN): each row is paired with every row of the other
    side, which multiplies the rows and is almost never what a report means. APPLY is not counted: it is correlated."""
    found = []
    for select in tree.find_all(exp.Select):
        for join in select.args.get("joins") or []:
            if isinstance(join.this, exp.Lateral) or join.args.get("on") or join.args.get("using"):
                continue
            found.append(join.this.sql(dialect=_DIALECT))
    return found


def _own_nodes(select: exp.Select, node: exp.Expression, kind: type[exp.Expression]) -> list[exp.Expression]:
    """The `kind` nodes under `node` that belong to `select` itself, not to a subquery nested in it."""
    return [found for found in node.find_all(kind) if found.find_ancestor(exp.Select) is select]


def _within(node: exp.Expression, stop: exp.Expression, *kinds: type[exp.Expression]) -> bool:
    """Whether a `kinds` node lies between `node` and `stop` (exclusive), looking upward."""
    parent = node.parent
    while parent is not None and parent is not stop:
        if isinstance(parent, kinds):
            return True
        parent = parent.parent
    return False


def _left_join_filtered_warnings(tree: exp.Select) -> list[str]:
    """A LEFT JOIN whose table is then filtered in WHERE drops its unmatched rows again: it is an INNER JOIN that the
    optimizer may not simplify. Conditions that keep the NULL row (IS NULL, OR, COALESCE/ISNULL) are left alone."""
    warnings = []
    for select in tree.find_all(exp.Select):
        where = select.args.get("where")
        if where is None:
            continue
        for join in select.args.get("joins") or []:
            if join.side != "LEFT":
                continue
            alias = join.this.alias_or_name.lower()
            for column in _own_nodes(select, where, exp.Column):
                if column.table.lower() != alias:
                    continue
                if _within(column, where, exp.Is, exp.Or, exp.Coalesce):
                    continue
                warnings.append(
                    f"`{join.this.sql(dialect=_DIALECT)}` is LEFT JOINed but WHERE filters on {column.sql(dialect=_DIALECT)}, "
                    "which removes the unmatched rows: write INNER JOIN, or move the condition into ON to keep them"
                )
                break
    return warnings


def _structure_warnings(tree: exp.Select) -> list[str]:
    warnings = [
        f"`{table}` is joined with no join condition, so every row pairs with every row of the other side"
        for table in unjoined_tables(tree)
    ]
    for node in tree.find_all(exp.Not):
        inner = node.this.this if isinstance(node.this, exp.Paren) else node.this
        if isinstance(inner, exp.In) and inner.args.get("query") is not None:
            warnings.append(
                f"`{node.sql(dialect=_DIALECT)[:90]}`: NOT IN over a subquery returns no rows at all if the subquery "
                "yields a NULL, and can stop the optimizer using an anti-join; use NOT EXISTS"
            )
    for item in tree.expressions:
        if item.find(exp.Subquery) is not None and not item.find(exp.Exists):
            warnings.append(
                f"output column {item.alias_or_name!r} is a subquery in the select list, evaluated for every row; "
                "join the table or aggregate it once in a derived table"
            )
    for select in tree.find_all(exp.Select):
        conditions = [select.args.get("where"), *(j.args.get("on") for j in select.args.get("joins") or [])]
        for condition in filter(None, conditions):
            for node in _own_nodes(select, condition, exp.Or):
                if _within(node, condition, exp.Or):
                    continue  # the outermost OR of a chain speaks for it
                columns = {(c.table.lower(), c.name.lower()) for c in node.find_all(exp.Column)}
                if len(columns) > 1:
                    warnings.append(
                        f"`{node.sql(dialect=_DIALECT)[:90]}` ORs conditions on different columns, so no single index "
                        "can seek it; use IN for one column, or check that the OR is really needed"
                    )
    warnings.extend(_left_join_filtered_warnings(tree))
    return warnings


def _warnings(tree: exp.Select) -> list[str]:
    warnings = _sargability_warnings(tree)
    warnings.extend(_structure_warnings(tree))
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
    _output_names(tree)  # every output column named and unique, so the SQL can later back a report
    _canonical_table_names(tree, columns)
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
        unjoined=unjoined_tables(tree),
    )
