from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.catalog import SchemaColumn, SchemaRelationship, SchemaTable
from app.schemas.catalog import ColumnMetadata, SyncResult, TableMetadata
from app.services.introspection_service import introspect_schema
from app.services.embedding_service import embed

settings = get_settings()

_TRUNCATE = "TRUNCATE TABLE schema_relationships, schema_columns, schema_tables RESTART IDENTITY"

_TSV_UPDATE = """
UPDATE schema_tables
SET tsv = setweight(to_tsvector('english', :name_text), 'A')
        || setweight(to_tsvector('english', :description_text), 'B')
        || setweight(to_tsvector('english', :schema_text), 'C')
WHERE id = :table_id
"""


def _normalize(value: str) -> str:
    """snake_case column/identifier names need spaces to tokenize as separate search terms."""
    return value.replace("_", " ")


def _format_column_line(column: ColumnMetadata) -> str:
    flags = []
    if column.is_primary_key:
        flags.append("primary key")
    if column.is_foreign_key:
        flags.append("foreign key")
    if not column.is_nullable:
        flags.append("not null")
    flag_suffix = f" ({', '.join(flags)})" if flags else ""
    desc_suffix = f": {column.description}" if column.description else ""
    return f"- {column.name} [{column.data_type}]{flag_suffix}{desc_suffix}"


def build_doc_text(table: TableMetadata) -> str:
    lines = [f"Table: {table.schema_name}.{table.table_name}"]
    if table.description:
        lines.append(f"Description: {table.description}")
    lines.append("Columns:")
    lines.extend(_format_column_line(c) for c in table.columns)
    return "\n".join(lines)


def _tsv_fragments(table: TableMetadata) -> tuple[str, str, str]:
    """Separate text groups so setweight() can rank them in _TSV_UPDATE.

    The table's own name is weighted the same as its column names ('A') -- otherwise a query
    naming a table directly (e.g. "merchant") loses to sibling tables that merely have a
    matching FK column (e.g. a merchant_id column contributing weight-A "merchant" + "id").
    The schema name (usually just "dbo") carries the least signal, so it alone gets 'C'.
    """
    name_text = " ".join(
        [_normalize(table.table_name), *(_normalize(c.name) for c in table.columns)]
    )
    description_text = " ".join(filter(None, [table.description, *(c.description for c in table.columns)]))
    schema_text = _normalize(table.schema_name)
    return name_text, description_text, schema_text


def sync_catalog(source_db: Session, catalog_db: Session) -> SyncResult:
    """Full-replace sync: re-introspects the source DB and rebuilds the catalog from scratch.

    Simpler than diffing against existing rows, and safe here since the catalog only
    mirrors the source schema and nothing outside it holds onto schema_tables ids.
    """
    introspection = introspect_schema(source_db)

    doc_texts = [build_doc_text(t) for t in introspection.tables]
    embeddings = embed(doc_texts, "document")

    catalog_db.execute(text(_TRUNCATE))

    now = datetime.now(UTC)
    table_ids: dict[tuple[str, str], int] = {}

    for table_meta, doc_text_value, embedding in zip(introspection.tables, doc_texts, embeddings, strict=True):
        row = SchemaTable(
            schema_name=table_meta.schema_name,
            table_name=table_meta.table_name,
            description=table_meta.description,
            doc_text=doc_text_value,
            embedding=embedding,
            synced_at=now,
        )
        catalog_db.add(row)
        catalog_db.flush()
        table_ids[(table_meta.schema_name, table_meta.table_name)] = row.id

        for col in table_meta.columns:
            catalog_db.add(
                SchemaColumn(
                    table_id=row.id,
                    column_name=col.name,
                    data_type=col.data_type,
                    is_nullable=col.is_nullable,
                    is_primary_key=col.is_primary_key,
                    is_foreign_key=col.is_foreign_key,
                    ordinal_position=col.ordinal_position,
                    description=col.description,
                )
            )

        name_text, description_text, schema_text = _tsv_fragments(table_meta)
        catalog_db.execute(
            text(_TSV_UPDATE),
            {
                "table_id": row.id,
                "name_text": name_text,
                "description_text": description_text,
                "schema_text": schema_text,
            },
        )

    relationships_synced = 0
    for rel in introspection.relationships:
        fk_table_id = table_ids.get((rel.fk_schema, rel.fk_table))
        pk_table_id = table_ids.get((rel.pk_schema, rel.pk_table))
        if fk_table_id is None or pk_table_id is None:
            continue
        catalog_db.add(
            SchemaRelationship(
                constraint_name=rel.constraint_name,
                fk_table_id=fk_table_id,
                fk_column_name=rel.fk_column,
                pk_table_id=pk_table_id,
                pk_column_name=rel.pk_column,
            )
        )
        relationships_synced += 1

    catalog_db.commit()

    return SyncResult(
        tables_synced=len(introspection.tables),
        columns_synced=sum(len(t.columns) for t in introspection.tables),
        relationships_synced=relationships_synced,
        embedding_model=settings.embedding_model,
    )
