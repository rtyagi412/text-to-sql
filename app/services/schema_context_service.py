import hashlib
import re
from dataclasses import dataclass, field

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
    snapshot: str  # short hash of `text`, so an answer can be tied to the exact schema the model saw
    columns: dict[str, dict[str, str]] = field(default_factory=dict)  # "schema.table" -> {column: data type}, for checking a model's picks
    allowed_values: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # "schema.table" -> {column: values its CHECK allows}


def _table_key(schema_name: str, table_name: str) -> str:
    return f"{schema_name}.{table_name}"


def retrieval_queries(output_fields: str, report_criteria: str) -> list[str]:
    """Several angles on the same request. Retrieval recall is the ceiling on what the model can pick, so a
    whole-request query is complemented by one query per requested field and per criteria clause.

    Bare "<Entity> ID" fields get no query of their own: they're foreign keys already on the primary
    table's row, and querying for them just pulls in that entity's (often hub) table as noise. They
    are still covered by the whole-request query."""
    whole = " ".join(part for part in (output_fields, report_criteria) if part.strip())
    queries = [whole] if whole.strip() else []
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


def table_index(catalog_db: Session) -> str:
    """Every table in the catalog, one line each with its description. It changes only when the catalog is synced,
    so it can sit in the cached part of the prompt; it tells the model what exists beyond the retrieved slice."""
    rows = (
        catalog_db.query(SchemaTable.schema_name, SchemaTable.table_name, SchemaTable.description)
        .order_by(SchemaTable.schema_name, SchemaTable.table_name)
        .all()
    )
    return "\n".join(
        f"{_table_key(schema, table)} -- {description}" if description else _table_key(schema, table)
        for schema, table, description in rows
    )


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
        chosen = _expand_with_neighbours(chosen, [(fk, pk) for fk, pk in edges], cap)
    return chosen


def _expand_with_neighbours(seeds: list[int], edges: list[tuple[int, int]], cap: int) -> list[int]:
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
    notes = [column.description] if column.description else []
    if column.allowed_values:
        notes.append("only values: " + ", ".join(f"'{v}'" for v in column.allowed_values))
    note = f"  -- {' | '.join(notes)}" if notes else ""
    return f"  {column.column_name} {column.data_type} {' '.join(flags)}{note}"


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
    return _render_context(catalog_db, by_id, table_ids)


def build_exact_schema_context(catalog_db: Session, table_keys: list[str]) -> SchemaContext:
    """Exactly the given "schema.table" tables, in the given order: no retrieval and no neighbour expansion.
    Keys the catalog doesn't have are skipped, so a caller can tell them apart by their absence from
    `tables` / `columns`."""
    by_id, key_to_id = _load_tables(catalog_db)
    by_lower = {key.lower(): table_id for key, table_id in key_to_id.items()}  # object names are case-insensitive
    table_ids = list(dict.fromkeys(by_lower[key.lower()] for key in table_keys if key.lower() in by_lower))
    return _render_context(catalog_db, by_id, table_ids)


def _render_context(catalog_db: Session, by_id: dict[int, SchemaTable], table_ids: list[int]) -> SchemaContext:
    """The chosen tables as prompt text (columns, then the foreign keys among them) plus the same facts as data,
    so a model's picks can be checked against exactly what it was shown."""
    keys = {i: _table_key(by_id[i].schema_name, by_id[i].table_name) for i in table_ids}

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
        header = f"TABLE {keys[table_id]}"
        if by_id[table_id].description:
            header += f"  -- {by_id[table_id].description}"
        blocks.append("\n".join([header, *(_render_column(c) for c in columns_by_table.get(table_id, []))]))

    edges = [
        f"  {keys[rel.fk_table_id]}.{rel.fk_column_name} -> {keys[rel.pk_table_id]}.{rel.pk_column_name}"
        for rel in catalog_db.query(SchemaRelationship).all()
        if rel.fk_table_id in keys and rel.pk_table_id in keys
    ]
    blocks.append("\n".join(["RELATIONSHIPS (foreign key -> primary key; the only join keys that are known)", *sorted(edges)]))

    text = "\n\n".join(blocks)
    return SchemaContext(
        text=text,
        tables=[keys[i] for i in table_ids],
        snapshot=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        columns={keys[i]: {c.column_name: c.data_type for c in columns_by_table.get(i, [])} for i in table_ids},
        allowed_values={
            keys[i]: {c.column_name: c.allowed_values for c in columns_by_table.get(i, []) if c.allowed_values}
            for i in table_ids
        },
    )
