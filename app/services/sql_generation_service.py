import hashlib
import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.prompts.models import PromptVersion
from app.prompts.registry import get_prompt_version
from app.schemas.ritm import Ritm
from app.schemas.catalog import JoinEdge
from app.schemas.sql_generation import (
    ApproveSqlResponse,
    ClaudeCallAudit,
    ColumnMapping,
    ColumnMappingRequest,
    ColumnMappingResponse,
    GenerationAudit,
    MatchedRitm,
    Operator,
    RitmExtraction,
    RitmExtractionResponse,
    SqlWrite,
    SqlWriteRequest,
    SqlWriteResponse,
)
from app.services import approved_ritm_service, claude_service, schema_context_service, sql_check_service
from app.services.approved_ritm_service import ApprovedRitm, SimilarRitm
from app.services.claude_service import ClaudeJsonResult, ClaudeOutputError
from app.services.join_service import resolve_join_paths
from app.services.ritm_service import get_ritm_by_id
from app.services.schema_context_service import SchemaContext
from app.services.sql_check_service import SqlRejectedError

settings = get_settings()
logger = logging.getLogger(__name__)

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_LONG_NUMBER = re.compile(r"\b\d{9,}\b")


def redact(text: str) -> str:
    """Masks emails and long digit runs (phone / account numbers). Applied to the RITM `summary` only:
    output_fields and report_criteria carry the literal values the SQL depends on and must reach the model
    untouched. The summary can now drive fields and filters too, so a literal that appears only there (a
    long merchant or account number) is masked and cannot become a filter value; set REDACT_RITM_SUMMARY=false
    if that matters."""
    return _LONG_NUMBER.sub("[number]", _EMAIL.sub("[email]", text))


def _ticket_summary(ritm: Ritm) -> str:
    return redact(ritm.variables.summary) if settings.redact_ritm_summary else ritm.variables.summary


def _read_glossary() -> str:
    path = settings.business_glossary_path
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _snapshot(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _system_blocks(system_prompt: str, glossary: str) -> list[dict]:
    """Static-first layering for prompt caching: rules and glossary change rarely, so one breakpoint on
    the last block lets every request reuse the whole prefix. Everything per-ticket goes in the user turn."""
    blocks = [{"type": "text", "text": system_prompt}]
    if glossary:
        blocks.append({"type": "text", "text": f"<business_glossary>\n{glossary}\n</business_glossary>"})
    blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _render_example(similar: SimilarRitm) -> str:
    ritm = similar.ritm
    ticket = json.dumps({"output_fields": ritm.output_fields, "report_criteria": ritm.report_criteria}, indent=2)
    if ritm.sql:
        outcome = f"<approved_sql>\n{ritm.sql.strip()}\n</approved_sql>"
    else:
        questions = "\n".join(f"- {q}" for q in ritm.clarifications)
        outcome = f"<approved_outcome>Clarification needed; no SQL. Questions:\n{questions}\n</approved_outcome>"
    return f'<example ritm="{ritm.number}" similarity="{similar.similarity:.2f}">\n<ticket>\n{ticket}\n</ticket>\n{outcome}\n</example>'


def _ticket_sections(ritm: Ritm, user_input: str | None) -> list[str]:
    """The ticket itself, plus the requester's clarification when there is one. Shared by every stage."""
    ticket = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": ritm.variables.output_fields,
        "report_criteria": ritm.variables.report_criteria,
        "summary": _ticket_summary(ritm),
    }
    sections = [f"<ritm>\n{json.dumps(ticket, indent=2)}\n</ritm>"]
    if user_input:
        sections.append(f"<requester_clarification>\n{user_input}\n</requester_clarification>")
    return sections


def _reference_sections(schema_text: str, examples: list[SimilarRitm]) -> list[str]:
    sections = [f"<schema>\n{schema_text}\n</schema>"]
    if examples:
        sections.append("<approved_examples>\n" + "\n".join(_render_example(e) for e in examples) + "\n</approved_examples>")
    return sections


@dataclass(frozen=True)
class _PromptInputs:
    """Everything Claude is shown for a ticket apart from the ticket itself and the stage's rules."""

    examples: list[SimilarRitm]
    schema: SchemaContext
    glossary: str


def _resolve_version(prompt_version: str | None, default: str, stage: str) -> PromptVersion:
    version_id = prompt_version or default
    version = get_prompt_version(version_id, stage)
    if version is None:
        raise ValueError(f"Unknown prompt version '{version_id}' for the {stage} stage")
    return version


