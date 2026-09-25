"""Helpers shared by the extract, map and write stages: prompt assembly, the model-call retry loop, audit."""

import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import TypeVar

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.prompts.models import PromptVersion
from app.prompts.registry import get_prompt_version
from app.schemas.ritm import Ritm
from app.schemas.sql_generation import (
    AdditionalFilter,
    GenerationAudit,
    MappedFilter,
    MatchedRitm,
    Operator,
    TokenUsage,
)
from app.services import approved_ritm_service, filter_check_service, llm_service, schema_context_service
from app.services.approved_ritm_service import SimilarRitm
from app.services.llm_service import LlmJsonResult, LlmOutputError
from app.services.schema_context_service import SchemaContext

settings = get_settings()
logger = logging.getLogger(__name__)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONG_NUMBER = re.compile(r"\b\d{9,}\b")


def redact(text: str) -> str:
    """Masks emails and long digit runs (phone / account numbers). Applied to the RITM `description` only:
    output_fields and report_criteria carry the literal values the SQL depends on and must reach the model
    untouched. The description can drive fields and filters too, so a literal that appears only there (a
    long customer or account number) is masked and cannot become a filter value; set
    REDACT_RITM_DESCRIPTION=false if that matters. `report_usage` (cadence, audience, format) never reaches
    the model."""
    return _LONG_NUMBER.sub("[number]", _EMAIL.sub("[email]", text))


def _ticket_description(ritm: Ritm) -> str:
    return redact(ritm.variables.description) if settings.redact_ritm_description else ritm.variables.description


def system_blocks(system_prompt: str, table_index: str = "") -> str:
    """The system prompt, static-first: the rules and the table index change rarely, and the provider caches
    the longest prefix that repeats between requests, so keeping them ahead of anything that varies lets every
    request of a stage reuse it. Everything per-ticket goes in the user turn."""
    parts = [system_prompt]
    if table_index:
        parts.append(f"<table_index>\n{table_index}\n</table_index>")
    return "\n\n".join(parts)


def _render_example(similar: SimilarRitm) -> str:
    ritm = similar.ritm
    ticket = json.dumps({"output_fields": ritm.output_fields, "report_criteria": ritm.report_criteria}, indent=2)
    if ritm.sql:
        outcome = f"<approved_sql>\n{ritm.sql.strip()}\n</approved_sql>"
    else:
        questions = "\n".join(f"- {q}" for q in ritm.clarifications)
        outcome = f"<approved_outcome>Clarification needed; no SQL. Questions:\n{questions}\n</approved_outcome>"
    return f'<example ritm="{ritm.number}" similarity="{similar.similarity:.2f}">\n<ticket>\n{ticket}\n</ticket>\n{outcome}\n</example>'


def ticket_sections(ritm: Ritm, user_input: str | None) -> list[str]:
    """The ticket itself, plus the requester's clarification when there is one. Shared by every stage."""
    ticket = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": ritm.variables.output_fields,
        "report_criteria": ritm.variables.report_criteria,
        "description": _ticket_description(ritm),
    }
    sections = [f"<ritm>\n{json.dumps(ticket, indent=2)}\n</ritm>"]
    if user_input:
        sections.append(f"<requester_clarification>\n{user_input}\n</requester_clarification>")
    return sections


def reference_sections(schema_text: str, examples: list[SimilarRitm]) -> list[str]:
    sections = [f"<schema>\n{schema_text}\n</schema>"]
    if examples:
        sections.append("<approved_examples>\n" + "\n".join(_render_example(e) for e in examples) + "\n</approved_examples>")
    return sections


def resolve_version(prompt_version: str | None, default: str, stage: str) -> PromptVersion:
    version_id = prompt_version or default
    version = get_prompt_version(version_id, stage)
    if version is None:
        raise ValueError(f"Unknown prompt version '{version_id}' for the {stage} stage")
    return version


def similar_examples(
    output_fields: str, report_criteria: str, exclude_number: str, catalog_db: Session
) -> list[SimilarRitm]:
    similar = approved_ritm_service.find_similar(output_fields, report_criteria, exclude_number)
    # An approved solution that references a table the catalog no longer has is stale: showing it would
    # teach the model a schema that doesn't exist.
    existing = schema_context_service.existing_table_keys(catalog_db)
    return [s for s in similar if all(t in existing for t in s.ritm.tables)]


