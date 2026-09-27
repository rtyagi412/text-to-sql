"""/sql/validate: every SQL guardrail on a query, reported rather than raised, so a reviewer can check hand-edited
SQL before /sql/approve (which runs the same checks and refuses what fails them)."""

import json
import logging

from sqlalchemy.orm import Session

from app.schemas.sql_generation import SqlValidateRequest, SqlValidateResponse
from app.services import schema_context_service, sql_check_service, sql_guardrail_service
from app.services.sql_check_service import SqlRejectedError
from app.services.sql_guardrail_service import SqlRuntimeError

logger = logging.getLogger(__name__)


def validate_sql(request: SqlValidateRequest, catalog_db: Session, source_db: Session | None) -> SqlValidateResponse:
    """Static checks against the catalog (one plain SELECT, known tables and columns, performance smells), then the
    source database: compile, estimated plan and a capped sample run. The first failure ends the checks."""
    try:
        tables = sql_check_service.referenced_tables(request.sql)
        context = schema_context_service.build_exact_schema_context(catalog_db, tables)
        checked = sql_check_service.check_sql(request.sql, context.columns)
    except SqlRejectedError as exc:
        return SqlValidateResponse(valid=False, errors=[f"The SQL was rejected: {exc}"], sql=request.sql)

    errors: list[str] = []
    if request.expected_columns is not None and checked.select_aliases != request.expected_columns:
        errors.append(f"the select list is {checked.select_aliases}, expected {request.expected_columns}")
    response = SqlValidateResponse(
        valid=not errors, errors=errors, sql=checked.sql, tables=checked.tables, warnings=checked.warnings
    )
    if errors or source_db is None:
        return response

    try:
        runtime = sql_guardrail_service.run_on_source(source_db, checked.sql, request.expected_columns)
    except SqlRuntimeError as exc:
        return response.model_copy(update={"valid": False, "errors": [str(exc)]})
    logger.info("sql validated %s", json.dumps({"tables": checked.tables, "compiled": runtime.compiled}))
    return response.model_copy(
        update={
            "warnings": [*checked.warnings, *runtime.warnings],
            "compiled": runtime.compiled,
            "plan": runtime.plan,
            "sample": runtime.sample,
        }
    )
