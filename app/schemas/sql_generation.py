from enum import Enum

import anthropic
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Operator(str, Enum):
    EQUALS = "EQUALS"
    NOT_EQUALS = "NOT_EQUALS"
    IN = "IN"
    NOT_IN = "NOT_IN"
    GREATER_THAN = "GREATER_THAN"
    GREATER_THAN_OR_EQUAL = "GREATER_THAN_OR_EQUAL"
    LESS_THAN = "LESS_THAN"
    LESS_THAN_OR_EQUAL = "LESS_THAN_OR_EQUAL"
    BETWEEN = "BETWEEN"
    CONTAINS = "CONTAINS"
    STARTS_WITH = "STARTS_WITH"
    ENDS_WITH = "ENDS_WITH"
    IS_NULL = "IS_NULL"
    IS_NOT_NULL = "IS_NOT_NULL"


class Status(str, Enum):
    READY = "READY"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


FilterValue = str | int | float | bool | list[str | int | float | bool] | None


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., description="Question to put to the requester.")
    reason: str = Field(..., description="What is missing, and why the SQL cannot be written without it.")


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ClaudeCallAudit(BaseModel):
    """What produced this answer -- enough to reproduce or explain it later."""

    model_config = ConfigDict(extra="forbid")

    prompt_version: str
    model: str
    request_id: str | None = None
    usage: TokenUsage


class GenerationAudit(ClaudeCallAudit):
    """Audit for the stages that read the catalog: also records exactly what schema context Claude saw."""

    schema_snapshot: str = Field(..., description="Short hash of the exact schema text Claude was shown.")
    glossary_snapshot: str = Field(..., description="Short hash of the business glossary Claude was shown.")
    tables_in_context: list[str] = Field(default_factory=list)
    example_ritms: list[str] = Field(default_factory=list, description="Approved RITMs shown as references.")


class ExtractionRequest(BaseModel):
    ritm_number: str
    user_input: str | None = Field(
        default=None, description="Requester's answer to an earlier clarification question, if any"
    )
    model: str | None = Field(default=None, description="Claude model ID; defaults to the configured CLAUDE_MODEL")
    prompt_version: str | None = Field(
        default=None, description="Override the configured default extract prompt version, e.g. 'extract-v1'"
    )


class TicketSource(str, Enum):
    """Which part of the request an extracted item comes from."""

    OUTPUT_FIELDS = "OUTPUT_FIELDS"
    REPORT_CRITERIA = "REPORT_CRITERIA"
    SUMMARY = "SUMMARY"
    REQUESTER_CLARIFICATION = "REQUESTER_CLARIFICATION"


_SOURCE_DESCRIPTION = (
    "Where the item is stated. When more than one part states it, the structured field "
    "(OUTPUT_FIELDS / REPORT_CRITERIA) wins over SUMMARY; REQUESTER_CLARIFICATION only for what the "
    "requester's answer added or changed."
)


class RequestedField(BaseModel):
    """A field the ticket asks the report to display, in the ticket's own words -- not yet a catalog column."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="The field as the ticket words it, e.g. 'Merchant Name'.")
    source: TicketSource = Field(..., description=_SOURCE_DESCRIPTION)
    evidence: str = Field(..., description="Exact ticket text that requests this field.")


class RequestedFilter(BaseModel):
    """A condition the ticket puts on the report's records, in business terms -- not yet a catalog column."""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(
        ..., description="What is being constrained, in business words, e.g. 'Payment status' or 'Failed at'."
    )
    operator: Operator
    value: FilterValue = Field(
        ...,
        description=(
            "The literal(s) as the ticket states them, not converted to any storage form. Null only for IS_NULL / "
            'IS_NOT_NULL. Relative time windows are written "-N UNIT" (UNIT: MINUTES, HOURS, DAYS, WEEKS, MONTHS, YEARS).'
        ),
    )
    source: TicketSource = Field(..., description=_SOURCE_DESCRIPTION)
    evidence: str = Field(..., description="Exact ticket text that states this condition.")


