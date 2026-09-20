from collections import deque
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.catalog import SchemaRelationship, SchemaTable
from app.schemas.catalog import AmbiguousJoinPath, JoinEdge, JoinPathResult

settings = get_settings()


@dataclass
class RelEdge:
    constraint_name: str
    fk_table_id: int
    fk_column: str
    pk_table_id: int
    pk_column: str

    def other_side(self, table_id: int) -> int:
        return self.pk_table_id if table_id == self.fk_table_id else self.fk_table_id


def _load_table_maps(catalog_db: Session) -> tuple[dict[str, int], dict[int, tuple[str, str]]]:
    tables = catalog_db.query(SchemaTable.id, SchemaTable.schema_name, SchemaTable.table_name).all()
    name_to_id = {f"{schema}.{table}": table_id for table_id, schema, table in tables}
    id_to_name = {table_id: (schema, table) for table_id, schema, table in tables}
    return name_to_id, id_to_name


def _load_adjacency(catalog_db: Session) -> dict[int, list[RelEdge]]:
    rows = catalog_db.query(
        SchemaRelationship.constraint_name,
        SchemaRelationship.fk_table_id,
        SchemaRelationship.fk_column_name,
        SchemaRelationship.pk_table_id,
        SchemaRelationship.pk_column_name,
    ).all()

    adjacency: dict[int, list[RelEdge]] = {}
    for constraint_name, fk_table_id, fk_column, pk_table_id, pk_column in rows:
        edge = RelEdge(
            constraint_name=constraint_name,
            fk_table_id=fk_table_id,
            fk_column=fk_column,
            pk_table_id=pk_table_id,
            pk_column=pk_column,
        )
        adjacency.setdefault(fk_table_id, []).append(edge)
        adjacency.setdefault(pk_table_id, []).append(edge)
    return adjacency


def _bfs_shortest_paths(
    adjacency: dict[int, list[RelEdge]], start_id: int, blocked: frozenset[int] = frozenset()
) -> dict[int, list[tuple[int, RelEdge]]]:
    """Standard multi-predecessor BFS: `preds[v]` holds every (u, edge) pair that reaches v
    along a shortest path from start_id, so callers can detect (and enumerate) ties instead
    of silently picking one when several equally-short joins exist.

    A `blocked` table can be reached but is never stepped through: paths may end at it, not pass it."""
    dist: dict[int, int] = {start_id: 0}
    preds: dict[int, list[tuple[int, RelEdge]]] = {start_id: []}
    queue = deque([start_id])

    while queue:
        u = queue.popleft()
        if u in blocked and u != start_id:
            continue
        for edge in adjacency.get(u, []):
            v = edge.other_side(u)
            if v not in dist:
                dist[v] = dist[u] + 1
                preds[v] = [(u, edge)]
                queue.append(v)
            elif dist[v] == dist[u] + 1:
                preds[v].append((u, edge))

    return preds


def _reconstruct_paths(
    preds: dict[int, list[tuple[int, RelEdge]]], start_id: int, target_id: int, cap: int
) -> list[list[RelEdge]]:
    if target_id == start_id:
        return [[]]
    if target_id not in preds:
        return []

    results: list[list[RelEdge]] = []

    def dfs(node: int, path_edges: list[RelEdge]) -> None:
        if len(results) >= cap:
            return
        if node == start_id:
            results.append(list(reversed(path_edges)))
            return
        for parent, edge in preds.get(node, []):
            dfs(parent, [*path_edges, edge])

    dfs(target_id, [])
    return results


def _to_join_edge(edge: RelEdge, id_to_name: dict[int, tuple[str, str]]) -> JoinEdge:
    fk_schema, fk_table = id_to_name[edge.fk_table_id]
    pk_schema, pk_table = id_to_name[edge.pk_table_id]
    return JoinEdge(
        from_schema=fk_schema,
        from_table=fk_table,
        from_column=edge.fk_column,
        to_schema=pk_schema,
        to_table=pk_table,
        to_column=edge.pk_column,
        constraint_name=edge.constraint_name,
    )


def resolve_join_paths(
    catalog_db: Session, tables_used: list[str], no_transit: list[str] | None = None
) -> JoinPathResult:
    """Shortest foreign-key paths from the first table in `tables_used` to each of the others.
    `no_transit` ("schema.table" names) can end a path but never sit in the middle of one."""
    name_to_id, id_to_name = _load_table_maps(catalog_db)
    target_ids = [name_to_id[t] for t in tables_used if t in name_to_id]

    if len(target_ids) <= 1:
        return JoinPathResult(tables=tables_used)

    adjacency = _load_adjacency(catalog_db)
    root = target_ids[0]
    blocked = frozenset(name_to_id[t] for t in no_transit or [] if t in name_to_id)
    preds = _bfs_shortest_paths(adjacency, root, blocked)

    edges_used: dict[str, JoinEdge] = {}
    unreachable_tables: list[str] = []
    ambiguous_joins: list[AmbiguousJoinPath] = []

    for table_id in target_ids[1:]:
        paths = _reconstruct_paths(preds, root, table_id, settings.catalog_join_path_cap)
        if not paths:
            schema, table = id_to_name[table_id]
            unreachable_tables.append(f"{schema}.{table}")
            continue

        if len(paths) > 1:
            root_schema, root_table = id_to_name[root]
            target_schema, target_table = id_to_name[table_id]
            ambiguous_joins.append(
                AmbiguousJoinPath(
                    table_a=f"{root_schema}.{root_table}",
                    table_b=f"{target_schema}.{target_table}",
                    path_options=[[_to_join_edge(e, id_to_name) for e in path] for path in paths],
                )
            )

        # Even when ambiguous, include the first option's edges so the working join tree stays
        # usable for tables reached only through this pair -- the ambiguity is still reported
        # above so a human (or a later stage) can pick the correct path instead of it being
        # silently guessed.
        for edge in paths[0]:
            join_edge = _to_join_edge(edge, id_to_name)
            edges_used[join_edge.constraint_name] = join_edge

    return JoinPathResult(
        tables=tables_used,
        edges=list(edges_used.values()),
        unreachable_tables=unreachable_tables,
        ambiguous_joins=ambiguous_joins,
    )
