import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.catalog import SchemaColumn, SchemaRelationship, SchemaTable
from app.services.retrieval_service import search_tables

settings = get_settings()

_BARE_ID_FIELD = re.compile(r"^[\w\s]+\bid$", re.IGNORECASE)
_LIST_SPLIT = re.compile(r"[,;\n]")


class CatalogEmptyError(RuntimeError):
    """The schema catalog has no tables -- POST /catalog/sync has not been run."""


@dataclass
class SchemaContext:
    text: str
    tables: list[str]  # "schema.table", in the order they were rendered
    snapshot: str  # short hash of `text`, so an answer can be tied to the exact schema Claude saw
    queries: list[str] = field(default_factory=list)
    columns: dict[str, dict[str, str]] = field(default_factory=dict)  # "schema.table" -> {column: data type}, for checking a model's picks


def _table_key(schema_name: str, table_name: str) -> str:
    return f"{schema_name}.{table_name}"


def retrieval_queries(output_fields: str, report_criteria: str, summary: str, user_input: str | None) -> list[str]:
    """Several angles on the same ticket. Retrieval recall is the ceiling on what Claude can pick, so a
    whole-ticket query is complemented by one query per requested field and per criteria clause. The
    summary can name fields and conditions the two structured lists omit, so it gets a query of its own
    (kept out of the whole-ticket query, whose keyword match needs every term present).

    Bare "<Entity> ID" fields get no query of their own: they're foreign keys already on the primary
    table's row, and querying for them just pulls in that entity's (often hub) table as noise. They
    are still covered by the whole-ticket query."""
    whole = " ".join(part for part in (output_fields, report_criteria, user_input or "") if part.strip())
    queries = [whole] if whole.strip() else []
    if summary.strip():
        queries.append(summary.strip())
    for item in _LIST_SPLIT.split(output_fields):
        item = item.strip()
        if item and not _BARE_ID_FIELD.match(item):
            queries.append(item)
    for clause in _LIST_SPLIT.split(report_criteria):
        clause = clause.strip().rstrip(".")
        if clause:
            queries.append(clause)

    seen: set[str] = set()
    unique = []
    for q in queries:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            unique.append(q)
    return unique[: settings.schema_max_queries]


def hub_tables(catalog_db: Session) -> list[str]:
    """Tables that at least `catalog_hub_min_references` foreign keys point at (typically the tenant table, here
    merchant). Two tables that both reference a hub are not related to each other through it: joining on it pairs
    each row with every row of the same parent, so a join path should not run through one."""
    rows = (
        catalog_db.query(SchemaTable.schema_name, SchemaTable.table_name)
        .join(SchemaRelationship, SchemaRelationship.pk_table_id == SchemaTable.id)
        .group_by(SchemaTable.id, SchemaTable.schema_name, SchemaTable.table_name)
        .having(func.count(SchemaRelationship.id) >= settings.catalog_hub_min_references)
        .all()
    )
    return [_table_key(schema, table) for schema, table in rows]


def existing_table_keys(catalog_db: Session) -> set[str]:
    rows = catalog_db.query(SchemaTable.schema_name, SchemaTable.table_name).all()
    return {_table_key(schema, table) for schema, table in rows}


def _select_table_ids(
    catalog_db: Session, queries: list[str], forced_tables: list[str], key_to_id: dict[str, int]
) -> list[int]:
    """Ordered table ids: forced tables first, then tables corroborated by the most queries, then
    FK neighbours (bridge tables touching several seeds first), all under `schema_max_tables`."""
    cap = settings.schema_max_tables

    hits: dict[int, int] = {}
    best: dict[int, float] = {}
    for query in queries:
        for result in search_tables(catalog_db, query, top_k=settings.schema_search_top_k):
            hits[result.table_id] = hits.get(result.table_id, 0) + 1
            best[result.table_id] = max(best.get(result.table_id, 0.0), result.hybrid_score)

    chosen: list[int] = []
    for key in forced_tables:
        table_id = key_to_id.get(key)
        if table_id is not None and table_id not in chosen:
            chosen.append(table_id)
    for table_id in sorted(hits, key=lambda t: (-hits[t], -best[t])):
        if table_id not in chosen:
            chosen.append(table_id)
    chosen = chosen[:cap]

    if len(chosen) < cap:
        edges = catalog_db.query(SchemaRelationship.fk_table_id, SchemaRelationship.pk_table_id).all()
        chosen = expand_with_neighbours(chosen, [(fk, pk) for fk, pk in edges], cap)
    return chosen