def add_usage(a: TokenUsage, b: TokenUsage) -> TokenUsage:
    return TokenUsage(**{name: getattr(a, name) + getattr(b, name) for name in TokenUsage.model_fields})


T = TypeVar("T")


def generate_checked(
    *, system: str, user_content: str, schema: dict, model: str | None, build: Callable[[dict], T]
) -> tuple[T, LlmJsonResult]:
    """Runs the model and `build`s its answer, which raises LlmOutputError (or a pydantic ValidationError) when the
    answer is unusable. JSON mode cannot enforce the schema, so an unusable answer is expected now and then: it is
    sent back once with the reasons, since a wrong column, value or field is usually fixed by pointing at it. A call
    that returns nothing usable (empty, cut off, not JSON) is simply asked again. A second failure is final. The
    result carries every answered call's token usage and the number of calls made."""
    prior: str | None = None
    feedback: str | None = None
    total: LlmJsonResult | None = None
    calls = 0
    for attempt in (1, 2):
        calls += 1
        try:
            result = llm_service.generate_json(
                system=system,
                user_content=user_content,
                schema=schema,
                model=model,
                prior_answer=prior,
                feedback=feedback,
            )
        except LlmOutputError as exc:
            if attempt == 2:
                raise
            logger.info("no usable reply, asking again: %s", exc)
            prior, feedback = exc.raw, str(exc)
            continue
        total = result if total is None else replace(result, usage=add_usage(total.usage, result.usage))
        try:
            return build(result.data), replace(total, attempts=calls)
        except (LlmOutputError, ValidationError) as exc:
            if attempt == 2:
                raise
            logger.info("answer rejected, asking for a correction: %s", exc)
            prior, feedback = result.raw, str(exc)
    raise AssertionError("unreachable: the second attempt returns or raises")


def audit(
    version: PromptVersion,
    result: LlmJsonResult,
    *,
    schema: SchemaContext,
    examples: list[SimilarRitm],
    tables_requested: list[str] | None = None,
) -> GenerationAudit:
    """What the model was shown and what it cost, so an answer can be tied back to the exact schema, examples
    and prompt version behind it."""
    return GenerationAudit(
        tables_requested=tables_requested or [],
        prompt_version=version.id,
        model=result.model,
        request_id=result.request_id,
        attempts=result.attempts,
        schema_snapshot=schema.snapshot,
        tables_in_context=schema.tables,
        example_ritms=[e.ritm.number for e in examples],
        usage=result.usage,
    )


def _render_filter_value(value: object, operator: Operator) -> str:
    if isinstance(value, list):
        return (" and " if operator is Operator.BETWEEN else " or ").join(map(str, value))
    return "" if value is None else str(value)


def render_criteria(clauses: list[tuple[str, Operator, object]]) -> str:
    """(field, operator, value) clauses in the same register as an approved RITM's report_criteria, so the
    similarity match compares like with like. Clauses are `;`-separated, which retrieval also splits on."""
    return "; ".join(
        f"{field} {operator.value.lower().replace('_', ' ')} {_render_filter_value(value, operator)}".strip()
        for field, operator, value in clauses
    )


def matched_ritms(examples: list[SimilarRitm]) -> list[MatchedRitm]:
    return [
        MatchedRitm(
            number=e.ritm.number,
            similarity=round(e.similarity, 3),
            output_fields=e.ritm.output_fields,
            report_criteria=e.ritm.report_criteria,
        )
        for e in examples
    ]


def filter_problems(label: str, item: MappedFilter | AdditionalFilter, context: SchemaContext) -> list[str]:
    """Value and type problems of one filter whose column is in `context` (an unknown column is reported separately)."""
    data_type = context.columns.get(item.table, {}).get(item.column)
    if data_type is None:
        return []
    allowed = context.allowed_values.get(item.table, {}).get(item.column)
    return filter_check_service.check_filter(label, item.operator, item.value, data_type, allowed)
