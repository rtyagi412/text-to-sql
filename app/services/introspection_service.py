import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.catalog import ColumnMetadata, IntrospectionResult, RelationshipMetadata, TableMetadata

# List of every table name in the database
_TABLES_QUERY = """
SELECT s.name AS schema_name, t.name AS table_name
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
ORDER BY s.name, t.name
"""

# Human-written notes attached to tables (if anyone added any)
_TABLE_DESCRIPTIONS_QUERY = """
SELECT s.name AS schema_name, t.name AS table_name, CAST(ep.value AS NVARCHAR(MAX)) AS description
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.extended_properties ep
    ON ep.major_id = t.object_id AND ep.minor_id = 0 AND ep.name = 'MS_Description'
"""

# List of every column in every table, with its data type
_COLUMNS_QUERY = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    ty.name AS type_name,
    c.max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.column_id AS ordinal_position
FROM sys.columns c
JOIN sys.tables t ON c.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.types ty ON c.user_type_id = ty.user_type_id
ORDER BY s.name, t.name, c.column_id
"""

# Human-written notes attached to columns (if anyone added any)
_COLUMN_DESCRIPTIONS_QUERY = """
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
    CAST(ep.value AS NVARCHAR(MAX)) AS description
FROM sys.columns c
JOIN sys.tables t ON c.object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.extended_properties ep
    ON ep.major_id = c.object_id AND ep.minor_id = c.column_id AND ep.name = 'MS_Description'