def expand_with_neighbours(seeds: list[int], edges: list[tuple[int, int]], cap: int) -> list[int]:
    """Appends tables one FK hop from the seeds until `cap` is reached. A table touching several seeds
    (typically a bridge table the report has to pass through) is added before one touching a single seed,
    so a hub table's many children can't crowd out the connectors that matter."""
    seed_set = set(seeds)
    links: dict[int, int] = {}
    for fk_table_id, pk_table_id in edges:
        if fk_table_id in seed_set and pk_table_id not in seed_set:
            links[pk_table_id] = links.get(pk_table_id, 0) + 1
        elif pk_table_id in seed_set and fk_table_id not in seed_set:
            links[fk_table_id] = links.get(fk_table_id, 0) + 1

    result = list(seeds)
    for table_id in sorted(links, key=lambda t: (-links[t], t)):
        if len(result) >= cap:
            break
        result.append(table_id)
    return result


def _render_column(column: SchemaColumn) -> str:
    flags = []
    if column.is_primary_key:
        flags.append("PK")
    if column.is_foreign_key:
        flags.append("FK")
    flags.append("NULL" if column.is_nullable else "NOT NULL")
    description = f"  -- {column.description}" if column.description else ""
    return f"  {column.column_name} {column.data_type} {' '.join(flags)}{description}"


def _load_tables(catalog_db: Session) -> tuple[dict[int, SchemaTable], dict[str, int]]:
    tables = catalog_db.query(SchemaTable).all()
    if not tables:
        raise CatalogEmptyError("The schema catalog is empty; run POST /catalog/sync first")
    return {t.id: t for t in tables}, {_table_key(t.schema_name, t.table_name): t.id for t in tables}


def build_schema_context(
    catalog_db: Session, queries: list[str], forced_tables: list[str] | None = None
) -> SchemaContext:
    by_id, key_to_id = _load_tables(catalog_db)
    table_ids = _select_table_ids(catalog_db, queries, forced_tables or [], key_to_id)
    return _render_context(catalog_db, by_id, table_ids, queries)


def build_exact_schema_context(catalog_db: Session, table_keys: list[str]) -> SchemaContext:
    """Exactly the given "schema.table" tables, in the given order: no retrieval and no neighbour expansion.
    Keys the catalog doesn't have are skipped, so a caller can tell them apart by their absence from
    `tables` / `columns`."""
    by_id, key_to_id = _load_tables(catalog_db)
    table_ids = [key_to_id[key] for key in dict.fromkeys(table_keys) if key in key_to_id]
    return _render_context(catalog_db, by_id, table_ids, [])


def _render_context(
    catalog_db: Session, by_id: dict[int, SchemaTable], table_ids: list[int], queries: list[str]
) -> SchemaContext:
    chosen_set = set(table_ids)

    columns = (
        catalog_db.query(SchemaColumn)
        .filter(SchemaColumn.table_id.in_(table_ids))
        .order_by(SchemaColumn.table_id, SchemaColumn.ordinal_position)
        .all()
    )
    columns_by_table: dict[int, list[SchemaColumn]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)

    blocks = []
    for table_id in table_ids:
        table = by_id[table_id]
        header = f"TABLE {_table_key(table.schema_name, table.table_name)}"
        if table.description:
            header += f"  -- {table.description}"
        blocks.append("\n".join([header, *(_render_column(c) for c in columns_by_table.get(table_id, []))]))

    edges = []
    for rel in catalog_db.query(SchemaRelationship).all():
        if rel.fk_table_id in chosen_set and rel.pk_table_id in chosen_set:
            fk, pk = by_id[rel.fk_table_id], by_id[rel.pk_table_id]
            edges.append(
                f"  {_table_key(fk.schema_name, fk.table_name)}.{rel.fk_column_name} -> "
                f"{_table_key(pk.schema_name, pk.table_name)}.{rel.pk_column_name}"
            )
    blocks.append("\n".join(["RELATIONSHIPS (foreign key -> primary key; the only join keys that are known)", *sorted(edges)]))

    text = "\n\n".join(blocks)
    return SchemaContext(
        text=text,
        tables=[_table_key(by_id[i].schema_name, by_id[i].table_name) for i in table_ids],
        snapshot=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        queries=queries,
        columns={
            _table_key(by_id[i].schema_name, by_id[i].table_name): {
                c.column_name: c.data_type for c in columns_by_table.get(i, [])
            }
            for i in table_ids
        },
    )
