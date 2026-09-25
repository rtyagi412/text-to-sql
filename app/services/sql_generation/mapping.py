"""Step 1, /sql/map: each confirmed field and condition resolved to a real catalog column.

`map_columns` reads top to bottom as the five steps; docs/sql-map-flow.md has the diagram."""

import json
import logging
from dataclasses import dataclass, replace
from functools import partial

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.prompts.models import PromptVersion
from app.schemas.ritm import Ritm
from app.schemas.sql_generation import ColumnMapping, ColumnMappingAnswer, ColumnMappingRequest, ColumnMappingResponse
from app.services import schema_context_service
from app.services.approved_ritm_service import SimilarRitm
from app.services.llm_service import LlmJsonResult, LlmOutputError
from app.services.ritm_service import get_ritm_by_id
from app.services.schema_context_service import SchemaContext
from app.services.sql_generation.common import (
    add_usage,
    audit,
    generate_checked,
    matched_ritms,
    reference_sections,
    render_criteria,
    resolve_version,
    similar_examples,
    system_blocks,
)
from app.services.sql_generation.mapping_validation import validate_mapping

settings = get_settings()
logger = logging.getLogger(__name__)


def map_columns(request: ColumnMappingRequest, catalog_db: Session) -> ColumnMappingResponse | None:
    """Step 1, after the requester confirms the extraction: each confirmed field and condition resolved to a
    real column, plus what similar approved queries add (extra filters, considerations). Similar RITMs are
    matched on the confirmed extraction, not the raw ticket, so the requester's edits count. No SQL is written.
    Returns None when the RITM does not exist."""
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    if not request.output_fields:
        raise ValueError("output_fields is empty: there is nothing to map")
    version = resolve_version(request.prompt_version, settings.mapping_prompt_version, "mapping")

    examples = _find_similar_examples(request, catalog_db)  # 1. what similar approved SQL looks like
    schema = _build_schema_slice(request, examples, catalog_db)  # 2. which tables and columns the model sees
    answer = _ask_model(ritm, request, version, examples, schema, catalog_db)  # 3 + 4. map, then validate

    mapping = answer.mapping
    call_audit = audit(
        version, answer.call, schema=answer.schema, examples=examples, tables_requested=answer.tables_requested
    )
    status = mapping.derive_status()
    logger.info("column mapping %s", json.dumps({"ritm": ritm.number, **call_audit.model_dump(), "status": status.value}))
    return ColumnMappingResponse(  # 5. the mapping, plus what it drew on
        **mapping.model_dump(),
        ritm_number=ritm.number,
        status=status,
        matched_ritms=matched_ritms(examples),
        audit=call_audit,
    )


# --- 1. similar approved RITMs ---------------------------------------------------------------------------------


def _request_texts(request: ColumnMappingRequest) -> tuple[str, str]:
    """The confirmed fields and conditions as two texts, in the register of an approved RITM's own
    output_fields and report_criteria: what both the similarity match and schema retrieval query with."""
    fields = ", ".join(f.name for f in request.output_fields)
    criteria = render_criteria([(f.field, f.operator, f.value) for f in request.filters])
    return fields, criteria


def _find_similar_examples(request: ColumnMappingRequest, catalog_db: Session) -> list[SimilarRitm]:
    fields, criteria = _request_texts(request)
    return similar_examples(fields, criteria, request.ritm_number, catalog_db)


# --- 2. schema slice --------------------------------------------------------------------------------------------


def _build_schema_slice(request: ColumnMappingRequest, examples: list[SimilarRitm], catalog_db: Session) -> SchemaContext:
    """The tables retrieval ranks most relevant to the request, plus every table the similar examples used."""
    fields, criteria = _request_texts(request)
    queries = schema_context_service.retrieval_queries(fields, criteria)
    return schema_context_service.build_schema_context(
        catalog_db, queries, forced_tables=[t for e in examples for t in e.ritm.tables]
    )


# --- 3 + 4. ask the model, validate its answer ----------------------------------------------------------------------