def _similar_examples(
    output_fields: str, report_criteria: str, exclude_number: str, catalog_db: Session
) -> list[SimilarRitm]:
    similar = approved_ritm_service.find_similar(output_fields, report_criteria, exclude_number)
    # An approved solution that references a table the catalog no longer has is stale: showing it would
    # teach Claude a schema that doesn't exist.
    existing = schema_context_service.existing_table_keys(catalog_db)
    return [s for s in similar if all(t in existing for t in s.ritm.tables)]


def _gather(output_fields: str, report_criteria: str, exclude_number: str, catalog_db: Session) -> _PromptInputs:
    """Similar approved RITMs and the schema slice for the mapping stage, from the confirmed extraction
    rendered as two texts (the fields, and the conditions)."""
    examples = _similar_examples(output_fields, report_criteria, exclude_number, catalog_db)
    queries = schema_context_service.retrieval_queries(output_fields, report_criteria, "", None)
    context = schema_context_service.build_schema_context(
        catalog_db, queries, forced_tables=[t for e in examples for t in e.ritm.tables]
    )
    return _PromptInputs(examples=examples, schema=context, glossary=_read_glossary())


def _audit(version: PromptVersion, result: ClaudeJsonResult, inputs: _PromptInputs) -> GenerationAudit:
    return GenerationAudit(
        prompt_version=version.id,
        model=result.model,
        request_id=result.request_id,
        schema_snapshot=inputs.schema.snapshot,
        glossary_snapshot=_snapshot(inputs.glossary),
        tables_in_context=inputs.schema.tables,
        example_ritms=[e.ritm.number for e in inputs.examples],
        usage=result.usage,
    )


