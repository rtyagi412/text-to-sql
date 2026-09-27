"""Step 2, /sql/write: one validated T-SQL SELECT for the confirmed mapping."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.sql_generation.common import Clarification, GenerationAudit, MatchedRitm, Status
from app.schemas.sql_generation.mapping import AdditionalFilter, Consideration, MappedField, MappedFilter


class SqlWriteRequest(BaseModel):
    """The requester-confirmed column mapping. POST the /sql/map response back, after deleting anything the
    requester rejected: every entry in `additional_filters` is applied to the query, so delete the proposals
    they did not accept."""

    ritm_number: str
    output_fields: list[MappedField]
    filters: list[MappedFilter]
    additional_filters: list[AdditionalFilter] = Field(
        default_factory=list, description="Only the proposed extra filters the requester accepted."
    )
    considerations: list[Consideration] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    model: str | None = Field(default=None, description="Model ID, e.g. deepseek-flash or deepseek-v4-pro; defaults to the configured LLM_MODEL")
    prompt_version: str | None = Field(
        default=None, description="Override the configured default write prompt version, e.g. 'write-v1'"
    )


class SqlWrite(BaseModel):
    """The model's output for the write stage. `status` is derived from `clarifications`, never taken from the model."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Brief working: the join path chosen, which tables are joined and which are tested with EXISTS, and how each filter is written. Written before the SQL it justifies.",
    )
    sql: str | None = Field(
        default=None, description="One read-only T-SQL SELECT statement. Null exactly when clarifications is non-empty."
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Non-blocking interpretations made while writing the SQL, shown to the requester."
    )
    clarifications: list[Clarification] = Field(
        default_factory=list, description="Empty unless the SQL genuinely cannot be written without the answer."
    )

    def _asks_for_tables(self) -> bool:
        return False

    @model_validator(mode="after")
    def _check_sql_matches_clarifications(self) -> "SqlWrite":
        if self.clarifications:
            self.sql = None
        elif not (self.sql and self.sql.strip()) and not self._asks_for_tables():
            raise ValueError("sql must be non-empty when there are no clarifications")
        return self

    def derive_status(self) -> Status:
        return Status.NEEDS_CLARIFICATION if self.clarifications else Status.READY

    @classmethod
    def generation_json_schema(cls) -> dict:
        return cls.model_json_schema()


class SqlWriteAnswer(SqlWrite):
    """What the model returns for the write stage: a SqlWrite, or a request to see more of the schema first.
    `tables_needed` is not part of the response to the requester; the service acts on it and drops it."""

    tables_needed: list[str] = Field(
        default_factory=list,
        description=(
            'Tables from <table_index>, as "schema.table", whose columns must be shown before the SQL can be written. '
            "Empty when <schema> is enough. When non-empty the tables are added and you are asked again; the rest of "
            "this answer is discarded, so leave sql null and clarifications empty."
        ),
    )

    def _asks_for_tables(self) -> bool:
        return bool(self.tables_needed)


class SqlWriteResponse(SqlWrite):
    ritm_number: str
    status: Status
    tables: list[str] = Field(default_factory=list, description="Tables the SQL reads, as \"schema.table\".")
    warnings: list[str] = Field(
        default_factory=list,
        description="Performance smells found in the SQL (non-sargable predicates, leading wildcards, DISTINCT). Not errors.",
    )
    compiled: bool = Field(
        default=False,
        description=(
            "True when SQL Server compiled the query (sp_describe_first_result_set: nothing was run) and its output "
            "columns are the requested ones. False when the check was switched off or the server could not be reached."
        ),
    )
    matched_ritms: list[MatchedRitm]
    audit: GenerationAudit
