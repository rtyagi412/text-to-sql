"""Step 2, /sql/write: one validated, formatted T-SQL SELECT for the confirmed mapping.

`write_sql` reads top to bottom as the five steps, the same shape as /sql/map."""

import json
import logging
from dataclasses import dataclass, replace
from functools import partial

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.prompts.models import PromptVersion
from app.schemas.ritm import Ritm
from app.schemas.sql_generation import SqlWrite, SqlWriteAnswer, SqlWriteRequest, SqlWriteResponse
from app.services import schema_context_service, sql_check_service, sql_guardrail_service
from app.services.approved_ritm_service import SimilarRitm
from app.services.llm_service import LlmJsonResult, LlmOutputError
from app.services.ritm_service import get_ritm_by_id
from app.services.schema_context_service import SchemaContext
from app.services.sql_check_service import SqlRejectedError
from app.services.sql_guardrail_service import RuntimeReport, SqlRuntimeError
from app.services.sql_generation.common import (
    audit,
    check_tables_needed,
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
    the model sees is the mapped tables (including the tables a derived field is computed over) plus the bridge
    tables of their join plan, and it may ask once for a table beyond them. Nothing is saved: the SQL
    joins the approved pool only through approve_sql, after someone has reviewed it. `source_db`, when given, is
    asked to compile, plan and sample-run the query (see _runtime_check). Returns None when the RITM does not exist."""
    ritm = get_ritm_by_id(request.ritm_number)
    if ritm is None:
        return None
    if not request.output_fields:
        raise ValueError("output_fields is empty: there is nothing to select")
    version = resolve_version(request.prompt_version, settings.write_prompt_version, "write")

    join_plan, schema = _plan_joins(request, catalog_db)  # 1. how the mapped tables join, and the schema they need
    examples = _find_similar_examples(request, ritm, catalog_db)  # 2. what similar approved SQL looks like
    asked = _ask_model(ritm, request, version, join_plan, schema, examples, catalog_db, source_db)  # 3 + 4. write, then check

    outcome = asked.outcome
    write, checked, runtime = outcome.write, outcome.checked, outcome.runtime
    call_audit = audit(version, asked.call, schema=asked.schema, examples=examples, tables_requested=asked.tables_requested)
    status = write.derive_status()
    logger.info("sql write %s", json.dumps({"ritm": ritm.number, **call_audit.model_dump(), "status": status.value}))
    return SqlWriteResponse(  # 5. the SQL, plus what was checked about it
        **{**write.model_dump(exclude={"tables_needed"}), "sql": checked.sql if checked else None},
        ritm_number=ritm.number,
        status=status,
        tables=checked.tables if checked else [],
        warnings=[*(checked.warnings if checked else []), *(runtime.warnings if runtime else [])],
        compiled=runtime.compiled if runtime else False,
        plan=runtime.plan if runtime else None,
        sample=runtime.sample if runtime else None,
        matched_ritms=matched_ritms(examples),
        audit=call_audit,
    )


# --- 1. join plan and schema ------------------------------------------------------------------------------------


def _plan_joins(request: SqlWriteRequest, catalog_db: Session, extra_tables: list[str] | None = None) -> tuple[str, SchemaContext]:
    """The join plan between the tables the confirmed mapping uses, and the schema of exactly those tables plus the
    bridge tables the plan passes through. The report's own tables are those of its plain columns and filters. The
    tables a derived field is computed over, and `extra_tables` the model asked for, are branches of the plan:
    each joins to the report's tables on its own. The request's columns are checked against that schema before
    anything goes to the model."""
    plain = [
        *(ref for field in request.output_fields if field.derived is None for ref in field.column_refs),
        *request.filters,
        *request.additional_filters,
    ]
    tables = list(
        dict.fromkeys(
            [
                *(item.table for item in plain),
                *(f.value_column.table for f in request.filters if f.value_column),
            ]
        )
    )
    branches = list(
        dict.fromkeys(
            [
                *(ref.table for field in request.output_fields if field.derived is not None for ref in field.column_refs),
                *(extra_tables or []),
            ]
        )
    )
    join_plan, bridge = build_join_plan(catalog_db, tables, branches)
    schema = schema_context_service.build_exact_schema_context(catalog_db, [*tables, *branches, *bridge])
    _check_request_columns(request, schema)
    return join_plan, schema


def _check_request_columns(request: SqlWriteRequest, context: SchemaContext) -> None:
    """The body may have been edited by hand, so its columns are checked against the catalog before the model sees them."""
    unknown = sorted(
        {
            f"{item.table}.{item.column}"
            for item in (
                *(ref for field in request.output_fields for ref in field.column_refs),
                *request.filters,
                *(f.value_column for f in request.filters if f.value_column),
                *request.additional_filters,
            )
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
        render_criteria([(f.requested, f.operator, f.shown_value) for f in request.filters]),
        ritm.number,
        catalog_db,
    )


# --- 3 + 4. ask the model, check its SQL ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _WriteOutcome:
    write: SqlWriteAnswer
    checked: sql_check_service.CheckedSql | None
    runtime: RuntimeReport | None  # what the source database said: compiled, estimated plan, sample run


@dataclass(frozen=True)
class _Asked:
    outcome: _WriteOutcome
    schema: SchemaContext  # the schema the answer was made against: larger than the first one if the model asked for tables
    tables_requested: list[str]
    call: LlmJsonResult  # token usage and attempts, summed over every call made


def _ask_model(
    ritm: Ritm,
    request: SqlWriteRequest,
    version: PromptVersion,
    join_plan: str,
    schema: SchemaContext,
    examples: list[SimilarRitm],
    catalog_db: Session,
    source_db: Session | None,
) -> _Asked:
    """Asks the model for the SQL. A reply that fails the checks is sent back once with the reasons
    (`generate_checked`). The mapping's tables are all the model is shown, so a table the query needs can be missing
    from them: the model sees an index of every table and may ask for such tables once. They are added as branches
    of the join plan and it answers again; a second request is refused. Worst case: 2 rounds x 2 attempts = 4 calls."""
    system = system_blocks(version.system_prompt, schema_context_service.table_index(catalog_db))
    known_tables = schema_context_service.existing_table_keys(catalog_db)

    requested: list[str] = []
    total: LlmJsonResult | None = None
    for may_ask in (True, False):
        outcome, result = generate_checked(
            system=system,
            user_content=_build_write_content(ritm, request, schema, examples, join_plan),
            schema=SqlWriteAnswer.generation_json_schema(),
            model=request.model,
            build=partial(
                _parse_answer, request=request, schema=schema, known_tables=known_tables, may_ask=may_ask, source_db=source_db
            ),
        )
        total = result.after(total)
        if not outcome.write.tables_needed:
            break
        requested = outcome.write.tables_needed
        logger.info("write asked for more tables %s", json.dumps({"ritm": ritm.number, "tables": requested}))
        join_plan, schema = _plan_joins(request, catalog_db, extra_tables=requested)

    return _Asked(outcome=outcome, schema=schema, tables_requested=requested, call=total)


def _build_write_content(
    ritm: Ritm, request: SqlWriteRequest, schema: SchemaContext, examples: list[SimilarRitm], join_plan: str
) -> str:
    """The user turn: the schema, the join plan, the similar examples, then the confirmed mapping."""
    confirmed = {
        "ritm_number": ritm.number,
        "title": ritm.name,
        "output_fields": [f.model_dump(mode="json", exclude_none=True) for f in request.output_fields],
        "filters": [f.model_dump(mode="json") for f in request.filters],
        "additional_filters": [f.model_dump(mode="json", exclude={"source_ritms"}) for f in request.additional_filters],
        "considerations": [c.text for c in request.considerations],
        "assumptions": request.assumptions,
    }
    sections = reference_sections(schema.text, examples)
    sections.insert(1, f"<join_plan>\n{join_plan}\n</join_plan>")
    sections.append(f"<confirmed_mapping>\n{json.dumps(confirmed, indent=2)}\n</confirmed_mapping>")
    return "\n\n".join(sections)


def _parse_answer(
    data: dict,
    *,
    request: SqlWriteRequest,
    schema: SchemaContext,
    known_tables: set[str],
    may_ask: bool,
    source_db: Session | None,
) -> _WriteOutcome:
    """Step 4, run on every model reply by `generate_checked`: raising LlmOutputError is what sends the reply back
    for a correction. The SQL must pass our own checks and then SQL Server's (compile, plan, sample run). A request
    for more tables is only checked (the rest of that answer is thrown away, and the model is asked again with them shown)."""
    write = SqlWriteAnswer.model_validate(data)
    if write.tables_needed:
        check_tables_needed(write.tables_needed, schema, known_tables, may_ask)
        return _WriteOutcome(write=write, checked=None, runtime=None)
    if not write.sql:
        return _WriteOutcome(write=write, checked=None, runtime=None)
    checked = _check_generated_sql(write.sql, request, schema)
    expected_headers = [f.requested for f in request.output_fields]
    runtime = _runtime_check(checked.sql, expected_headers, source_db)
    return _WriteOutcome(write=write, checked=checked, runtime=runtime)


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
    for f in request.filters:
        if f.value_column and (f.value_column.table.lower(), f.value_column.column.lower()) not in checked.filter_columns:
            problems.append(f"no condition uses {f.value_column.table}.{f.value_column.column}, which '{f.requested}' compares with")
    if checked.unjoined:
        problems.append(f"{', '.join(checked.unjoined)} joined with no join condition; join along <join_plan>")
    read = {table.lower() for table in checked.tables}
    for field in request.output_fields:
        for ref in field.derived.sources if field.derived else []:
            if ref.table.lower() not in read:
                problems.append(
                    f"'{field.requested}' is computed from {ref.table}.{ref.column}, but the query does not read {ref.table}"
                )
    if problems:
        raise LlmOutputError("The SQL was rejected: " + "; ".join(problems))
    return checked


def _runtime_check(sql: str, expected_headers: list[str], source_db: Session | None) -> RuntimeReport | None:
    """Has the source database compile the query, estimate its plan and run it capped at a few rows
    (sql_guardrail_service). What SQL Server refuses or fails on, or a result whose columns are not the requested
    ones, is raised as LlmOutputError so the model is shown SQL Server's own reason and corrects the query. A server
    that cannot be reached is not the query's fault: the checks are skipped and a warning says so."""
    if source_db is None:
        return None
    try:
        return sql_guardrail_service.run_on_source(source_db, sql, expected_headers)
    except SqlRuntimeError as exc:
        raise LlmOutputError(str(exc)) from exc