class RitmExtraction(BaseModel):
    """Claude's output for the extraction stage: what the ticket asks the report to show and filter on, read
    from the ticket text alone. This is what the requester confirms before any catalog lookup happens.
    As elsewhere, `status` is derived from `clarifications`, never taken from the model."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Brief working: how output_fields, report_criteria and summary together split into fields and conditions. Written before the fields and filters it justifies.",
    )
    output_fields: list[RequestedField]
    filters: list[RequestedFilter]
    assumptions: list[str] = Field(
        ..., description="Non-blocking interpretations made while reading the ticket, shown to the requester."
    )
    clarifications: list[Clarification] = Field(
        ..., description="Empty unless the fields or filters genuinely cannot be decided without the answer."
    )

    def derive_status(self) -> Status:
        return Status.NEEDS_CLARIFICATION if self.clarifications else Status.READY

    @classmethod
    def generation_json_schema(cls) -> dict:
        return anthropic.transform_schema(cls)


class RitmExtractionResponse(RitmExtraction):
    ritm_number: str
    status: Status
    audit: ClaudeCallAudit


class ColumnMappingRequest(BaseModel):
    """The requester-confirmed extraction. POST the /sql/extract response back as it is, or with the
    requester's edits: fields it doesn't need (status, audit, reasoning, clarifications) are ignored."""

    ritm_number: str
    output_fields: list[RequestedField]
    filters: list[RequestedFilter]
    assumptions: list[str] = Field(
        default_factory=list, description="The extraction's assumptions, as the requester saw them."
    )
    model: str | None = Field(default=None, description="Claude model ID; defaults to the configured CLAUDE_MODEL")
    prompt_version: str | None = Field(
        default=None, description="Override the configured default mapping prompt version, e.g. 'mapping-v1'"
    )


class MappedField(BaseModel):
    """A requested field resolved to the real column that carries it."""

    model_config = ConfigDict(extra="forbid")

    requested: str = Field(..., description="The requested field's name, copied exactly from the confirmed request.")
    table: str = Field(..., description='Catalog table as "schema.table", e.g. "dbo.merchant".')
    column: str = Field(..., description="Exact column name from the provided schema.")


class MappedFilter(BaseModel):
    """A requested condition resolved to the real column it constrains, with the value in stored form."""

    model_config = ConfigDict(extra="forbid")

    requested: str = Field(..., description="The requested filter's field, copied exactly from the confirmed request.")
    table: str = Field(..., description='Catalog table as "schema.table", e.g. "dbo.merchant".')
    column: str = Field(..., description="Exact column name from the provided schema.")
    operator: Operator
    value: FilterValue = Field(
        ...,
        description=(
            "The confirmed value written the way this column stores it (glossary and schema conventions: "
            "UPPER_SNAKE_CASE categories, integer minor units). Null only for IS_NULL / IS_NOT_NULL. Relative "
            'windows stay "-N UNIT" (UNIT: MINUTES, HOURS, DAYS, WEEKS, MONTHS, YEARS).'
        ),
    )


class AdditionalFilter(BaseModel):
    """A condition that similar approved queries apply but the ticket does not state. A proposal for the
    requester to accept or drop, not something already in the report."""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(..., description='Catalog table as "schema.table", e.g. "dbo.merchant".')
    column: str = Field(..., description="Exact column name from the provided schema.")
    operator: Operator
    value: FilterValue = Field(
        ..., description="Literal(s) in stored form, as the approved SQL uses them. Null only for IS_NULL / IS_NOT_NULL."
    )
    rationale: str = Field(..., description="Why queries like this one carry it, in a sentence a requester can judge.")
    source_ritms: list[str] = Field(
        ..., description="Numbers of the approved examples whose SQL applies it. Only numbers shown in <approved_examples>."
    )


class Consideration(BaseModel):
    """How similar approved queries handle something that changes this report's rows or figures, and that is
    not itself a filter: a join path, one-to-many handling, unit or time conventions, a resolved ambiguity."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., description="The consideration, stated plainly.")
    source_ritms: list[str] = Field(
        ..., description="Numbers of the approved examples that show it. Only numbers shown in <approved_examples>."
    )


