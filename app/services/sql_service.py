from app.schemas.catalog import GroundedField, GroundedFilter, GroundedSchemaMapping, JoinEdge
from app.schemas.extraction import FilterValue, Operator, Status
from app.schemas.sql import GeneratedSql

_COMPARISON_OPERATORS = {
    Operator.EQUALS: "=",
    Operator.NOT_EQUALS: "<>",
    Operator.GREATER_THAN: ">",
    Operator.GREATER_THAN_OR_EQUAL: ">=",
    Operator.LESS_THAN: "<",
    Operator.LESS_THAN_OR_EQUAL: "<=",
}

_LIKE_PATTERNS = {
    Operator.CONTAINS: "%{}%",
    Operator.STARTS_WITH: "{}%",
    Operator.ENDS_WITH: "%{}",
}


def _quote(identifier: str) -> str:
    return f"[{identifier}]"


def _table_key(schema_name: str, table_name: str) -> str:
    return f"{schema_name}.{table_name}"


def _assign_aliases(tables: list[str]) -> dict[str, str]:
    return {table: f"t{i}" for i, table in enumerate(tables)}


def _order_joins(tables: list[str], edges: list[JoinEdge]) -> list[tuple[JoinEdge, str, str]]:
    """Orders join edges into a valid emission sequence: an edge is only emitted once one of its
    two tables is already placed (starting from the root table, tables[0]), so every `JOIN ... ON`
    clause can reference an alias introduced earlier in the same query rather than one that
    doesn't exist yet."""
    if len(tables) <= 1:
        return []

    remaining = list(edges)
    placed = {tables[0]}
    ordered: list[tuple[JoinEdge, str, str]] = []

    while remaining:
        progressed = False
        still_remaining = []
        for edge in remaining:
            from_key = _table_key(edge.from_schema, edge.from_table)
            to_key = _table_key(edge.to_schema, edge.to_table)
            if from_key in placed and to_key not in placed:
                ordered.append((edge, from_key, to_key))
                placed.add(to_key)
                progressed = True
            elif to_key in placed and from_key not in placed:
                ordered.append((edge, to_key, from_key))
                placed.add(from_key)
                progressed = True
            else:
                still_remaining.append(edge)
        if not progressed:
            raise ValueError("join edges do not form a tree reachable from the root table")
        remaining = still_remaining

    return ordered


def _select_clause(fields: list[GroundedField], aliases: dict[str, str]) -> str:
    parts = []
    for field in fields:
        col = field.column
        alias = aliases[_table_key(col.schema_name, col.table_name)]
        parts.append(f"{alias}.{_quote(col.column_name)} AS {_quote(field.requested_name)}")
    return "SELECT " + ",\n       ".join(parts)


def _from_clause(tables: list[str], edges: list[JoinEdge], aliases: dict[str, str]) -> str:
    root_schema, root_table = tables[0].split(".", 1)
    lines = [f"FROM {_quote(root_schema)}.{_quote(root_table)} AS {aliases[tables[0]]}"]

    for edge, placed_key, new_key in _order_joins(tables, edges):
        placed_alias, new_alias = aliases[placed_key], aliases[new_key]
        if _table_key(edge.from_schema, edge.from_table) == new_key:
            new_col, placed_col = edge.from_column, edge.to_column
        else:
            new_col, placed_col = edge.to_column, edge.from_column
        new_schema, new_table = new_key.split(".", 1)
        lines.append(
            f"JOIN {_quote(new_schema)}.{_quote(new_table)} AS {new_alias} "
            f"ON {placed_alias}.{_quote(placed_col)} = {new_alias}.{_quote(new_col)}"
        )
    return "\n".join(lines)


def _where_clause(filters: list[GroundedFilter], aliases: dict[str, str]) -> tuple[str, dict[str, FilterValue]]:
    predicates: list[str] = []
    parameters: dict[str, FilterValue] = {}
    param_index = 0

    def next_param(value: FilterValue) -> str:
        nonlocal param_index
        name = f"p{param_index}"
        param_index += 1
        parameters[name] = value
        return name

    for filt in filters:
        col = filt.column
        column_ref = f"{aliases[_table_key(col.schema_name, col.table_name)]}.{_quote(col.column_name)}"

        if filt.operator is Operator.IS_NULL:
            predicates.append(f"{column_ref} IS NULL")
        elif filt.operator is Operator.IS_NOT_NULL:
            predicates.append(f"{column_ref} IS NOT NULL")
        elif filt.operator in (Operator.IN, Operator.NOT_IN):
            values = filt.value if isinstance(filt.value, list) else [filt.value]
            placeholders = [f":{next_param(v)}" for v in values]
            keyword = "IN" if filt.operator is Operator.IN else "NOT IN"
            predicates.append(f"{column_ref} {keyword} ({', '.join(placeholders)})")
        elif filt.operator is Operator.BETWEEN:
            low, high = filt.value
            predicates.append(f"{column_ref} BETWEEN :{next_param(low)} AND :{next_param(high)}")
        elif filt.operator in _LIKE_PATTERNS:
            pattern = _LIKE_PATTERNS[filt.operator].format(filt.value)
            predicates.append(f"{column_ref} LIKE :{next_param(pattern)}")
        else:
            predicates.append(f"{column_ref} {_COMPARISON_OPERATORS[filt.operator]} :{next_param(filt.value)}")

    clause = "WHERE " + "\n  AND ".join(predicates) if predicates else ""
    return clause, parameters


def build_sql(mapping: GroundedSchemaMapping) -> GeneratedSql:
    if mapping.status is Status.NEEDS_CLARIFICATION:
        raise ValueError("cannot generate SQL for a mapping that needs clarification")
    if mapping.join_paths.unreachable_tables:
        raise ValueError(f"unreachable tables: {mapping.join_paths.unreachable_tables}")
    if mapping.join_paths.ambiguous_joins:
        raise ValueError("cannot generate SQL when join paths are ambiguous")
    if not mapping.fields:
        raise ValueError("no resolved fields to select")

    tables = mapping.join_paths.tables
    aliases = _assign_aliases(tables)

    select_clause = _select_clause(mapping.fields, aliases)
    from_clause = _from_clause(tables, mapping.join_paths.edges, aliases)
    where_clause, parameters = _where_clause(mapping.filters, aliases)

    sql = "\n".join(part for part in (select_clause, from_clause, where_clause) if part)

    return GeneratedSql(ritm_number=mapping.ritm_number, sql=sql, parameters=parameters, tables=tables)
