"""The deterministic foreign-key join plan the write stage shows the model."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.schemas.catalog import AmbiguousJoinPath, JoinEdge
from app.services.join_service import hub_tables, key_text, resolve_join_paths


def join_plan(catalog_db: Session, tables: list[str], branches: list[str] | None = None) -> tuple[str, list[str]]:
    """The shortest foreign-key paths connecting `tables`, as prompt text, plus the bridge tables those paths
    pass through. Deterministic, so the model picks join keys from the catalog rather than inventing them.

    `branches` are tables a derived value is counted or summed over (the tables that reference a reason code, for
    "how often is it used"). Each is attached to the report's own tables on its own, never to another branch: two
    branches joined to each other pair every row of one with every row of the other. The write step aggregates each
    branch before joining it. With no `tables` the branches are simply the report's tables.

    Paths never run through a hub table (merchant: nearly every table points at it), so two tables that merely
    share a merchant are not joined on it: that pairs a settlement with every payment of its merchant instead of
    the payments it contains, and the shortest such path would hide the real one (customer -> merchant <-
    settlement is 2 hops; customer <- order <- payment <- settlement_payment -> settlement is 4). A table that
    can only be reached through a hub is joined that way as a flagged fallback. Equally short paths are all
    shown, with their tables, and the right path's tables must be in the schema."""
    branches = [b for b in dict.fromkeys(branches or []) if b not in tables]
    if not tables:
        tables, branches = branches, []
    resolved = _resolve(catalog_db, tables)
    for branch in branches:
        resolved = _merge(resolved, _resolve(catalog_db, tables, branch))
    everything = [*tables, *branches]
    return _render(resolved, tables, branches), _bridge_tables(resolved, everything)


@dataclass(frozen=True)
class _ResolvedJoins:
    start: str  # the table the search started from; every other table is joined towards it
    edges: list[JoinEdge]  # the foreign keys to join along
    ambiguous_joins: list[AmbiguousJoinPath]  # pairs of tables that connect along several equally short paths
    via_hub: list[str]  # tables connected to `start` only through a hub (the flagged fallback)
    unreachable: list[str]  # tables with no foreign-key path to `start`, even through a hub


def _resolve(catalog_db: Session, tables: list[str], branch: str | None = None) -> _ResolvedJoins:
    """Finds the paths without running through a hub; whatever that leaves unreachable is retried allowing hubs.
    A `branch` is searched for last, so it attaches to the nearest of `tables` and nothing is attached to it."""
    hubs = hub_tables(catalog_db)
    # The search starts from the first table; a hub as the start would fan out to all its children at once.
    ordered = [*(t for t in tables if t not in hubs), *(t for t in tables if t in hubs), *([branch] if branch else [])]
    result = resolve_join_paths(catalog_db, ordered, no_transit=hubs, grow_tree=True)

    edges = list(result.edges)
    ambiguous_joins = list(result.ambiguous_joins)
    unreachable = list(result.unreachable_tables)
    via_hub: list[str] = []
    if unreachable:
        fallback = resolve_join_paths(catalog_db, [ordered[0], *unreachable], grow_tree=True)
        known = {edge.constraint_name for edge in edges}
        edges += [edge for edge in fallback.edges if edge.constraint_name not in known]
        ambiguous_joins += fallback.ambiguous_joins
        unreachable = list(fallback.unreachable_tables)
        via_hub = [table for table in result.unreachable_tables if table not in unreachable]

    return _ResolvedJoins(
        start=ordered[0] if ordered else "",
        edges=edges,
        ambiguous_joins=ambiguous_joins,
        via_hub=via_hub,
        unreachable=unreachable,
    )


def _merge(first: _ResolvedJoins, other: _ResolvedJoins) -> _ResolvedJoins:
    """The plans of two searches over overlapping tables as one, without repeating an edge or an ambiguity."""
    known = {edge.constraint_name for edge in first.edges}
    seen = {(a.table_a, a.table_b) for a in first.ambiguous_joins}
    return _ResolvedJoins(
        start=first.start,
        edges=[*first.edges, *(e for e in other.edges if e.constraint_name not in known)],
        ambiguous_joins=[*first.ambiguous_joins, *(a for a in other.ambiguous_joins if (a.table_a, a.table_b) not in seen)],
        via_hub=list(dict.fromkeys([*first.via_hub, *other.via_hub])),
        unreachable=list(dict.fromkeys([*first.unreachable, *other.unreachable])),
    )


def _render(resolved: _ResolvedJoins, tables: list[str], branches: list[str]) -> str:
    """The plan as prompt text: the edges, then a flagged line for each hub path, unreachable table and ambiguity."""
    lines = ["JOIN PLAN (shortest foreign-key paths between the tables the report needs; join only along these keys)"]
    lines += [f"  {_edge_text(edge)}" for edge in resolved.edges]
    if len(tables) + len(branches) == 1:
        lines.append("  (a single table: no join is needed)")
    elif not resolved.edges and not resolved.unreachable:
        lines.append("  (none needed)")
    if branches:
        lines.append(
            f"BRANCH TABLES ({', '.join(branches)}): a derived field is computed over each of these. Each joins to "
            "the report's own tables on its own key above and never to another branch: aggregate it first (GROUP BY "
            "its key in a derived table), then join the result."
        )
    if resolved.via_hub:
        lines.append(
            f"HUB PATH ({', '.join(resolved.via_hub)}): connected to {resolved.start} only through a shared parent table "
            "that both reference. Such a join pairs each row with every row of the same parent. Use it only if the "
            "report is about that parent; otherwise ask."
        )
    for table in resolved.unreachable:
        lines.append(f"UNREACHABLE: {table} has no foreign-key path to {resolved.start}")
    for ambiguous in resolved.ambiguous_joins:
        lines.append(
            f"AMBIGUOUS: {ambiguous.table_a} and {ambiguous.table_b} connect along {len(ambiguous.path_options)} "
            "equally short paths. Choose the one that relates the two directly (see the rules); the edges listed "
            "above include only the first option, as a placeholder:"
        )
        for number, option in enumerate(ambiguous.path_options, start=1):
            lines.append(f"  option {number}: " + "; ".join(_edge_text(edge) for edge in option))
    return "\n".join(lines)


def _bridge_tables(resolved: _ResolvedJoins, tables: list[str]) -> list[str]:
    """Tables the paths pass through that the report itself does not use. They must be in the schema the model sees,
    including those on the alternative paths of an ambiguous join."""
    bridge: list[str] = []
    all_edges = [*resolved.edges, *(edge for ambiguous in resolved.ambiguous_joins for option in ambiguous.path_options for edge in option)]
    for edge in all_edges:
        for key in (f"{edge.from_schema}.{edge.from_table}", f"{edge.to_schema}.{edge.to_table}"):
            if key not in tables and key not in bridge:
                bridge.append(key)
    return bridge


def _edge_text(edge: JoinEdge) -> str:
    """`a.t.Col -> b.u.Col`; a composite key lists all its columns, `a.t(ColA, ColB) -> b.u(ColX, ColY)`, and is joined on every pair."""
    return (
        f"{key_text(f'{edge.from_schema}.{edge.from_table}', edge.from_columns)} -> "
        f"{key_text(f'{edge.to_schema}.{edge.to_table}', edge.to_columns)}"
    )