def extract_requirements_for_ritm(
    ritm: Ritm,
    user_input: str | None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> RitmExtractionResponse:
    """Step 0: what the ticket says the report displays and filters on, read from the ticket text alone.
    No catalog, embeddings, approved examples or glossary are involved -- this is the summary the requester
    confirms before the columns are resolved against the schema."""
    version = _resolve_version(prompt_version, settings.extract_prompt_version, "extract")
    result = claude_service.generate_json(
        system=_system_blocks(version.system_prompt, ""),
        user_content="\n\n".join(_ticket_sections(ritm, user_input)),
        schema=RitmExtraction.generation_json_schema(),
        model=model,
    )
    extraction = RitmExtraction.model_validate(result.data)

    audit = ClaudeCallAudit(
        prompt_version=version.id, model=result.model, request_id=result.request_id, usage=result.usage
    )
    status = extraction.derive_status()
    logger.info("requirements extraction %s", json.dumps({"ritm": ritm.number, **audit.model_dump(), "status": status.value}))
    return RitmExtractionResponse(**extraction.model_dump(), ritm_number=ritm.number, status=status, audit=audit)


def extract_requirements(
    ritm_number: str,
    user_input: str | None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> RitmExtractionResponse | None:
    ritm = get_ritm_by_id(ritm_number)
    if ritm is None:
        return None
    return extract_requirements_for_ritm(ritm, user_input, model, prompt_version)


def _render_filter_value(value: object, operator: Operator) -> str:
    if isinstance(value, list):
        return (" and " if operator is Operator.BETWEEN else " or ").join(map(str, value))
    return "" if value is None else str(value)


def _render_criteria(clauses: list[tuple[str, Operator, object]]) -> str:
    """(field, operator, value) clauses in the same register as an approved RITM's report_criteria, so the
    similarity match compares like with like. Clauses are `;`-separated, which retrieval also splits on."""
    return "; ".join(
        f"{field} {operator.value.lower().replace('_', ' ')} {_render_filter_value(value, operator)}".strip()
        for field, operator, value in clauses
    )


def _matched_ritms(examples: list[SimilarRitm]) -> list[MatchedRitm]:
    return [
        MatchedRitm(
            number=e.ritm.number,
            similarity=round(e.similarity, 3),
            output_fields=e.ritm.output_fields,
            report_criteria=e.ritm.report_criteria,
        )
        for e in examples
    ]


def _build_mapping_content(ritm: Ritm, request: ColumnMappingRequest, inputs: _PromptInputs) -> str:
    # `source` says which part of the ticket an item came from; the requester has confirmed the item, so it is noise here.
    confirmed = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": [f.model_dump(mode="json", exclude={"source"}) for f in request.output_fields],
        "filters": [f.model_dump(mode="json", exclude={"source"}) for f in request.filters],
        "assumptions": request.assumptions,
    }
    sections = _reference_sections(inputs.schema.text, inputs.examples)
    sections.append(f"<confirmed_request>\n{json.dumps(confirmed, indent=2)}\n</confirmed_request>")
    return "\n\n".join(sections)


def _validate_mapping(mapping: ColumnMapping, request: ColumnMappingRequest, inputs: _PromptInputs) -> None:
    """Checks the model's answer against what it was actually shown. A made-up column, a dropped field or a
    citation of an example it never saw would all read as plausible in the response, so they are rejected
    here rather than left for the SQL step to trip over."""
    problems: list[str] = []

    def check_column(label: str, table: str, column: str) -> None:
        known = inputs.schema.columns.get(table)
        if known is None:
            problems.append(f"{label}: table '{table}' is not in the schema provided")
        elif column not in known:
            problems.append(f"{label}: '{table}' has no column '{column}'")

    for m in mapping.output_fields:
        check_column(f"field '{m.requested}'", m.table, m.column)
    for m in mapping.filters:
        check_column(f"filter '{m.requested}'", m.table, m.column)
    for a in mapping.additional_filters:
        check_column(f"additional filter on {a.table}.{a.column}", a.table, a.column)

    requested_fields = {f.name for f in request.output_fields}
    requested_filters = {f.field for f in request.filters}
    mapped_fields = {m.requested for m in mapping.output_fields}
    mapped_filters = {m.requested for m in mapping.filters}
    for name in sorted(mapped_fields - requested_fields):
        problems.append(f"field '{name}' was not in the confirmed request")
    for name in sorted(mapped_filters - requested_filters):
        problems.append(f"filter '{name}' was not in the confirmed request")
    if not mapping.clarifications:
        for name in sorted(requested_fields - mapped_fields):
            problems.append(f"field '{name}' was left unmapped without a clarification")
        for name in sorted(requested_filters - mapped_filters):
            problems.append(f"filter '{name}' was left unmapped without a clarification")

    shown = {e.ritm.number for e in inputs.examples}
    cited_by = [
        *((f"additional filter on {a.table}.{a.column}", a.source_ritms) for a in mapping.additional_filters),
        *((f"consideration '{c.text[:40]}'", c.source_ritms) for c in mapping.considerations),
    ]
    for label, cited in cited_by:
        if not cited or not set(cited) <= shown:
            problems.append(f"{label} cites {cited or 'no RITM'}; the examples shown were {sorted(shown) or 'none'}")

    if problems:
        raise ClaudeOutputError("Claude's column mapping was rejected: " + "; ".join(problems))


def map_columns_for_ritm(ritm: Ritm, request: ColumnMappingRequest, catalog_db: Session) -> ColumnMappingResponse:
    """Step 1, after the requester confirms the extraction: each confirmed field and condition resolved to a
    real column, plus what similar approved queries add (extra filters, considerations). Similar RITMs are
    matched on the confirmed extraction, not the raw ticket, so the requester's edits count. No SQL is written."""
    if not request.output_fields:
        raise ValueError("output_fields is empty: there is nothing to map")
    version = _resolve_version(request.prompt_version, settings.mapping_prompt_version, "mapping")
    inputs = _gather(
        ", ".join(f.name for f in request.output_fields),
        _render_criteria([(f.field, f.operator, f.value) for f in request.filters]),
        ritm.number,
        catalog_db,
    )
    result = claude_service.generate_json(
        system=_system_blocks(version.system_prompt, inputs.glossary),
        user_content=_build_mapping_content(ritm, request, inputs),
        schema=ColumnMapping.generation_json_schema(),
        model=request.model,
    )
    mapping = ColumnMapping.model_validate(result.data)
    _validate_mapping(mapping, request, inputs)

    audit = _audit(version, result, inputs)
    status = mapping.derive_status()
    logger.info("column mapping %s", json.dumps({"ritm": ritm.number, **audit.model_dump(), "status": status.value}))
    return ColumnMappingResponse(
        **mapping.model_dump(),
        ritm_number=ritm.number,
        status=status,
        matched_ritms=_matched_ritms(inputs.examples),
        audit=audit,
    )


def map_columns(request: ColumnMappingRequest, catalog_db: Session) -> ColumnMappingResponse | None:
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    return map_columns_for_ritm(ritm, request, catalog_db)


def _edge_text(edge: JoinEdge) -> str:
    return (
        f"{edge.from_schema}.{edge.from_table}.{edge.from_column} -> "
        f"{edge.to_schema}.{edge.to_table}.{edge.to_column}"
    )


def _join_plan(catalog_db: Session, tables: list[str]) -> tuple[str, list[str]]:
    """The shortest foreign-key paths connecting `tables`, as prompt text, plus the bridge tables those paths
    pass through. Deterministic, so the model picks join keys from the catalog rather than inventing them.

    Paths never run through a hub table (merchant: nearly every table points at it), so two tables that merely
    share a merchant are not joined on it: that pairs a settlement with every payment of its merchant instead of
    the payments it contains, and the shortest such path would hide the real one (customer -> merchant <-
    settlement is 2 hops; customer <- order <- payment <- settlement_payment -> settlement is 4). A table that
    can only be reached through a hub is joined that way as a flagged fallback. Equally short paths are all
    shown, with their tables, and the right path's tables must be in the schema."""
    hubs = schema_context_service.hub_tables(catalog_db)
    # The search starts from the first table; a hub as the start would fan out to all its children at once.
    ordered = [*(t for t in tables if t not in hubs), *(t for t in tables if t in hubs)]
    result = resolve_join_paths(catalog_db, ordered, no_transit=hubs)

    edges = list(result.edges)
    ambiguous_joins = list(result.ambiguous_joins)
    unreachable = list(result.unreachable_tables)
    via_hub: list[str] = []
    if unreachable:
        fallback = resolve_join_paths(catalog_db, [ordered[0], *unreachable])
        known = {edge.constraint_name for edge in edges}
        edges += [edge for edge in fallback.edges if edge.constraint_name not in known]
        ambiguous_joins += fallback.ambiguous_joins
        unreachable = list(fallback.unreachable_tables)
        via_hub = [table for table in result.unreachable_tables if table not in unreachable]

    lines = ["JOIN PLAN (shortest foreign-key paths between the tables the report needs; join only along these keys)"]
    bridge: list[str] = []

    def note_bridge(edge: JoinEdge) -> None:
        for key in (f"{edge.from_schema}.{edge.from_table}", f"{edge.to_schema}.{edge.to_table}"):
            if key not in tables and key not in bridge:
                bridge.append(key)

    for edge in edges:
        lines.append(f"  {_edge_text(edge)}")
        note_bridge(edge)
    if len(tables) == 1:
        lines.append("  (a single table: no join is needed)")
    elif not edges and not unreachable:
        lines.append("  (none needed)")
    if via_hub:
        lines.append(
            f"HUB PATH ({', '.join(via_hub)}): connected to {ordered[0]} only through a shared parent table that both "
            "reference. Such a join pairs each row with every row of the same parent. Use it only if the report is "
            "about that parent; otherwise ask."
        )
    for table in unreachable:
        lines.append(f"UNREACHABLE: {table} has no foreign-key path to {ordered[0]}")
    for ambiguous in ambiguous_joins:
        lines.append(
            f"AMBIGUOUS: {ambiguous.table_a} and {ambiguous.table_b} connect along {len(ambiguous.path_options)} "
            "equally short paths. Choose the one that relates the two directly (see the rules); the edges listed "
            "above include only the first option, as a placeholder:"
        )
        for number, option in enumerate(ambiguous.path_options, start=1):
            lines.append(f"  option {number}: " + "; ".join(_edge_text(edge) for edge in option))
            for edge in option:
                note_bridge(edge)
    return "\n".join(lines), bridge


def _check_request_columns(request: SqlWriteRequest, context: SchemaContext) -> None:
    """The body may have been edited by hand, so its columns are checked against the catalog before Claude sees them."""
    unknown = sorted(
        {
            f"{item.table}.{item.column}"
            for item in (*request.output_fields, *request.filters, *request.additional_filters)
            if item.column not in context.columns.get(item.table, {})
        }
    )
    if unknown:
        raise ValueError(f"These columns are not in the schema catalog: {', '.join(unknown)}")


def _build_write_content(ritm: Ritm, request: SqlWriteRequest, inputs: _PromptInputs, join_plan: str) -> str:
    confirmed = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": [f.model_dump(mode="json") for f in request.output_fields],
        "filters": [f.model_dump(mode="json") for f in request.filters],
        "additional_filters": [f.model_dump(mode="json", exclude={"source_ritms"}) for f in request.additional_filters],
        "considerations": [c.text for c in request.considerations],
        "assumptions": request.assumptions,
    }
    sections = _reference_sections(inputs.schema.text, inputs.examples)
    sections.insert(1, f"<join_plan>\n{join_plan}\n</join_plan>")
    sections.append(f"<confirmed_mapping>\n{json.dumps(confirmed, indent=2)}\n</confirmed_mapping>")
    return "\n\n".join(sections)


