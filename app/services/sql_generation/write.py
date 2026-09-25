"""Step 2, /sql/write: one validated, formatted T-SQL SELECT for the confirmed mapping.

`write_sql` reads top to bottom as the five steps, the same shape as /sql/map."""

import json
import logging
from dataclasses import dataclass
from functools import partial

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.prompts.models import PromptVersion
from app.schemas.ritm import Ritm
from app.schemas.sql_generation import SqlWrite, SqlWriteRequest, SqlWriteResponse
from app.services import schema_context_service, sql_check_service, sql_compile_service
from app.services.approved_ritm_service import SimilarRitm
from app.services.llm_service import LlmJsonResult, LlmOutputError
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


def write_sql(request: SqlWriteRequest, catalog_db: Session, source_db: Session | None = None) -> SqlWriteResponse | None:
    """Step 2, after the requester confirms the column mapping: one validated, formatted T-SQL SELECT. The schema
    the model sees is exactly the mapped tables plus the bridge tables of their join plan. Nothing is saved: the SQL
    joins the approved pool only through approve_sql, after someone has reviewed it. `source_db`, when given, is
    asked to compile the query (see _compile_check). Returns None when the RITM does not exist."""
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    if not request.output_fields:
        raise ValueError("output_fields is empty: there is nothing to select")
    version = resolve_version(request.prompt_version, settings.write_prompt_version, "write")

    join_plan, schema = _plan_joins(request, catalog_db)  # 1. how the mapped tables join, and the schema they need
    examples = _find_similar_examples(request, ritm, catalog_db)  # 2. what similar approved SQL looks like
    outcome, result = _ask_model(ritm, request, version, join_plan, schema, examples, source_db)  # 3 + 4. write, then check

    write, checked = outcome.write, outcome.checked
    call_audit = audit(version, result, schema=schema, examples=examples)
    status = write.derive_status()
    logger.info("sql write %s", json.dumps({"ritm": ritm.number, **call_audit.model_dump(), "status": status.value}))
    return SqlWriteResponse(  # 5. the SQL, plus what was checked about it
        **{**write.model_dump(), "sql": checked.sql if checked else None},
        ritm_number=ritm.number,
        status=status,
        tables=checked.tables if checked else [],
        warnings=[*(checked.warnings if checked else []), *([outcome.note] if outcome.note else [])],
        compiled=outcome.compiled,
        matched_ritms=matched_ritms(examples),
        audit=call_audit,
    )


# --- 1. join plan and schema ------------------------------------------------------------------------------------


def _plan_joins(request: SqlWriteRequest, catalog_db: Session) -> tuple[str, SchemaContext]:
    """The join plan between the tables the confirmed mapping uses, and the schema of exactly those tables plus the
    bridge tables the plan passes through. The request's columns are checked against that schema before anything
    goes to the model."""
    tables = list(
        dict.fromkeys(item.table for item in (*request.output_fields, *request.filters, *request.additional_filters))
    )
    join_plan, bridge = build_join_plan(catalog_db, tables)
    schema = schema_context_service.build_exact_schema_context(catalog_db, [*tables, *bridge])
    _check_request_columns(request, schema)
    return join_plan, schema


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


# --- 2. similar approved RITMs ----------------------------------------------------------------------------------


def _find_similar_examples(request: SqlWriteRequest, ritm: Ritm, catalog_db: Session) -> list[SimilarRitm]:
    return similar_examples(
        ", ".join(f.requested for f in request.output_fields),
        render_criteria([(f.requested, f.operator, f.value) for f in request.filters]),
        ritm.number,
        catalog_db,
    )


# --- 3 + 4. ask the model, check its SQL ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _WriteOutcome:
    write: SqlWrite
    checked: sql_check_service.CheckedSql | None
    compiled: bool  # SQL Server compiled the query and its output columns are the requested ones
    note: str | None  # why the compile check did not run, when it was wanted but could not


def _ask_model(
    ritm: Ritm,
    request: SqlWriteRequest,
    version: PromptVersion,
    join_plan: str,
    schema: SchemaContext,
    examples: list[SimilarRitm],
    source_db: Session | None,
) -> tuple[_WriteOutcome, LlmJsonResult]:
    """Asks the model for the SQL. A reply that fails the checks is sent back once with the reasons
    (`generate_checked`), so there are at most 2 model calls."""
    return generate_checked(
        system=system_blocks(version.system_prompt),
        user_content=_build_write_content(ritm, request, schema, examples, join_plan),
        schema=SqlWrite.generation_json_schema(),
        model=request.model,
        build=partial(_parse_answer, request=request, schema=schema, source_db=source_db),
    )


def _build_write_content(
    ritm: Ritm, request: SqlWriteRequest, schema: SchemaContext, examples: list[SimilarRitm], join_plan: str
) -> str:
    """The user turn: the schema, the join plan, the similar examples, then the confirmed mapping."""
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


def _parse_answer(data: dict, *, request: SqlWriteRequest, schema: SchemaContext, source_db: Session | None) -> _WriteOutcome:
    """Step 4, run on every model reply by `generate_checked`: raising LlmOutputError is what sends the reply back
    for a correction. The SQL must pass our own checks and then SQL Server's compile check."""
    write = SqlWrite.model_validate(data)
    if not write.sql:
        return _WriteOutcome(write=write, checked=None, compiled=False, note=None)
    checked = _check_generated_sql(write.sql, request, schema)
    expected_headers = [f.requested for f in request.output_fields]
    compiled, note = _compile_check(checked.sql, expected_headers, source_db)
    return _WriteOutcome(write=write, checked=checked, compiled=compiled, note=note)


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
