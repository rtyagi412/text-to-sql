"""Types every step shares: enums, the filter value, clarifications, and the audit that records what produced an answer."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


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

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(**{name: getattr(self, name) + getattr(other, name) for name in TokenUsage.model_fields})


class LlmCallAudit(BaseModel):
    """What produced this answer -- enough to reproduce or explain it later."""

    model_config = ConfigDict(extra="forbid")

    prompt_version: str
    model: str
    request_id: str | None = None
    attempts: int = Field(default=1, description="Model calls made: more than 1 when a reply was unusable, an answer was rejected and corrected, or the model asked to see more tables.")
    usage: TokenUsage = Field(..., description="Summed over every call that returned an answer.")


class GenerationAudit(LlmCallAudit):
    """Audit for the stages that read the catalog: also records exactly what schema context the model saw."""

    schema_snapshot: str = Field(..., description="Short hash of the exact schema text the model was shown.")
    tables_in_context: list[str] = Field(default_factory=list)
    example_ritms: list[str] = Field(default_factory=list, description="Approved RITMs shown as references.")
    tables_requested: list[str] = Field(
        default_factory=list,
        description="Tables the model asked to see beyond the retrieved schema; they were added and the request re-run.",
    )


class MatchedRitm(BaseModel):
    """An approved RITM that was similar enough to inform the mapping, so the requester can see what it drew on."""

    model_config = ConfigDict(extra="forbid")

    number: str
    similarity: float
    output_fields: str
    report_criteria: str