"""

# CHECK constraints: for an enum-like column (status, method, ...) the definition lists every value it can hold
_CHECK_CONSTRAINTS_QUERY = """
SELECT s.name AS schema_name, t.name AS table_name, cc.definition
FROM sys.check_constraints cc
JOIN sys.tables t ON cc.parent_object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
"""

# Which column(s) uniquely identify each row in each table
_PRIMARY_KEYS_QUERY = """
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name
FROM sys.key_constraints kc
JOIN sys.tables t ON kc.parent_object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id
JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE kc.type = 'PK'
"""

# Which columns link to which other tables (the relationships between tables)
_FOREIGN_KEYS_QUERY = """
SELECT
    fk.name AS constraint_name,
    fs.name AS fk_schema,
    ft.name AS fk_table,
    fc.name AS fk_column,
    ps.name AS pk_schema,
    pt.name AS pk_table,
    pc.name AS pk_column
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables ft ON fkc.parent_object_id = ft.object_id
JOIN sys.schemas fs ON ft.schema_id = fs.schema_id
JOIN sys.columns fc ON fc.object_id = fkc.parent_object_id AND fc.column_id = fkc.parent_column_id
JOIN sys.tables pt ON fkc.referenced_object_id = pt.object_id
JOIN sys.schemas ps ON pt.schema_id = ps.schema_id
JOIN sys.columns pc ON pc.object_id = fkc.referenced_object_id AND pc.column_id = fkc.referenced_column_id
ORDER BY fk.name
"""

# Text types measured in characters (length shown to the user, e.g. nvarchar(50))
_VARIABLE_LENGTH_CHAR_TYPES = {"nvarchar", "nchar"}
# Text/binary types measured in bytes (length shown to the user, e.g. varchar(50))
_VARIABLE_LENGTH_BYTE_TYPES = {"varchar", "char", "binary", "varbinary"}
# Number types shown with digits and decimal places, e.g. decimal(10,2)
_PRECISION_SCALE_TYPES = {"decimal", "numeric"}


def _format_data_type(type_name: str, max_length: int, precision: int, scale: int) -> str:
    """Turn raw type info into a readable label, e.g. "varchar(50)" or "decimal(10,2)".

    Args:
        type_name: raw SQL type, e.g. "varchar"
        max_length: size limit in bytes (-1 means unlimited/MAX)
        precision: total digits, for number types
        scale: digits after the decimal point, for number types
    """
    if type_name in _VARIABLE_LENGTH_CHAR_TYPES:
        length = "MAX" if max_length == -1 else max_length // 2
        return f"{type_name}({length})"
    if type_name in _VARIABLE_LENGTH_BYTE_TYPES:
        length = "MAX" if max_length == -1 else max_length
        return f"{type_name}({length})"
    if type_name in _PRECISION_SCALE_TYPES:
        return f"{type_name}({precision},{scale})"
    return type_name


_EQUALS_LITERAL = re.compile(r"\[(\w+)\]\s*=\s*N?'((?:[^']|'')*)'")
_IS_NULL = re.compile(r"\[\w+\]\s+IS\s+NULL", re.IGNORECASE)


def parse_enum_check(definition: str) -> tuple[str, list[str]] | None:
    """(column, allowed values) when a CHECK definition is nothing but `[col]='A' OR [col]='B' ...` on one column,
    optionally also allowing NULL. SQL Server rewrites IN (...) into this OR chain. Anything else -- ranges, JSON
    checks, conditions across columns -- is not an enumeration and returns None."""
    matches = _EQUALS_LITERAL.findall(definition)
    if not matches or len({column.lower() for column, _ in matches}) != 1:
        return None
    rest = _IS_NULL.sub("", _EQUALS_LITERAL.sub("", definition))
    if re.sub(r"\bOR\b|[()\s]", "", rest, flags=re.IGNORECASE):
        return None
    return matches[0][0], sorted({value.replace("''", "'") for _, value in matches})


# Example IntrospectionResult JSON:
# {
#   "tables": [
#     {
#       "schema_name": "dbo",
#       "table_name": "merchant",
#       "description": "Human-written note on the table, or null",
#       "columns": [
#         {
#           "name": "merchant_id",
#           "data_type": "int",
#           "is_nullable": false,
#           "is_primary_key": true,
#           "is_foreign_key": false,
#           "ordinal_position": 1,
#           "description": "Human-written note on the column, or null"
#         }
#       ]
#     }
#   ],
#   "relationships": [
#     {
#       "constraint_name": "FK_order_merchant",
#       "fk_schema": "dbo",
#       "fk_table": "order",
#       "fk_column": "merchant_id",
#       "pk_schema": "dbo",
#       "pk_table": "merchant",
#       "pk_column": "merchant_id"
#     }
#   ]
# }
def introspect_schema(db: Session) -> IntrospectionResult:
    """Read SQL Server system catalogs to build the full table/column/relationship metadata for the connected database.

    Args:
        db: open connection to the target database
    """
    table_rows = db.execute(text(_TABLES_QUERY)).mappings().all()  # every table
    table_descriptions = {  # table -> its note, keyed by (schema, table)
        (row["schema_name"], row["table_name"]): row["description"]
        for row in db.execute(text(_TABLE_DESCRIPTIONS_QUERY)).mappings().all()
    }
    column_descriptions = {  # column -> its note, keyed by (schema, table, column)
        (row["schema_name"], row["table_name"], row["column_name"]): row["description"]
        for row in db.execute(text(_COLUMN_DESCRIPTIONS_QUERY)).mappings().all()
    }
    primary_keys = {  # set of columns that are a table's unique ID
        (row["schema_name"], row["table_name"], row["column_name"])
        for row in db.execute(text(_PRIMARY_KEYS_QUERY)).mappings().all()
    }
    allowed_values: dict[tuple[str, str, str], list[str]] = {}  # enum-like column -> the values its CHECK allows
    for row in db.execute(text(_CHECK_CONSTRAINTS_QUERY)).mappings().all():
        parsed = parse_enum_check(row["definition"])
        if parsed is not None:
            allowed_values[(row["schema_name"], row["table_name"], parsed[0])] = parsed[1]
    fk_rows = db.execute(text(_FOREIGN_KEYS_QUERY)).mappings().all()  # every table-to-table link
    foreign_key_columns = {(row["fk_schema"], row["fk_table"], row["fk_column"]) for row in fk_rows}  # set of columns that point to another table

    tables_by_key: dict[tuple[str, str], TableMetadata] = {  # the result we're building, keyed by (schema, table)
        (row["schema_name"], row["table_name"]): TableMetadata(
            schema_name=row["schema_name"],
            table_name=row["table_name"],
            description=table_descriptions.get((row["schema_name"], row["table_name"])),
        )
        for row in table_rows
    }

    for row in db.execute(text(_COLUMNS_QUERY)).mappings().all():
        key = (row["schema_name"], row["table_name"])  # which table this column belongs to
        table = tables_by_key.get(key)
        if table is None:
            continue
        column_key = (row["schema_name"], row["table_name"], row["column_name"])  # this column's unique lookup key
        table.columns.append(
            ColumnMetadata(
                name=row["column_name"],
                data_type=_format_data_type(row["type_name"], row["max_length"], row["precision"], row["scale"]),
                is_nullable=bool(row["is_nullable"]),
                is_primary_key=column_key in primary_keys,
                is_foreign_key=column_key in foreign_key_columns,
                ordinal_position=row["ordinal_position"],
                description=column_descriptions.get(column_key),
                allowed_values=allowed_values.get(column_key),
            )
        )

    relationships = [  # the table-to-table links, in the final output shape
        RelationshipMetadata(
            constraint_name=row["constraint_name"],
            fk_schema=row["fk_schema"],
            fk_table=row["fk_table"],
            fk_column=row["fk_column"],
            pk_schema=row["pk_schema"],
            pk_table=row["pk_table"],
            pk_column=row["pk_column"],
        )
        for row in fk_rows
    ]

    return IntrospectionResult(tables=list(tables_by_key.values()), relationships=relationships)
