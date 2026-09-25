"""Step 1, /sql/map: the confirmed extraction resolved to real catalog columns.

ColumnMapping is the mapping itself. The model answers with ColumnMappingAnswer (a ColumnMapping that may instead ask to
see more tables first); the requester receives ColumnMappingResponse (a ColumnMapping plus status, matched RITMs and audit)."""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.sql_generation.common import Clarification, FilterValue, GenerationAudit, MatchedRitm, Operator, Status
from app.schemas.sql_generation.extract import RequestedField, RequestedFilter


class ColumnMappingRequest(BaseModel):
    """The requester-confirmed extraction. POST the /sql/extract response back as it is, or with the
    requester's edits: fields it doesn't need (status, audit, reasoning, clarifications) are ignored."""

    ritm_number: str
    output_fields: list[RequestedField]
    filters: list[RequestedFilter]
    assumptions: list[str] = Field(
        default_factory=list, description="The extraction's assumptions, as the requester saw them."
    )
    model: str | None = Field(default=None, description="Model ID, e.g. deepseek-flash or deepseek-v4-pro; defaults to the configured LLM_MODEL")
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
    """The model's output for the mapping stage. `status` is derived from `clarifications`, never taken from the model."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Brief working: which column carries each requested field and condition, and what the similar approved queries add. Written before the mappings it justifies.",
    )
    output_fields: list[MappedField] = Field(default_factory=list)
    filters: list[MappedFilter] = Field(default_factory=list)
    additional_filters: list[AdditionalFilter] = Field(
        default_factory=list, description="Empty when no similar approved query applies a condition the ticket leaves unstated."
    )
    considerations: list[Consideration] = Field(
        default_factory=list, description="Empty when the approved examples add nothing beyond the mapping."
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Non-blocking interpretations made while mapping, shown to the requester."
    )
    clarifications: list[Clarification] = Field(
        default_factory=list, description="Empty unless a field or condition genuinely cannot be mapped without the answer."
    )

    def derive_status(self) -> Status:
        return Status.NEEDS_CLARIFICATION if self.clarifications else Status.READY

    @classmethod
    def generation_json_schema(cls) -> dict:
        return cls.model_json_schema()


class ColumnMappingAnswer(ColumnMapping):
    """What the model returns for the mapping stage: a ColumnMapping, or a request to see more of the schema first.
    `tables_needed` is not part of the response to the requester; the service acts on it and drops it."""

    tables_needed: list[str] = Field(
        default_factory=list,
        description=(
            'Tables from <table_index>, as "schema.table", whose columns must be shown before the request can be '
            "mapped. Empty when <schema> is enough. When non-empty the tables are added and you are asked again; "
            "the rest of this answer is discarded, so leave output_fields and filters empty."
        ),
    )


class ColumnMappingResponse(ColumnMapping):
    status: Status
    matched_ritms: list[MatchedRitm]
    audit: GenerationAudit
    ritm_number: str