@dataclass(frozen=True)
class _ModelAnswer:
    mapping: ColumnMapping
    schema: SchemaContext  # the slice the answer was made against: larger than the first one if the model asked for tables
    tables_requested: list[str]
    call: LlmJsonResult  # token usage and attempts, summed over every call made


def _ask_model(
    ritm: Ritm,
    request: ColumnMappingRequest,
    version: PromptVersion,
    examples: list[SimilarRitm],
    schema: SchemaContext,
    catalog_db: Session,
) -> _ModelAnswer:
    """Asks the model for the mapping. Retrieval fills the slice by relevance, so a table the report needs can be
    missing from it. The model sees an index of every table and may ask for such tables once: they are added to the
    slice and it answers again. A second request is refused. Each round is `generate_checked`, which sends a
    rejected answer back once with the reasons, so the worst case is 2 rounds x 2 attempts = 4 model calls."""
    system = system_blocks(version.system_prompt, schema_context_service.table_index(catalog_db))
    known_tables = schema_context_service.existing_table_keys(catalog_db)

    requested: list[str] = []
    total: LlmJsonResult | None = None
    for may_ask in (True, False):
        answer, result = generate_checked(
            system=system,
            user_content=_build_content(ritm, request, schema, examples),
            schema=ColumnMappingAnswer.generation_json_schema(),
            model=request.model,
            build=partial(
                _parse_answer, request=request, schema=schema, examples=examples, known_tables=known_tables, may_ask=may_ask
            ),
        )
        total = result if total is None else replace(
            result, usage=add_usage(total.usage, result.usage), attempts=total.attempts + result.attempts
        )
        if not answer.tables_needed:
            break
        requested = answer.tables_needed
        logger.info("mapping asked for more tables %s", json.dumps({"ritm": ritm.number, "tables": requested}))
        schema = schema_context_service.build_exact_schema_context(catalog_db, [*schema.tables, *requested])

    mapping = ColumnMapping.model_validate(answer.model_dump(exclude={"tables_needed"}))
    return _ModelAnswer(mapping=mapping, schema=schema, tables_requested=requested, call=total)


def _build_content(ritm: Ritm, request: ColumnMappingRequest, schema: SchemaContext, examples: list[SimilarRitm]) -> str:
    """The user turn: the schema slice, the similar examples, then the confirmed request."""
    # `source` says which part of the ticket an item came from; the requester has confirmed the item, so it is noise here.
    confirmed = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": [f.model_dump(mode="json", exclude={"source"}) for f in request.output_fields],
        "filters": [f.model_dump(mode="json", exclude={"source"}) for f in request.filters],
        "assumptions": request.assumptions,
    }
    sections = reference_sections(schema.text, examples)
    sections.append(f"<confirmed_request>\n{json.dumps(confirmed, indent=2)}\n</confirmed_request>")
    return "\n\n".join(sections)


def _parse_answer(
    data: dict,
    *,
    request: ColumnMappingRequest,
    schema: SchemaContext,
    examples: list[SimilarRitm],
    known_tables: set[str],
    may_ask: bool,
) -> ColumnMappingAnswer:
    """Step 4, run on every model reply by `generate_checked`: raising LlmOutputError is what sends the reply back
    for a correction. A mapping is validated against what the model was shown; a request for more tables is only
    checked (the rest of that answer is thrown away, and the model is asked again with those tables shown)."""
    answer = ColumnMappingAnswer.model_validate(data)
    if not answer.tables_needed:
        validate_mapping(answer, request, schema, examples)
        return answer

    shown = set(schema.tables)
    problems = [
        *(f"'{t}' is not a table in <table_index>" for t in answer.tables_needed if t not in known_tables),
        *(f"'{t}' is already shown in <schema>" for t in answer.tables_needed if t in shown),
    ]
    if not may_ask:
        problems.append("the tables you asked for earlier have been added and no more can be requested: map with what is shown, or ask a clarification")
    if problems:
        raise LlmOutputError("The request for more tables was rejected: " + "; ".join(problems))
    return answer
