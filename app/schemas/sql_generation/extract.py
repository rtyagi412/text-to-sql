"""Step 0, /sql/extract: what the ticket asks the report to display and filter on, in business terms."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.sql_generation.common import Clarification, FilterValue, LlmCallAudit, Operator, Status


class ExtractionRequest(BaseModel):
    ritm_number: str
    user_input: str | None = Field(
        default=None, description="Requester's answer to an earlier clarification question, if any"
    )
    model: str | None = Field(default=None, description="Model ID, e.g. deepseek-flash or deepseek-v4-pro; defaults to the configured LLM_MODEL")
    prompt_version: str | None = Field(
        default=None, description="Override the configured default extract prompt version, e.g. 'extract-v1'"
    )


class TicketSource(str, Enum):
    """Which part of the request an extracted item comes from."""

    OUTPUT_FIELDS = "OUTPUT_FIELDS"
    REPORT_CRITERIA = "REPORT_CRITERIA"
    DESCRIPTION = "DESCRIPTION"
    REQUESTER_CLARIFICATION = "REQUESTER_CLARIFICATION"


_SOURCE_DESCRIPTION = (
    "Where the item is stated. When more than one part states it, the structured field "
    "(OUTPUT_FIELDS / REPORT_CRITERIA) wins over DESCRIPTION; REQUESTER_CLARIFICATION only for what the "
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
    """The model's output for the extraction stage: what the ticket asks the report to show and filter on, read
    from the ticket text alone. This is what the requester confirms before any catalog lookup happens.
    As elsewhere, `status` is derived from `clarifications`, never taken from the model."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(
        ...,
        description="Brief working: how output_fields, report_criteria and description together split into fields and conditions. Written before the fields and filters it justifies.",
    )
    output_fields: list[RequestedField] = Field(default_factory=list)
    filters: list[RequestedFilter] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list, description="Non-blocking interpretations made while reading the ticket, shown to the requester."
    )
    clarifications: list[Clarification] = Field(
        default_factory=list, description="Empty unless the fields or filters genuinely cannot be decided without the answer."
    )

    def derive_status(self) -> Status:
        return Status.NEEDS_CLARIFICATION if self.clarifications else Status.READY

    @classmethod
    def generation_json_schema(cls) -> dict:
        return cls.model_json_schema()


class RitmExtractionResponse(RitmExtraction):
    ritm_number: str
    status: Status
    audit: LlmCallAudit
