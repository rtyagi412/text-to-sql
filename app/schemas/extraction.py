from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionRequest(BaseModel):
    ritm_number: str
    user_input: str | None = None
    model: str = Field(..., description="Ollama model tag, e.g. 'llama3.1:8b'")
    prompt_version: str | None = Field(
        default=None, description="Override the configured default extraction prompt version, e.g. 'v1'"
    )


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


class AmbiguityCategory(str, Enum):
    MISSING_ENTITY = "MISSING_ENTITY"
    MISSING_OUTPUT_FIELDS = "MISSING_OUTPUT_FIELDS"
    MISSING_REPORT_CRITERIA = "MISSING_REPORT_CRITERIA"
    AMBIGUOUS_FIELD = "AMBIGUOUS_FIELD"
    AMBIGUOUS_FILTER = "AMBIGUOUS_FILTER"
    OTHER = "OTHER"


class Status(str, Enum):
    READY = "READY"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


FilterValue = str | int | float | bool | list[str | int | float | bool] | None


class ExtractedEntity(BaseModel):
    """Normalized business entity, not a database table name."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    evidence: str = Field(..., min_length=1, description="Exact RITM text supporting the extracted entity.")


class ExtractedField(BaseModel):
    """Normalized requested business field, not a database column."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    evidence: str = Field(..., min_length=1, description="RITM text supporting the requested field.")


class ExtractedFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(..., min_length=1)
    operator: Operator
    evidence: str = Field(..., min_length=1, description="RITM text supporting the complete filter.")
    value: FilterValue = None


class Ambiguity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: AmbiguityCategory
    description: str = Field(..., min_length=1)
    clarification_question: str = Field(..., min_length=1)


class RitmExtractionData(BaseModel):
    """LLM output shape only — kept separate from ExtractionResult so response metadata doesn't leak into the model's schema."""

    model_config = ConfigDict(extra="forbid")

    ritm_number: str = Field(..., pattern=r"^RITM(-EVAL-)?[0-9]+$")
    entities: list[ExtractedEntity] = Field(default_factory=list, json_schema_extra={"uniqueItems": True})
    requested_fields: list[ExtractedField] = Field(default_factory=list)
    filters: list[ExtractedFilter] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    status: Status

    @model_validator(mode="after")
    def _validate_ambiguities_for_status(self) -> "RitmExtractionData":
        if self.status is Status.READY and self.ambiguities:
            raise ValueError("ambiguities must be empty when status is READY")
        if self.status is Status.NEEDS_CLARIFICATION and not self.ambiguities:
            raise ValueError("ambiguities must be non-empty when status is NEEDS_CLARIFICATION")
        return self

    @classmethod
    def generation_json_schema(cls) -> dict:
        """Schema for constraining LLM output (Ollama's `format` param).

        Fields like `entities`/`filters` have Python-side defaults so API consumers can
        omit them, which makes model_json_schema() mark them as not required. Under
        constrained decoding that gives smaller models an easy out to skip them entirely
        instead of extracting real content, so every top-level field is forced required
        here regardless of its Python default.
        """
        schema = cls.model_json_schema()
        schema["required"] = list(schema["properties"].keys())
        return schema


class ExtractionResult(RitmExtractionData):
    prompt_version: str = Field(default="", description="Prompt version used to produce this extraction")
