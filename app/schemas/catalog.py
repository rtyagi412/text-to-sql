from pydantic import BaseModel, ConfigDict, Field


class ColumnMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool
    is_foreign_key: bool
    ordinal_position: int
    description: str | None = None
    allowed_values: list[str] | None = Field(
        default=None, description="Every value a CHECK constraint lets this column hold; null when it isn't an enumeration."
    )


class TableMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_name: str
    table_name: str
    description: str | None = None
    columns: list[ColumnMetadata] = Field(default_factory=list)


class RelationshipMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_name: str
    fk_schema: str
    fk_table: str
    fk_column: str
    pk_schema: str
    pk_table: str
    pk_column: str


class IntrospectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableMetadata] = Field(default_factory=list)
    relationships: list[RelationshipMetadata] = Field(default_factory=list)


class SyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables_synced: int
    columns_synced: int
    relationships_synced: int
    embedding_model: str


class TableSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: int
    schema_name: str
    table_name: str
    description: str | None = None
    keyword_score: float
    semantic_score: float
    hybrid_score: float


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[TableSearchResult] = Field(default_factory=list)


class JoinEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_schema: str
    from_table: str
    from_column: str
    to_schema: str
    to_table: str
    to_column: str
    constraint_name: str


class AmbiguousJoinPath(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_a: str
    table_b: str
    path_options: list[list[JoinEdge]] = Field(default_factory=list)


class JoinPathResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[str] = Field(default_factory=list)
    edges: list[JoinEdge] = Field(default_factory=list)
    unreachable_tables: list[str] = Field(default_factory=list)
    ambiguous_joins: list[AmbiguousJoinPath] = Field(default_factory=list)


class JoinPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[str] = Field(..., min_length=1, description='"schema.table" strings, e.g. "dbo.merchant"')