def _check_generated_sql(sql: str, request: SqlWriteRequest, context: SchemaContext) -> sql_check_service.CheckedSql:
    """Validates and formats Claude's SQL, then checks it does what was confirmed: the select list is exactly the
    requested fields in order, and every confirmed filter's column is actually used in a condition."""
    try:
        checked = sql_check_service.check_sql(sql, context.columns)
    except SqlRejectedError as exc:
        raise ClaudeOutputError(f"Claude's SQL was rejected: {exc}") from exc

    problems: list[str] = []
    expected = [f.requested for f in request.output_fields]
    if checked.select_aliases != expected:
        problems.append(f"the select list is {checked.select_aliases}, expected {expected}")
    for item in (*request.filters, *request.additional_filters):
        if (item.table.lower(), item.column.lower()) not in checked.filter_columns:
            problems.append(f"no condition uses {item.table}.{item.column}")
    if problems:
        raise ClaudeOutputError("Claude's SQL was rejected: " + "; ".join(problems))
    return checked


def write_sql_for_ritm(ritm: Ritm, request: SqlWriteRequest, catalog_db: Session) -> SqlWriteResponse:
    """Step 2, after the requester confirms the column mapping: one validated, formatted T-SQL SELECT. The schema
    Claude sees is exactly the mapped tables plus the bridge tables of their join plan. Nothing is saved: the SQL
    joins the approved pool only through approve_sql, after someone has reviewed it."""
    if not request.output_fields:
        raise ValueError("output_fields is empty: there is nothing to select")
    version = _resolve_version(request.prompt_version, settings.write_prompt_version, "write")

    tables = list(
        dict.fromkeys(item.table for item in (*request.output_fields, *request.filters, *request.additional_filters))
    )
    join_plan, bridge = _join_plan(catalog_db, tables)
    context = schema_context_service.build_exact_schema_context(catalog_db, [*tables, *bridge])
    _check_request_columns(request, context)

    examples = _similar_examples(
        ", ".join(f.requested for f in request.output_fields),
        _render_criteria([(f.requested, f.operator, f.value) for f in request.filters]),
        ritm.number,
        catalog_db,
    )
    inputs = _PromptInputs(examples=examples, schema=context, glossary=_read_glossary())
    result = claude_service.generate_json(
        system=_system_blocks(version.system_prompt, inputs.glossary),
        user_content=_build_write_content(ritm, request, inputs, join_plan),
        schema=SqlWrite.generation_json_schema(),
        model=request.model,
    )
    write = SqlWrite.model_validate(result.data)
    checked = _check_generated_sql(write.sql, request, context) if write.sql else None

    audit = _audit(version, result, inputs)
    status = write.derive_status()
    logger.info("sql write %s", json.dumps({"ritm": ritm.number, **audit.model_dump(), "status": status.value}))
    return SqlWriteResponse(
        **{**write.model_dump(), "sql": checked.sql if checked else None},
        ritm_number=ritm.number,
        status=status,
        tables=checked.tables if checked else [],
        warnings=checked.warnings if checked else [],
        matched_ritms=_matched_ritms(examples),
        audit=audit,
    )


