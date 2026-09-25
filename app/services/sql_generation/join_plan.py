"""The deterministic foreign-key join plan the write stage shows the model."""

from sqlalchemy.orm import Session

from app.schemas.catalog import JoinEdge
from app.services import schema_context_service
from app.services.join_service import resolve_join_paths


def _edge_text(edge: JoinEdge) -> str:
    return (
        f"{edge.from_schema}.{edge.from_table}.{edge.from_column} -> "
        f"{edge.to_schema}.{edge.to_table}.{edge.to_column}"
    )


def join_plan(catalog_db: Session, tables: list[str]) -> tuple[str, list[str]]:
    """The shortest foreign-key paths connecting `tables`, as prompt text, plus the bridge tables those paths
    pass through. Deterministic, so the model picks join keys from the catalog rather than inventing them.

    Paths never run through a hub table (merchant: nearly every table points at it), so two tables that merely
    share a merchant are not joined on it: that pairs a settlement with every payment of its merchant instead of
    the payments it contains, and the shortest such path would hide the real one (customer -> merchant <-
    settlement is 2 hops; customer <- order <- payment <- settlement_payment -> settlement is 4). A table that
    can only be reached through a hub is joined that way as a flagged fallback. Equally short paths are all
    shown, with their tables, and the right path's tables must be in the schema."""
    hubs = schema_context_service.hub_tables(catalog_db)
    # The search starts from the first table; a hub as the start would fan out to all its children at once.
    ordered = [*(t for t in tables if t not in hubs), *(t for t in tables if t in hubs)]
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

    lines = ["JOIN PLAN (shortest foreign-key paths between the tables the report needs; join only along these keys)"]
    bridge: list[str] = []

    def note_bridge(edge: JoinEdge) -> None:
        for key in (f"{edge.from_schema}.{edge.from_table}", f"{edge.to_schema}.{edge.to_table}"):
            if key not in tables and key not in bridge:
                bridge.append(key)

    for edge in edges:
        lines.append(f"  {_edge_text(edge)}")
        note_bridge(edge)
    if len(tables) == 1:
        lines.append("  (a single table: no join is needed)")
    elif not edges and not unreachable:
        lines.append("  (none needed)")
    if via_hub:
        lines.append(
            f"HUB PATH ({', '.join(via_hub)}): connected to {ordered[0]} only through a shared parent table that both "
            "reference. Such a join pairs each row with every row of the same parent. Use it only if the report is "
            "about that parent; otherwise ask."
        )
    for table in unreachable:
        lines.append(f"UNREACHABLE: {table} has no foreign-key path to {ordered[0]}")
    for ambiguous in ambiguous_joins:
        lines.append(
            f"AMBIGUOUS: {ambiguous.table_a} and {ambiguous.table_b} connect along {len(ambiguous.path_options)} "
            "equally short paths. Choose the one that relates the two directly (see the rules); the edges listed "
            "above include only the first option, as a placeholder:"
        )
        for number, option in enumerate(ambiguous.path_options, start=1):
            lines.append(f"  option {number}: " + "; ".join(_edge_text(edge) for edge in option))
            for edge in option:
                note_bridge(edge)
    return "\n".join(lines), bridge
