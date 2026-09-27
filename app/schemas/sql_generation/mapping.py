"""Step 1, /sql/map: the confirmed extraction resolved to real catalog columns.

ColumnMapping is the mapping itself. The model answers with ColumnMappingAnswer (a ColumnMapping that may instead ask to
see more tables first); the requester receives ColumnMappingResponse (a ColumnMapping plus status, matched RITMs and audit)."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.sql_generation.common import Clarification, FilterValue, GenerationAudit, MatchedRitm, Operator, Status
from app.schemas.sql_generation.extract import RequestedField, RequestedFilter

# The operators that can take a column on the right: the ones that compare one value with one value, and IN / NOT_IN,
# which test the column against the other column's values across the related rows ("owner is not one of the borrowers").
COLUMN_COMPARISON_OPERATORS = (
    Operator.EQUALS,
    Operator.NOT_EQUALS,
    Operator.GREATER_THAN,
    Operator.GREATER_THAN_OR_EQUAL,
    Operator.LESS_THAN,
    Operator.LESS_THAN_OR_EQUAL,
    Operator.IN,
    Operator.NOT_IN,
)


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


class ColumnRef(BaseModel):
    """A real column: a table as "schema.table" and a column of it."""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(..., description='Catalog table as "schema.table", e.g. "dbo.merchant".')
    column: str = Field(..., description="Exact column name from the provided schema.")


class DerivedValue(BaseModel):
    """A field no single column stores: a count, total or average, a label saying where a row came from. It is
    worked out from real columns, which are listed so the SQL step is given every table the value reads."""

    model_config = ConfigDict(extra="forbid")

    definition: str = Field(
        ...,
        min_length=1,
        description=(
            "How the value is computed, in a sentence or two a requester can check: the function (count, sum, ...), "
            "what it is taken over, the row it belongs to (one row per what) and what shows when nothing matches."
        ),
    )
    sources: list[ColumnRef] = Field(
        ...,
        min_length=1,
        description=(
            "Every column the value is computed from or counted over, each from <schema>: the foreign key column of "
            "each table counted (one entry per place counted), the amount column summed, and so on."
        ),
    )


class MappedField(BaseModel):
    """A requested field resolved to the real column that carries it, or, when no column stores it, to the value
    computed from real columns. Exactly one of (table, column) and `derived` is set."""

    model_config = ConfigDict(extra="forbid")

    requested: str = Field(..., description="The requested field's name, copied exactly from the confirmed request.")
    table: str | None = Field(
        default=None, description='Catalog table as "schema.table", e.g. "dbo.merchant". Null for a derived field.'
    )
    column: str | None = Field(default=None, description="Exact column name from the provided schema. Null for a derived field.")
    derived: DerivedValue | None = Field(
        default=None,
        description="Set, with table and column null, when the field is computed rather than stored in one column.",
    )

    @model_validator(mode="after")
    def _column_xor_derived(self) -> "MappedField":
        has_column = self.table is not None or self.column is not None
        if self.derived is not None and has_column:
            raise ValueError(f"field '{self.requested}': a derived field has table and column null; put its columns in derived.sources")
        if self.derived is None and (self.table is None or self.column is None):
            raise ValueError(f"field '{self.requested}': set table and column, or set derived when no single column carries it")
        return self

    @property
    def column_refs(self) -> list[ColumnRef]:
        """The real columns this field reads."""
        if self.derived is not None:
            return list(self.derived.sources)
        return [ColumnRef(table=self.table, column=self.column)]


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
            'windows stay "-N UNIT" (UNIT: MINUTES, HOURS, DAYS, WEEKS, MONTHS, YEARS). Null too when value_column is set.'
        ),
    )
    value_column: ColumnRef | None = Field(
        default=None,
        description=(
            "Set, with value null, when the condition compares the column with another column rather than a "
            "literal (the negotiated rate above the product's rate). The other column, from <schema>."
        ),
    )

    @model_validator(mode="after")
    def _value_or_column(self) -> "MappedFilter":
        if self.value_column is not None:
            if self.value is not None:
                raise ValueError(f"filter '{self.requested}': value_column is set, so value must be null")
            if self.operator not in COLUMN_COMPARISON_OPERATORS:
                allowed = ", ".join(op.value for op in COLUMN_COMPARISON_OPERATORS)
                raise ValueError(f"filter '{self.requested}': comparing with another column allows only {allowed}, not {self.operator.value}")
        return self

    @property
    def shown_value(self) -> FilterValue | str:
        """The value as a person would read it: the other column's name when the filter compares two columns."""
        if self.value_column is not None:
            return f"{self.value_column.table}.{self.value_column.column}"
        return self.value


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
