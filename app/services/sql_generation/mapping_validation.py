"""Checks of the model's column mapping against what it was actually shown.

Each `_check_*` returns the problems it finds as sentences addressed to the model, since they are sent back to it
for a correction. `validate_mapping` raises when any check found something."""

from app.schemas.sql_generation import ColumnMapping, ColumnMappingRequest
from app.services.approved_ritm_service import SimilarRitm
from app.services.llm_service import LlmOutputError
from app.services.schema_context_service import SchemaContext
from app.services.sql_generation.common import filter_problems


def validate_mapping(
    mapping: ColumnMapping, request: ColumnMappingRequest, schema: SchemaContext, examples: list[SimilarRitm]
) -> None:
    """Checks the model's answer against what it was actually shown (`schema` and `examples`). A made-up column,
    a value the column can't hold, a dropped field or a citation of an example it never saw would all read as
    plausible in the response, so they are rejected here rather than left for the SQL step to trip over."""
    problems = [
        *_check_columns_exist(mapping, schema),
        *_check_filter_values(mapping, schema),
        *_check_coverage(mapping, request),
        *_check_citations(mapping, examples),
    ]
    if problems:
        raise LlmOutputError("The column mapping was rejected: " + "; ".join(problems))


def _check_columns_exist(mapping: ColumnMapping, schema: SchemaContext) -> list[str]:
    """Every table and column the mapping names is one the model was shown."""
    picks = [
        *((f"field '{m.requested}'", m.table, m.column) for m in mapping.output_fields),
        *((f"filter '{m.requested}'", m.table, m.column) for m in mapping.filters),
        *((f"additional filter on {a.table}.{a.column}", a.table, a.column) for a in mapping.additional_filters),
    ]
    problems: list[str] = []
    for label, table, column in picks:
        known = schema.columns.get(table)
        if known is None:
            problems.append(f"{label}: table '{table}' is not in the schema provided")
        elif column not in known:
            problems.append(f"{label}: '{table}' has no column '{column}'")
    return problems


def _check_filter_values(mapping: ColumnMapping, schema: SchemaContext) -> list[str]:
    """Each filter's operator and value fit its column's type and, where it has one, the values its CHECK allows."""
    return [
        *(
            problem
            for m in mapping.filters
            for problem in filter_problems(f"filter '{m.requested}' ({m.table}.{m.column})", m, schema)
        ),
        *(
            problem
            for a in mapping.additional_filters
            for problem in filter_problems(f"additional filter on {a.table}.{a.column}", a, schema)
        ),
    ]


def _check_coverage(mapping: ColumnMapping, request: ColumnMappingRequest) -> list[str]:
    """The mapping answers exactly what was confirmed: nothing invented, and nothing dropped unless the model
    asked a clarification instead."""
    requested_fields = {f.name for f in request.output_fields}
    requested_filters = {f.field for f in request.filters}
    mapped_fields = {m.requested for m in mapping.output_fields}
    mapped_filters = {m.requested for m in mapping.filters}

    problems = [
        *(f"field '{name}' was not in the confirmed request" for name in sorted(mapped_fields - requested_fields)),
        *(f"filter '{name}' was not in the confirmed request" for name in sorted(mapped_filters - requested_filters)),
    ]
    if not mapping.clarifications:
        problems += [
            *(f"field '{name}' was left unmapped without a clarification" for name in sorted(requested_fields - mapped_fields)),
            *(f"filter '{name}' was left unmapped without a clarification" for name in sorted(requested_filters - mapped_filters)),
        ]
    return problems


def _check_citations(mapping: ColumnMapping, examples: list[SimilarRitm]) -> list[str]:
    """Additional filters and considerations cite only approved RITMs the model was actually shown."""
    shown = {e.ritm.number for e in examples}
    cited_by = [
        *((f"additional filter on {a.table}.{a.column}", a.source_ritms) for a in mapping.additional_filters),
        *((f"consideration '{c.text[:40]}'", c.source_ritms) for c in mapping.considerations),
    ]
    return [
        f"{label} cites {cited or 'no RITM'}; the examples shown were {sorted(shown) or 'none'}"
        for label, cited in cited_by
        if not cited or not set(cited) <= shown
    ]