class ColumnMapping(BaseModel):
    """Claude's output for the mapping stage. `status` is derived from `clarifications`, never taken from the model."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Brief working: which column carries each requested field and condition, and what the similar approved queries add. Written before the mappings it justifies.",
    )
    output_fields: list[MappedField]
    filters: list[MappedFilter]
    additional_filters: list[AdditionalFilter] = Field(
        ..., description="Empty when no similar approved query applies a condition the ticket leaves unstated."
    )
    considerations: list[Consideration] = Field(
        ..., description="Empty when the approved examples add nothing beyond the mapping."
    )
    assumptions: list[str] = Field(
        ..., description="Non-blocking interpretations made while mapping, shown to the requester."
    )
    clarifications: list[Clarification] = Field(
        ..., description="Empty unless a field or condition genuinely cannot be mapped without the answer."
    )

    def derive_status(self) -> Status:
        return Status.NEEDS_CLARIFICATION if self.clarifications else Status.READY

    @classmethod
    def generation_json_schema(cls) -> dict:
        return anthropic.transform_schema(cls)


class MatchedRitm(BaseModel):
    """An approved RITM that was similar enough to inform the mapping, so the requester can see what it drew on."""

    model_config = ConfigDict(extra="forbid")

    number: str
    similarity: float
    output_fields: str
    report_criteria: str


class ColumnMappingResponse(ColumnMapping):
    ritm_number: str
    status: Status
    matched_ritms: list[MatchedRitm]
    audit: GenerationAudit


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
    model: str | None = Field(default=None, description="Claude model ID; defaults to the configured CLAUDE_MODEL")
    prompt_version: str | None = Field(
        default=None, description="Override the configured default write prompt version, e.g. 'write-v1'"
    )


class SqlWrite(BaseModel):
    """Claude's output for the write stage. `status` is derived from `clarifications`, never taken from the model."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Brief working: the join path chosen, which tables are joined and which are tested with EXISTS, and how each filter is written. Written before the SQL it justifies.",
    )
    sql: str | None = Field(
        ..., description="One read-only T-SQL SELECT statement. Null exactly when clarifications is non-empty."
    )
    assumptions: list[str] = Field(
        ..., description="Non-blocking interpretations made while writing the SQL, shown to the requester."
    )
    clarifications: list[Clarification] = Field(
        ..., description="Empty unless the SQL genuinely cannot be written without the answer."
    )

    @model_validator(mode="after")
    def _check_sql_matches_clarifications(self) -> "SqlWrite":
        if self.clarifications:
            self.sql = None
        elif not (self.sql and self.sql.strip()):
            raise ValueError("sql must be non-empty when there are no clarifications")
        return self

    def derive_status(self) -> Status:
        return Status.NEEDS_CLARIFICATION if self.clarifications else Status.READY

    @classmethod
    def generation_json_schema(cls) -> dict:
        return anthropic.transform_schema(cls)


class SqlWriteResponse(SqlWrite):
    ritm_number: str
    status: Status
    tables: list[str] = Field(default_factory=list, description="Tables the SQL reads, as \"schema.table\".")
    warnings: list[str] = Field(
        default_factory=list,
        description="Performance smells found in the SQL (non-sargable predicates, leading wildcards, DISTINCT). Not errors.",
    )
    matched_ritms: list[MatchedRitm]
    audit: GenerationAudit


class ApproveSqlRequest(BaseModel):
    ritm_number: str
    sql: str = Field(..., description="The reviewed SQL for this RITM, as returned by /sql/write or edited by hand.")


class ApproveSqlResponse(BaseModel):
    ritm_number: str
    sql: str = Field(..., description="The SQL as stored: validated and formatted.")
    tables: list[str]
    warnings: list[str]
    replaced: bool = Field(..., description="True when the RITM was already in the approved pool and was overwritten.")
