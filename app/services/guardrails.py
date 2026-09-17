import re
from collections.abc import Callable
from dataclasses import dataclass

from app.schemas.extraction import (
    Ambiguity,
    AmbiguityCategory,
    RitmExtractionData,
    Status,
)
from app.schemas.ritm import Ritm


def _is_explicit(value: str | None) -> bool:
    return value is not None and value != ""


_UNIT_WORD_TO_LABEL: dict[str, str] = {
    "minute": "MINUTES",
    "hour": "HOURS",
    "day": "DAYS",
    "week": "WEEKS",
    "month": "MONTHS",
    "year": "YEARS",
}

_RELATIVE_TIME_RANGE_PATTERN = re.compile(
    r"(?:last|past)\s+(\d+)\s+(minute|hour|day|week|month|year)s?", re.IGNORECASE
)


def _relative_offset_literal(evidence: str) -> str | None:
    """Normalize phrasing like 'last 30 days' / 'past 6 months' into a '-N UNIT' filter value.

    The LLM sometimes turns this into a filter with an invented absolute date instead of
    the relative offset the RITM text actually supports. When the filter's own evidence
    matches this pattern, the deterministic reading wins over whatever the LLM produced.
    """
    match = _RELATIVE_TIME_RANGE_PATTERN.search(evidence)
    if match is None:
        return None
    amount = int(match.group(1))
    unit = _UNIT_WORD_TO_LABEL[match.group(2).lower()]
    return f"-{amount} {unit}"


def _normalize_relative_time_filters(data: RitmExtractionData) -> RitmExtractionData:
    changed = False
    filters = []
    for f in data.filters:
        literal = _relative_offset_literal(f.evidence)
        if literal is not None and f.value != literal:
            filters.append(f.model_copy(update={"value": literal}))
            changed = True
        else:
            filters.append(f)
    if not changed:
        return data
    return data.model_copy(update={"filters": filters})


@dataclass(frozen=True)
class FieldGuardrail:
    category: AmbiguityCategory
    get_raw_value: Callable[[Ritm], str]
    is_resolved_in_extraction: Callable[[RitmExtractionData], bool]
    description: str
    clarification_question: str


_GUARDRAILS: list[FieldGuardrail] = [
    FieldGuardrail(
        category=AmbiguityCategory.MISSING_OUTPUT_FIELDS,
        get_raw_value=lambda ritm: ritm.variables.output_fields,
        is_resolved_in_extraction=lambda data: bool(data.requested_fields),
        description="The RITM does not state which fields/columns the report should include.",
        clarification_question="Which fields/columns should this report include?",
    ),
    FieldGuardrail(
        category=AmbiguityCategory.MISSING_REPORT_CRITERIA,
        get_raw_value=lambda ritm: ritm.variables.report_criteria,
        is_resolved_in_extraction=lambda data: bool(data.filters),
        description="The RITM does not state the filtering criteria/population for this report.",
        clarification_question="What criteria should determine which records are included in this report?",
    ),
]


def apply_ambiguity_guardrails(ritm: Ritm, data: RitmExtractionData) -> RitmExtractionData:
    """Deterministically cross-check the LLM's self-reported ambiguities against the raw RITM.

    A field is "explicit" when it was passed in the request and is not null/empty
    (see _is_explicit). For each guardrailed category:
    - If the raw field is explicit, any LLM-reported ambiguity in that category is a
      hallucination against data it was actually given, so it is dropped.
    - Otherwise, if the raw field is not explicit but the LLM still populated the
      corresponding extracted field (e.g. resolved via a user_input clarification),
      the ambiguity is considered answered and is dropped too.
    - Otherwise, an ambiguity is force-added when the LLM failed to report one.
    """
    data = _normalize_relative_time_filters(data)
    ambiguities = list(data.ambiguities)

    for guardrail in _GUARDRAILS:
        raw_value = guardrail.get_raw_value(ritm)
        explicit = _is_explicit(raw_value)
        resolved = explicit or guardrail.is_resolved_in_extraction(data)

        if resolved:
            ambiguities = [a for a in ambiguities if a.category != guardrail.category]
            continue

        already_flagged = any(a.category == guardrail.category for a in ambiguities)
        if not already_flagged:
            ambiguities.append(
                Ambiguity(
                    category=guardrail.category,
                    description=guardrail.description,
                    clarification_question=guardrail.clarification_question,
                )
            )

    status = Status.NEEDS_CLARIFICATION if ambiguities else Status.READY

    updated = data.model_dump()
    updated["ambiguities"] = [a.model_dump() for a in ambiguities]
    updated["status"] = status.value
    return RitmExtractionData.model_validate(updated)