def write_sql(request: SqlWriteRequest, catalog_db: Session) -> SqlWriteResponse | None:
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    return write_sql_for_ritm(ritm, request, catalog_db)


def approve_sql(ritm_number: str, sql: str, catalog_db: Session) -> ApproveSqlResponse | None:
    """Validates and formats reviewed SQL and stores it in the approved pool against the RITM, overwriting any
    earlier entry for it. The record keeps the ticket's own output_fields and report_criteria: retrieval matches
    on them, and later requests read what this SQL does beyond them as the conventions it carries."""
    ritm = get_ritm_by_id(ritm_number)
    if ritm is None:
        return None
    tables = sql_check_service.referenced_tables(sql)
    context = schema_context_service.build_exact_schema_context(catalog_db, tables)
    checked = sql_check_service.check_sql(sql, context.columns)

    replaced = approved_ritm_service.save(
        ApprovedRitm(
            number=ritm.number,
            output_fields=ritm.variables.output_fields,
            report_criteria=ritm.variables.report_criteria,
            tables=checked.tables,
            sql=checked.sql,
        )
    )
    logger.info("sql approved %s", json.dumps({"ritm": ritm.number, "tables": checked.tables, "replaced": replaced}))
    return ApproveSqlResponse(
        ritm_number=ritm.number, sql=checked.sql, tables=checked.tables, warnings=checked.warnings, replaced=replaced
    )
