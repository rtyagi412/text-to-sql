"""Step 2, /sql/write: one validated, formatted T-SQL SELECT for the confirmed mapping."""

import json
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.schemas.ritm import Ritm
from app.schemas.sql_generation import SqlWrite, SqlWriteRequest, SqlWriteResponse
from app.services import schema_context_service, sql_check_service, sql_compile_service
from app.services.approved_ritm_service import SimilarRitm
from app.services.llm_service import LlmOutputError
from app.services.ritm_service import get_ritm_by_id
from app.services.schema_context_service import SchemaContext
from app.services.sql_check_service import SqlRejectedError
from app.services.sql_generation.common import (
    audit,
    filter_problems,
    generate_checked,
    matched_ritms,
    reference_sections,
    render_criteria,
    resolve_version,
    similar_examples,
    system_blocks,
)
from app.services.sql_generation.join_plan import join_plan as build_join_plan

settings = get_settings()
logger = logging.getLogger(__name__)


def _check_request_columns(request: SqlWriteRequest, context: SchemaContext) -> None:
    """The body may have been edited by hand, so its columns are checked against the catalog before the model sees them."""
    unknown = sorted(
        {
            f"{item.table}.{item.column}"
            for item in (*request.output_fields, *request.filters, *request.additional_filters)
            if item.column not in context.columns.get(item.table, {})
        }
    )
    if unknown:
        raise ValueError(f"These columns are not in the schema catalog: {', '.join(unknown)}")

    problems = [
        problem
        for item in (*request.filters, *request.additional_filters)
        for problem in filter_problems(f"{item.table}.{item.column}", item, context)
    ]
    if problems:
        raise ValueError("These filters cannot be applied as written: " + "; ".join(problems))


def _build_write_content(
    ritm: Ritm, request: SqlWriteRequest, schema: SchemaContext, examples: list[SimilarRitm], join_plan: str
) -> str:
    confirmed = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": [f.model_dump(mode="json") for f in request.output_fields],
        "filters": [f.model_dump(mode="json") for f in request.filters],
        "additional_filters": [f.model_dump(mode="json", exclude={"source_ritms"}) for f in request.additional_filters],
        "considerations": [c.text for c in request.considerations],
        "assumptions": request.assumptions,
    }
    sections = reference_sections(schema.text, examples)
    sections.insert(1, f"<join_plan>\n{join_plan}\n</join_plan>")
    sections.append(f"<confirmed_mapping>\n{json.dumps(confirmed, indent=2)}\n</confirmed_mapping>")
    return "\n\n".join(sections)


def _check_generated_sql(sql: str, request: SqlWriteRequest, context: SchemaContext) -> sql_check_service.CheckedSql:
    """Validates and formats the model's SQL, then checks it does what was confirmed: the select list is exactly the
    requested fields in order, and every confirmed filter's column is actually used in a condition."""
    try:
        checked = sql_check_service.check_sql(sql, context.columns)
    except SqlRejectedError as exc:
        raise LlmOutputError(f"The SQL was rejected: {exc}") from exc

    problems: list[str] = []
    expected = [f.requested for f in request.output_fields]
    if checked.select_aliases != expected:
        problems.append(f"the select list is {checked.select_aliases}, expected {expected}")
    for item in (*request.filters, *request.additional_filters):
        if (item.table.lower(), item.column.lower()) not in checked.filter_columns:
            problems.append(f"no condition uses {item.table}.{item.column}")
    if problems:
        raise LlmOutputError("The SQL was rejected: " + "; ".join(problems))
    return checked


@dataclass(frozen=True)
class _WriteOutcome:
    write: SqlWrite
    checked: sql_check_service.CheckedSql | None
    compiled: bool  # SQL Server compiled the query and its output columns are the requested ones
    note: str | None  # why the compile check did not run, when it was wanted but could not


def _compile_check(sql: str, expected_headers: list[str], source_db: Session | None) -> tuple[bool, str | None]:
    """Has the source database compile the query (nothing runs, no rows are read). What it refuses, or a result
    whose columns are not the requested ones, is raised as LlmOutputError so the model is shown SQL Server's own
    reason and corrects the query. A server that cannot be reached is not the query's fault: the check is skipped."""
    if source_db is None or not settings.write_compile_check:
        return False, None
    outcome = sql_compile_service.describe(source_db, sql)
    if outcome.error:
        raise LlmOutputError(f"SQL Server could not compile the query: {outcome.error}")
    if outcome.columns is None:
        return False, f"compile check skipped: {outcome.unavailable}"
    if outcome.columns != expected_headers:
        raise LlmOutputError(f"SQL Server reports the result columns as {outcome.columns}, expected {expected_headers}")
    return True, None


def write_sql_for_ritm(
    ritm: Ritm, request: SqlWriteRequest, catalog_db: Session, source_db: Session | None = None
) -> SqlWriteResponse:
    """Step 2, after the requester confirms the column mapping: one validated, formatted T-SQL SELECT. The schema
    the model sees is exactly the mapped tables plus the bridge tables of their join plan. Nothing is saved: the SQL
    joins the approved pool only through approve_sql, after someone has reviewed it. `source_db`, when given, is
    asked to compile the query (see _compile_check)."""
    if not request.output_fields:
        raise ValueError("output_fields is empty: there is nothing to select")
    version = resolve_version(request.prompt_version, settings.write_prompt_version, "write")

    tables = list(
        dict.fromkeys(item.table for item in (*request.output_fields, *request.filters, *request.additional_filters))
    )
    join_plan, bridge = build_join_plan(catalog_db, tables)
    context = schema_context_service.build_exact_schema_context(catalog_db, [*tables, *bridge])
    _check_request_columns(request, context)

    examples = similar_examples(
        ", ".join(f.requested for f in request.output_fields),
        render_criteria([(f.requested, f.operator, f.value) for f in request.filters]),
        ritm.number,
        catalog_db,
    )
    expected_headers = [f.requested for f in request.output_fields]

    def build(data: dict) -> _WriteOutcome:
        write = SqlWrite.model_validate(data)
        if not write.sql:
            return _WriteOutcome(write=write, checked=None, compiled=False, note=None)
        checked = _check_generated_sql(write.sql, request, context)
        compiled, note = _compile_check(checked.sql, expected_headers, source_db)
        return _WriteOutcome(write=write, checked=checked, compiled=compiled, note=note)

    outcome, result = generate_checked(
        system=system_blocks(version.system_prompt),
        user_content=_build_write_content(ritm, request, context, examples, join_plan),
        schema=SqlWrite.generation_json_schema(),
        model=request.model,
        build=build,
    )
    write, checked = outcome.write, outcome.checked

    call_audit = audit(version, result, schema=context, examples=examples)
    status = write.derive_status()
    logger.info("sql write %s", json.dumps({"ritm": ritm.number, **call_audit.model_dump(), "status": status.value}))
    return SqlWriteResponse(
        **{**write.model_dump(), "sql": checked.sql if checked else None},
        ritm_number=ritm.number,
        status=status,
        tables=checked.tables if checked else [],
        warnings=[*(checked.warnings if checked else []), *([outcome.note] if outcome.note else [])],
        compiled=outcome.compiled,
        matched_ritms=matched_ritms(examples),
        audit=call_audit,
    )


def write_sql(request: SqlWriteRequest, catalog_db: Session, source_db: Session | None = None) -> SqlWriteResponse | None:
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    return write_sql_for_ritm(ritm, request, catalog_db, source_db)
